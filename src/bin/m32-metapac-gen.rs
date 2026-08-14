use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::fs;
use std::io::Write as _;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use clap::Parser;
use mcu_compat_gen::generate::run_generator;
use mcu_compat_gen::hash::sha256_tree;
use serde_json::json;
use stm32_data_serde::Chip;

const MARKER: &str = ".m32-metapac-generation.json";

#[derive(Parser)]
#[command(about = "从 m32/stm32-data staging 调用官方 stm32-metapac-gen")]
struct Args {
    #[arg(long)]
    data_dir: PathBuf,
    #[arg(long)]
    output: PathBuf,
    #[arg(long)]
    report: PathBuf,
}

fn chips(data_dir: &Path) -> Result<(Vec<String>, BTreeSet<String>, usize)> {
    let chips_dir = data_dir.join("chips");
    let mut names = Vec::new();
    let mut riscv = BTreeSet::new();
    let mut registers = BTreeSet::new();
    for entry in fs::read_dir(&chips_dir)
        .with_context(|| format!("读取 staging Chip 目录 {} 失败", chips_dir.display()))?
    {
        let path = entry?.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let chip: Chip = serde_json::from_slice(
            &fs::read(&path).with_context(|| format!("读取 Chip {} 失败", path.display()))?,
        )
        .with_context(|| format!("反序列化 Chip {} 失败", path.display()))?;
        let stem = path
            .file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or_default();
        if stem != chip.name {
            bail!("Chip 文件名和 name 不一致：{stem} != {}", chip.name);
        }
        let riscv_cores = chip
            .cores
            .iter()
            .filter(|core| core.name.starts_with("riscv"))
            .count();
        if riscv_cores != 0 && riscv_cores != chip.cores.len() {
            bail!("暂不支持混合 ARM/RISC-V 多核 Chip：{}", chip.name);
        }
        if riscv_cores != 0 {
            riscv.insert(chip.name.clone());
        }
        for core in &chip.cores {
            for peripheral in &core.peripherals {
                if let Some(register) = &peripheral.registers {
                    registers.insert(format!("{}_{}.json", register.kind, register.version));
                }
            }
        }
        names.push(chip.name);
    }
    names.sort();
    if names.is_empty() {
        bail!("staging 不包含 Chip");
    }
    for register in &registers {
        let path = data_dir.join("registers").join(register);
        if !path.is_file() {
            bail!("staging 缺少 register IR：{}", path.display());
        }
    }
    Ok((names, riscv, registers.len()))
}

fn braced_item_end(source: &str, start: usize) -> Result<usize> {
    let open = source[start..]
        .find('{')
        .map(|offset| start + offset)
        .context("生成的 Rust 项缺少左花括号")?;
    let mut depth = 0usize;
    for (offset, character) in source[open..].char_indices() {
        match character {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return Ok(open + offset + character.len_utf8());
                }
            }
            _ => {}
        }
    }
    bail!("生成的 Rust 项花括号不闭合")
}

fn replace_braced_item(source: &mut String, marker: &str, replacement: &str) -> Result<()> {
    let starts = source
        .match_indices(marker)
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    if starts.len() != 1 {
        bail!(
            "生成的 PAC 中 {marker:?} 应出现一次，实际 {} 次",
            starts.len()
        );
    }
    let end = braced_item_end(source, starts[0])?;
    source.replace_range(starts[0]..end, replacement);
    Ok(())
}

fn remove_once(source: &mut String, value: &str) -> Result<()> {
    let matches = source
        .match_indices(value)
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    if matches.len() != 1 {
        bail!(
            "生成的 PAC 中 {value:?} 应出现一次，实际 {} 次",
            matches.len()
        );
    }
    source.replace_range(matches[0]..matches[0] + value.len(), "");
    Ok(())
}

fn rewrite_riscv_pac(source: &str) -> Result<String> {
    let mut source = source.to_owned();
    replace_braced_item(
        &mut source,
        "unsafe impl cortex_m :: interrupt :: InterruptNumber for Interrupt",
        "impl Interrupt { #[inline(always)] pub const fn number(self) -> u16 { self as u16 } }",
    )?;
    replace_braced_item(&mut source, "# [cfg (feature = \"rt\")]\nmod _vectors", "")?;
    remove_once(
        &mut source,
        "# [cfg (feature = \"rt\")]\npub use cortex_m_rt :: interrupt ;",
    )?;
    remove_once(
        &mut source,
        "# [cfg (feature = \"rt\")]\npub use Interrupt as interrupt ;",
    )?;
    for forbidden in ["cortex_m", "vector_table.interrupts", "__INTERRUPTS"] {
        if source.contains(forbidden) {
            bail!("RISC-V PAC 仍包含 ARM 专用内容：{forbidden}");
        }
    }
    Ok(source)
}

