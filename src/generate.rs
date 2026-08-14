use std::collections::BTreeSet;
use std::fs;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Component, Path};
use std::process::Command;

use anyhow::{Context, Result, bail};
use serde_json::Value;
use sha2::{Digest, Sha256};
use stm32_data_serde::Chip;
use stm32_metapac_gen::{Gen, Options};
use walkdir::WalkDir;

use crate::hash::sha256_file;
use crate::lock::SourceLock;
use crate::mapping::{Mapping, Scope, load_mappings};
use crate::merge_patch::apply_merge_patch;
use crate::sources::verify_sources;

const COMPAT_BUILD_RS: &str = include_str!("../tests/fixtures/metapac/build.rs");
const GENERATED_REPOSITORY: &str = "https://github.com/itswenb/embassy-mcu-compat-generated";
const GENERATED_DESCRIPTION: &str = "支持厂商兼容 MCU 的 STM32 外设访问包。";

pub struct GenerateRequest<'a> {
    pub official_generated: &'a Path,
    pub output: &'a Path,
    pub mappings: &'a [Mapping],
    pub source_lock: &'a SourceLock,
    pub source_lock_sha256: String,
    pub include_test: bool,
}

pub fn run_generate(
    official_generated: &Path,
    output: &Path,
    lock_path: &Path,
    cache_dir: &Path,
    compat_dir: &Path,
    include_test: bool,
) -> Result<()> {
    let lock = SourceLock::read(lock_path)?;
    let source_lock_sha256 = sha256_file(lock_path)?;
    verify_sources(&lock, cache_dir)?;
    let project_root = lock_path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let pack_root = cache_dir.join("cmsis");
    let mappings = load_mappings(compat_dir, &lock, &[project_root, &pack_root])?;
    let selected: Vec<_> = mappings
        .into_values()
        .filter(|mapping| mapping.ready() || (include_test && mapping.mapping.scope == Scope::Test))
        .map(|mapping| mapping.mapping)
        .collect();
    generate_repository(GenerateRequest {
        official_generated,
        output,
        mappings: &selected,
        source_lock: &lock,
        source_lock_sha256,
        include_test,
    })
}

pub fn generate_repository(request: GenerateRequest<'_>) -> Result<()> {
    validate_output(request.output)?;
    verify_source_lock_hash(request.source_lock, &request.source_lock_sha256)?;
    if !request.include_test
        && request
            .mappings
            .iter()
            .any(|mapping| mapping.scope == Scope::Test)
    {
        bail!("未启用 --include-test，拒绝生成 test 映射");
    }
    verify_official_checkout(
        request.official_generated,
        &request.source_lock.upstream.stm32_data_generated,
    )?;

    let parent = request
        .output
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)
        .with_context(|| format!("创建输出父目录 {} 失败", parent.display()))?;
    let temporary = tempfile::Builder::new()
        .prefix(".mcu-compat-generate-")
        .tempdir_in(parent)
        .with_context(|| format!("在 {} 创建生成临时目录失败", parent.display()))?;
    let data_dir = temporary.path().join("data");
    let generated_dir = temporary.path().join("generated");
    let publication_dir = temporary.path().join("publication");

    let chip_names = prepare_staging_data(
        &request.official_generated.join("data"),
        &data_dir,
        request.mappings,
    )?;
    if !chip_names.is_empty() {
        run_generator(&data_dir, &generated_dir, chip_names)?;
        format_rust_tree(&generated_dir)?;
        verify_shared_modules(
            &generated_dir,
            &request.official_generated.join("stm32-metapac"),
        )?;
    }

    copy_tree(
        &request.official_generated.join("stm32-metapac"),
        &publication_dir,
    )?;
    if !request.mappings.is_empty() {
        merge_private_chips(&generated_dir, &publication_dir, request.mappings)?;
    }
    write_compat_table(&publication_dir, request.mappings)?;
    fs::write(publication_dir.join("build.rs"), COMPAT_BUILD_RS)
        .context("写入兼容 build.rs 失败")?;
    rewrite_package_manifest(&publication_dir.join("Cargo.toml"))?;
    write_generation_manifest(&publication_dir, &request)?;
    publish(&publication_dir, request.output)
}

fn verify_source_lock_hash(lock: &SourceLock, actual: &str) -> Result<()> {
    let contents = lock.to_toml()?;
    let expected = format!("{:x}", Sha256::digest(contents.as_bytes()));
    if actual != expected {
        bail!("来源锁 SHA-256 不匹配：期望 {expected}，实际 {actual}");
    }
    Ok(())
}

pub fn prepare_staging_data(
    official_data: &Path,
    staging_data: &Path,
    mappings: &[Mapping],
) -> Result<Vec<String>> {
    fs::create_dir_all(staging_data.join("chips"))
        .with_context(|| format!("创建 staging chips 目录 {} 失败", staging_data.display()))?;
    fs::create_dir_all(staging_data.join("registers")).with_context(|| {
        format!(
            "创建 staging registers 目录 {} 失败",
            staging_data.display()
        )
    })?;

    let mut chip_names = Vec::new();
    let mut seen_chips = BTreeSet::new();
    let mut registers = BTreeSet::new();
    for mapping in mappings {
        if !seen_chips.insert(mapping.chip.as_str()) {
            bail!("生成请求包含重复真实型号：{}", mapping.chip);
        }
        validate_component(&mapping.source.device, "真实型号")?;
        let alias = mapping.alias.to_ascii_uppercase();
        validate_component(&alias, "alias")?;
        let source = official_data.join("chips").join(format!("{alias}.json"));
        let bytes = fs::read(&source)
            .with_context(|| format!("读取官方 alias Chip {} 失败", source.display()))?;
        let mut value: Value = serde_json::from_slice(&bytes)
            .with_context(|| format!("解析官方 alias Chip {} 失败", source.display()))?;
        apply_merge_patch(&mut value, &mapping.patch);
        let mut chip: Chip = serde_json::from_value(value)
            .with_context(|| format!("映射 {} patch 后无法解析为 Chip", mapping.chip))?;
        chip.name.clone_from(&mapping.source.device);
        if chip.cores.len() != 1 {
            bail!(
                "映射 {} patch 后包含 {} 个 core；当前只支持单核兼容生成",
                mapping.chip,
                chip.cores.len()
            );
        }

        for core in &chip.cores {
            for peripheral in &core.peripherals {
                if let Some(register) = &peripheral.registers {
                    validate_component(&register.kind, "register kind")?;
                    validate_component(&register.version, "register version")?;
                    registers.insert((register.kind.clone(), register.version.clone()));
                }
            }
        }

        let mut encoded = serde_json::to_vec_pretty(&chip)
            .with_context(|| format!("编码真实 Chip {} 失败", mapping.chip))?;
        encoded.push(b'\n');
        fs::write(
            staging_data
                .join("chips")
                .join(format!("{}.json", mapping.source.device)),
            encoded,
        )
        .with_context(|| format!("写入真实 Chip {} 失败", mapping.chip))?;
        chip_names.push(mapping.source.device.clone());
    }

    for (kind, version) in registers {
        let filename = format!("{kind}_{version}.json");
        let source = official_data.join("registers").join(&filename);
        if !source.is_file() {
            bail!("官方数据缺少映射引用的 register：{filename}");
        }
        fs::copy(&source, staging_data.join("registers").join(&filename))
            .with_context(|| format!("复制 register {} 失败", source.display()))?;
    }
    Ok(chip_names)
}

fn validate_output(output: &Path) -> Result<()> {
    if !output.exists() {
        return Ok(());
    }
    let metadata = fs::symlink_metadata(output)
        .with_context(|| format!("读取输出 {} 元数据失败", output.display()))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        bail!("输出路径必须不存在或是空目录：{}", output.display());
    }
    if fs::read_dir(output)
        .with_context(|| format!("读取输出目录 {} 失败", output.display()))?
        .next()
        .is_some()
    {
        bail!("输出目录非空，拒绝覆盖：{}", output.display());
    }
    Ok(())
}