fn rewrite_riscv_pacs(output: &Path, chips: &BTreeSet<String>) -> Result<()> {
    for chip in chips {
        let path = output
            .join("src/chips")
            .join(chip.to_ascii_lowercase())
            .join("pac.rs");
        let source = fs::read_to_string(&path)
            .with_context(|| format!("读取 RISC-V PAC {} 失败", path.display()))?;
        fs::write(&path, rewrite_riscv_pac(&source)?)
            .with_context(|| format!("写入 RISC-V PAC {} 失败", path.display()))?;
    }
    Ok(())
}

fn write_build_script(output: &Path, chips: &[String]) -> Result<()> {
    let mut entries = String::new();
    for chip in chips {
        writeln!(
            entries,
            "        (\"CARGO_FEATURE_{}\", \"{}\"),",
            chip.to_ascii_uppercase().replace('-', "_"),
            chip.to_ascii_lowercase()
        )?;
    }
    let source = format!(
        r#"use std::env;
#[cfg(feature = "rt")]
use std::path::PathBuf;

fn main() {{
    let chips = [
{entries}    ];
    let mut selected = chips
        .iter()
        .filter(|(feature, _)| env::var_os(feature).is_some())
        .map(|(_, chip)| *chip);
    let chip = selected.next().expect("No MCU Cargo feature enabled");
    assert!(selected.next().is_none(), "Multiple MCU Cargo features enabled");

    #[cfg(feature = "rt")]
    println!(
        "cargo:rustc-link-search={{}}/src/chips/{{}}",
        PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap()).display(),
        chip,
    );
    println!("cargo:rustc-env=STM32_METAPAC_PAC_PATH=chips/{{chip}}/pac.rs");
    println!("cargo:rustc-env=STM32_METAPAC_METADATA_PATH=chips/{{chip}}/metadata.rs");
    println!("cargo:rerun-if-changed=build.rs");
}}
"#
    );
    fs::write(output.join("build.rs"), source).context("写入厂商无关 metapac build.rs 失败")
}

fn main() -> Result<()> {
    let args = Args::parse();
    let (chip_names, riscv_chips, register_files) = chips(&args.data_dir)?;
    let riscv_chip_names = riscv_chips.iter().cloned().collect::<Vec<_>>();
    let data_tree_sha256 = sha256_tree(&args.data_dir)?;
    let parent = args.output.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)
        .with_context(|| format!("创建输出父目录 {} 失败", parent.display()))?;
    let temporary = tempfile::Builder::new()
        .prefix(".m32-metapac-")
        .tempdir_in(parent)
        .with_context(|| format!("创建临时生成目录 {} 失败", parent.display()))?;
    run_generator(&args.data_dir, temporary.path(), chip_names.clone())?;
    rewrite_riscv_pacs(temporary.path(), &riscv_chips)?;
    write_build_script(temporary.path(), &chip_names)?;
    fs::write(
        temporary.path().join(MARKER),
        serde_json::to_vec_pretty(&json!({
            "schema_version": 1,
            "chips": chip_names.len(),
            "riscv_chips": riscv_chips.len(),
            "riscv_devices": &riscv_chip_names,
            "data_tree_sha256": data_tree_sha256,
        }))?,
    )?;
    let output_tree_sha256 = sha256_tree(temporary.path())?;
    let mut report_bytes = serde_json::to_vec_pretty(&json!({
        "schema_version": 1,
        "chips": chip_names.len(),
        "riscv_chips": riscv_chips.len(),
        "riscv_devices": &riscv_chip_names,
        "register_files": register_files,
        "data_tree_sha256": data_tree_sha256,
        "output_tree_sha256": output_tree_sha256,
    }))?;
    report_bytes.push(b'\n');
    let report_parent = args.report.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(report_parent)
        .with_context(|| format!("创建报告目录 {} 失败", report_parent.display()))?;
    let mut report = tempfile::NamedTempFile::new_in(report_parent)
        .with_context(|| format!("创建临时报告 {} 失败", report_parent.display()))?;
    report
        .write_all(&report_bytes)
        .context("写入 metapac 报告失败")?;
    report
        .as_file()
        .sync_all()
        .context("同步 metapac 报告失败")?;
    if args.output.exists() {
        if !args.output.join(MARKER).is_file() {
            bail!("拒绝覆盖非 m32-metapac 生成目录：{}", args.output.display());
        }
        fs::remove_dir_all(&args.output)
            .with_context(|| format!("清理旧生成目录 {} 失败", args.output.display()))?;
    }
    fs::rename(temporary.keep(), &args.output)
        .with_context(|| format!("发布生成目录 {} 失败", args.output.display()))?;
    report
        .persist(&args.report)
        .map_err(|error| error.error)
        .with_context(|| format!("发布生成报告 {} 失败", args.report.display()))?;
    println!(
        "chips={} output={}",
        chip_names.len(),
        args.output.display()
    );
    Ok(())
}