fn verify_official_checkout(root: &Path, expected_revision: &str) -> Result<()> {
    let revision = git_output(root, &["rev-parse", "HEAD"])?;
    if revision != expected_revision {
        bail!(
            "官方 stm32-data-generated revision 不匹配：期望 {expected_revision}，实际 {revision}"
        );
    }
    let status = git_output(root, &["status", "--porcelain=v1"])?;
    if !status.is_empty() {
        bail!("官方 stm32-data-generated 工作树不是干净的：{status}");
    }
    for relative in ["data/chips", "data/registers", "stm32-metapac"] {
        if !root.join(relative).is_dir() {
            bail!("官方 checkout 缺少目录 {relative}");
        }
    }
    Ok(())
}

fn git_output(root: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .with_context(|| format!("运行 git -C {} 失败", root.display()))?;
    if !output.status.success() {
        bail!(
            "git -C {} {} 失败：{}{}",
            root.display(),
            args.join(" "),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

pub fn run_generator(data_dir: &Path, output: &Path, chips: Vec<String>) -> Result<()> {
    let result = catch_unwind(AssertUnwindSafe(|| {
        Gen::new(Options {
            chips,
            out_dir: output.to_path_buf(),
            data_dir: data_dir.to_path_buf(),
        })
        .run_gen();
    }));
    match result {
        Ok(()) => Ok(()),
        Err(payload) => {
            let message = payload
                .downcast_ref::<&str>()
                .copied()
                .or_else(|| payload.downcast_ref::<String>().map(String::as_str))
                .unwrap_or("未知 panic");
            bail!("stm32-metapac-gen 生成失败：{message}")
        }
    }
}

fn format_rust_tree(root: &Path) -> Result<()> {
    let mut files = files_with_extension(root, "rs")?;
    files.sort();
    for path in files {
        let output = Command::new("rustfmt")
            .args([
                "--config",
                "max_width=120,group_imports=StdExternalCrate,imports_granularity=Module",
                "--skip-children",
                "--unstable-features",
                "--edition",
                "2024",
            ])
            .arg(&path)
            .output()
            .with_context(|| format!("运行 rustfmt {} 失败", path.display()))?;
        if !output.status.success() {
            bail!(
                "rustfmt {} 失败：{}{}",
                path.display(),
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    Ok(())
}

fn verify_shared_modules(generated: &Path, official_metapac: &Path) -> Result<()> {
    for directory in ["peripherals", "registers"] {
        let generated_root = generated.join("src").join(directory);
        for path in files_with_extension(&generated_root, "rs")? {
            let relative = path
                .strip_prefix(generated)
                .with_context(|| format!("{} 不在 staging 输出中", path.display()))?;
            let official = official_metapac.join(relative);
            if !official.is_file() {
                bail!("官方 metapac 缺少共享模块：{}", relative.display());
            }
            let actual =
                fs::read(&path).with_context(|| format!("读取生成模块 {} 失败", path.display()))?;
            let expected = fs::read(&official)
                .with_context(|| format!("读取官方模块 {} 失败", official.display()))?;
            if actual != expected {
                bail!("生成共享模块与官方基线不同：{}", relative.display());
            }
        }
    }
    Ok(())
}

fn merge_private_chips(generated: &Path, publication: &Path, mappings: &[Mapping]) -> Result<()> {
    let mut copied_dedup = BTreeSet::new();
    for mapping in mappings {
        let source = generated.join("src/chips").join(&mapping.chip);
        let destination = publication.join("src/chips").join(&mapping.chip);
        fs::create_dir_all(&destination)
            .with_context(|| format!("创建真实芯片目录 {} 失败", destination.display()))?;
        for filename in ["pac.rs", "device.x"] {
            let path = source.join(filename);
            if !path.is_file() {
                bail!("生成器没有生成 {} 的 {filename}", mapping.chip);
            }
            fs::copy(&path, destination.join(filename))
                .with_context(|| format!("复制 {} 失败", path.display()))?;
        }

        let metadata_path = source.join("metadata.rs");
        let metadata = fs::read_to_string(&metadata_path)
            .with_context(|| format!("读取 {} 失败", metadata_path.display()))?;
        let (dedup, rewritten) = rewrite_metadata_include(&metadata)?;
        fs::write(destination.join("metadata.rs"), rewritten)
            .with_context(|| format!("写入 {} metadata 失败", mapping.chip))?;
        if copied_dedup.insert(dedup.clone()) {
            let source = generated.join("src/chips").join(&dedup);
            let destination = publication.join("src/chips").join(dedup.replacen(
                "metadata_",
                "compat_metadata_",
                1,
            ));
            fs::copy(&source, &destination)
                .with_context(|| format!("复制 metadata 去重文件 {} 失败", source.display()))?;
        }
    }
    Ok(())
}

fn write_compat_table(publication: &Path, mappings: &[Mapping]) -> Result<()> {
    let mut entries: Vec<_> = mappings
        .iter()
        .map(|mapping| (&mapping.chip, &mapping.alias))
        .collect();
    entries.sort();

    let mut contents = String::from("const COMPATIBLE_CHIPS: &[(&str, &str)] = &[");
    if entries.is_empty() {
        contents.push_str("];\n");
    } else {
        contents.push('\n');
        for (chip, alias) in entries {
            contents.push_str("    (");
            contents.push_str(&serde_json::to_string(chip).context("编码兼容芯片名失败")?);
            contents.push_str(", ");
            contents.push_str(&serde_json::to_string(alias).context("编码兼容 alias 失败")?);
            contents.push_str("),\n");
        }
        contents.push_str("];\n");
    }
    fs::write(publication.join("src/compat.rs"), contents).context("写入静态兼容芯片表失败")
}

fn rewrite_package_manifest(path: &Path) -> Result<()> {
    let mut contents = fs::read_to_string(path)
        .with_context(|| format!("读取生成包清单 {} 失败", path.display()))?;
    for (original, replacement) in [
        (
            "repository = \"https://github.com/embassy-rs/stm32-data\"",
            format!("repository = {GENERATED_REPOSITORY:?}"),
        ),
        (
            "description = \"Peripheral Access Crate (PAC) for all STM32 chips, including metadata.\"",
            format!("description = {GENERATED_DESCRIPTION:?}"),
        ),
    ] {
        if contents.matches(original).count() != 1 {
            bail!("官方 Cargo.toml 描述字段不再唯一匹配：{original}");
        }
        contents = contents.replacen(original, &replacement, 1);
    }
    fs::write(path, contents).with_context(|| format!("写入生成包清单 {} 失败", path.display()))
}

fn write_generation_manifest(publication: &Path, request: &GenerateRequest<'_>) -> Result<()> {
    let mut chips = Vec::new();
    for mapping in request.mappings {
        let bytes = serde_json::to_vec(mapping)
            .with_context(|| format!("编码映射 {} 失败", mapping.chip))?;
        chips.push(serde_json::json!({
            "chip": mapping.chip,
            "alias": mapping.alias,
            "mapping_sha256": format!("{:x}", Sha256::digest(bytes)),
        }));
    }
    chips.sort_by(|left, right| left["chip"].as_str().cmp(&right["chip"].as_str()));

    let manifest = serde_json::json!({
        "schema": 1,
        "source_lock_sha256": &request.source_lock_sha256,
        "index": &request.source_lock.index,
        "target_db": &request.source_lock.target_db,
        "tools": &request.source_lock.tools,
        "upstream": &request.source_lock.upstream,
        "packs": &request.source_lock.packs,
        "include_test": request.include_test,
        "chips": chips,
    });
    let mut contents = serde_json::to_vec_pretty(&manifest).context("编码生成清单失败")?;
    contents.push(b'\n');
    fs::write(publication.join("generation.json"), contents).context("写入 generation.json 失败")
}

fn rewrite_metadata_include(contents: &str) -> Result<(String, String)> {
    let newline = contents
        .find('\n')
        .ok_or_else(|| anyhow::anyhow!("生成 metadata 缺少首行换行"))?;
    let first = &contents[..newline];
    let name = first
        .strip_prefix("include!(\"../")
        .and_then(|value| value.strip_suffix("\");"))
        .ok_or_else(|| anyhow::anyhow!("生成 metadata include 格式变化：{first:?}"))?;
    let number = name
        .strip_prefix("metadata_")
        .and_then(|value| value.strip_suffix(".rs"))
        .filter(|value| value.len() == 4 && value.bytes().all(|byte| byte.is_ascii_digit()))
        .ok_or_else(|| anyhow::anyhow!("生成 metadata include 文件名异常：{name:?}"))?;
    if contents[newline + 1..].contains("include!(\"../metadata_") {
        bail!("生成 metadata 包含多个去重 include");
    }
    let dedup = format!("metadata_{number}.rs");
    let rewritten = format!(
        "include!(\"../compat_metadata_{number}.rs\");{}",
        &contents[newline..]
    );
    Ok((dedup, rewritten))
}

fn files_with_extension(root: &Path, extension: &str) -> Result<Vec<std::path::PathBuf>> {
    let mut files = Vec::new();
    for entry in WalkDir::new(root).follow_links(false) {
        let entry = entry.with_context(|| format!("遍历 {} 失败", root.display()))?;
        if entry.file_type().is_symlink() {
            bail!("生成目录不接受符号链接：{}", entry.path().display());
        }
        if entry.file_type().is_file()
            && entry
                .path()
                .extension()
                .is_some_and(|value| value == extension)
        {
            files.push(entry.into_path());
        }
    }
    Ok(files)
}

fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    if !source.is_dir() {
        bail!("复制来源不是目录：{}", source.display());
    }
    for entry in WalkDir::new(source).follow_links(false) {
        let entry = entry.with_context(|| format!("遍历 {} 失败", source.display()))?;
        if entry.file_type().is_symlink() {
            bail!("官方基线不接受符号链接：{}", entry.path().display());
        }
        let relative = entry
            .path()
            .strip_prefix(source)
            .with_context(|| format!("{} 不在 {} 内", entry.path().display(), source.display()))?;
        let target = destination.join(relative);
        if entry.file_type().is_dir() {
            fs::create_dir_all(&target)
                .with_context(|| format!("创建 {} 失败", target.display()))?;
        } else if entry.file_type().is_file() {
            fs::copy(entry.path(), &target).with_context(|| {
                format!(
                    "复制 {} 到 {} 失败",
                    entry.path().display(),
                    target.display()
                )
            })?;
        }
    }
    Ok(())
}

fn publish(publication: &Path, output: &Path) -> Result<()> {
    if output.exists() {
        fs::remove_dir(output)
            .with_context(|| format!("移除空输出目录 {} 失败", output.display()))?;
    }
    fs::rename(publication, output)
        .with_context(|| format!("原子发布生成仓库到 {} 失败", output.display()))
}

fn validate_component(value: &str, label: &str) -> Result<()> {
    let path = Path::new(value);
    if value.is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
        || value.contains(['/', '\\'])
    {
        bail!("{label} 不是安全文件名：{value:?}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::format_rust_tree;

    #[test]
    fn formatting_matches_the_upstream_width_contract() {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("sample.rs");
        fs::write(
            &source,
            r#"fn demo(value: &Value, f: &mut core::fmt::Formatter) -> core::fmt::Result {
    f.debug_struct("Crcpr").field("crcpoly", &value.crcpoly()).finish()
}
"#,
        )
        .unwrap();

        format_rust_tree(temp.path()).unwrap();

        let formatted = fs::read_to_string(source).unwrap();
        assert!(
            formatted.contains(
                r#"    f.debug_struct("Crcpr").field("crcpoly", &value.crcpoly()).finish()"#
            ),
            "实际格式化结果：\n{formatted}"
        );
    }
}
