use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;
use std::process::Command;

use mcu_compat_gen::generate::{GenerateRequest, generate_repository};
use mcu_compat_gen::hash::sha256_tree;
use mcu_compat_gen::lock::SourceLock;
use mcu_compat_gen::mapping::Mapping;
use serde_json::json;
use sha2::{Digest, Sha256};
use stm32_metapac_gen::{Gen, Options};
use walkdir::WalkDir;

const FIXTURE: &str = "tests/fixtures/upstream";

fn mapping() -> Mapping {
    Mapping::read("compat/gigadevice/gd32f103c8.json").unwrap()
}

fn run(program: &str, args: &[&str], directory: &Path) -> String {
    let output = Command::new(program)
        .args(args)
        .current_dir(directory)
        .env("GIT_AUTHOR_NAME", "测试")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "测试")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{program} 失败：{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

fn format_rust(root: &Path) {
    let mut files: Vec<_> = WalkDir::new(root)
        .into_iter()
        .map(Result::unwrap)
        .filter(|entry| entry.file_type().is_file())
        .filter(|entry| entry.path().extension().is_some_and(|value| value == "rs"))
        .map(|entry| entry.into_path())
        .collect();
    files.sort();
    for file in files {
        let output = Command::new("rustfmt")
            .args([
                "--config",
                "max_width=120,group_imports=StdExternalCrate,imports_granularity=Module",
                "--skip-children",
                "--unstable-features",
                "--edition",
                "2024",
            ])
            .arg(&file)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "rustfmt {} 失败：{}",
            file.display(),
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

fn official_fixture() -> (tempfile::TempDir, SourceLock) {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("data");
    fs::create_dir_all(data.join("chips")).unwrap();
    fs::create_dir_all(data.join("registers")).unwrap();
    fs::copy(
        Path::new(FIXTURE).join("chips/STM32F103C8.json"),
        data.join("chips/STM32F103C8.json"),
    )
    .unwrap();
    fs::copy(
        Path::new(FIXTURE).join("registers/crc_v1.json"),
        data.join("registers/crc_v1.json"),
    )
    .unwrap();

    let metapac = temp.path().join("stm32-metapac");
    Gen::new(Options {
        chips: vec!["STM32F103C8".into()],
        out_dir: metapac.clone(),
        data_dir: data,
    })
    .run_gen();
    format_rust(&metapac);

    run("git", &["init", "-q"], temp.path());
    run("git", &["add", "."], temp.path());
    run("git", &["commit", "-q", "-m", "fixture"], temp.path());
    let revision = run("git", &["rev-parse", "HEAD"], temp.path());
    let mut lock = SourceLock::read("tests/fixtures/source-lock.toml").unwrap();
    lock.upstream.stm32_data_generated = revision;
    (temp, lock)
}

fn request<'a>(
    official: &'a Path,
    output: &'a Path,
    mappings: &'a [Mapping],
    source_lock: &'a SourceLock,
) -> GenerateRequest<'a> {
    GenerateRequest {
        official_generated: official,
        output,
        mappings,
        source_lock,
        source_lock_sha256: format!(
            "{:x}",
            Sha256::digest(source_lock.to_toml().unwrap().as_bytes())
        ),
        include_test: true,
        projection_manifest: None,
        native_data: None,
        projection_data: None,
    }
}

#[test]
fn generates_real_private_chip_from_an_official_alias() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];

    generate_repository(request(official.path(), &output, &mappings, &lock)).unwrap();

    let chip = output.join("src/chips/gd32f103c8");
    assert!(chip.join("pac.rs").is_file());
    assert!(chip.join("metadata.rs").is_file());
    assert!(chip.join("device.x").is_file());
    let metadata = fs::read_to_string(chip.join("metadata.rs")).unwrap();
    assert!(metadata.contains("name: \"GD32F103C8\""));
    assert!(!metadata.contains("name: \"STM32F103C8\""));
}

#[test]
fn rejects_revision_mismatch_without_publishing_output() {
    let (official, mut lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];

    lock.upstream.stm32_data_generated = "0".repeat(40);
    let error =
        generate_repository(request(official.path(), &output, &mappings, &lock)).unwrap_err();

    assert!(error.to_string().contains("revision"), "{error:#}");
    assert!(!output.exists());
}

#[test]
fn refuses_nonempty_output_without_changing_it() {
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    fs::create_dir(&output).unwrap();
    fs::write(output.join("keep"), b"untouched").unwrap();
    let mappings = [mapping()];
    let lock = SourceLock::read("tests/fixtures/source-lock.toml").unwrap();

    let error = generate_repository(request(
        Path::new("does-not-exist"),
        &output,
        &mappings,
        &lock,
    ))
    .unwrap_err();

    assert!(error.to_string().contains("非空"), "{error:#}");
    assert_eq!(fs::read(output.join("keep")).unwrap(), b"untouched");
}

#[test]
fn rejects_invalid_chip_patch_before_publishing() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mut invalid = mapping();
    invalid.patch = json!({"cores": "不是数组"});
    let mappings = [invalid];

    let error =
        generate_repository(request(official.path(), &output, &mappings, &lock)).unwrap_err();

    assert!(error.to_string().contains("Chip"), "{error:#}");
    assert!(!output.exists());
}

#[test]
fn real_name_cannot_be_overridden_by_the_patch() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mut renamed = mapping();
    renamed.patch = json!({"name": "STM32F103C8"});
    let mappings = [renamed];

    generate_repository(request(official.path(), &output, &mappings, &lock)).unwrap();

    let metadata = fs::read_to_string(output.join("src/chips/gd32f103c8/metadata.rs")).unwrap();
    assert!(metadata.contains("name: \"GD32F103C8\""));
}

#[test]
fn rejects_missing_register_input_before_publishing() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mut missing = mapping();
    missing.patch = json!({
        "cores": [{
            "name": "cm3",
            "peripherals": [{
                "name": "MISSING",
                "address": 0,
                "registers": {"kind": "missing", "version": "v1", "block": "MISSING"}
            }],
            "nvic_priority_bits": 4,
            "interrupts": [],
            "dma_channels": [],
            "pins": []
        }]
    });
    let mappings = [missing];

    let error =
        generate_repository(request(official.path(), &output, &mappings, &lock)).unwrap_err();

    assert!(error.to_string().contains("missing_v1.json"), "{error:#}");
    assert!(!output.exists());
}

#[test]
fn generated_build_script_matches_the_tested_contract() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];

    generate_repository(request(official.path(), &output, &mappings, &lock)).unwrap();

    assert_eq!(
        fs::read(output.join("build.rs")).unwrap(),
        fs::read("tests/fixtures/metapac/build.rs").unwrap()
    );
    assert_eq!(
        fs::read_to_string(output.join("src/compat.rs")).unwrap(),
        concat!(
            "pub static COMPATIBLE_CHIPS: &[(&str, &str)] = &[\n",
            "    (\"gd32f103c8\", \"stm32f103c8\"),\n",
            "];\n",
        )
    );

    let executable = target.path().join("build-script-tests");
    let source = output.join("build.rs");
    run(
        "rustc",
        &[
            "--test",
            "--edition=2024",
            source.to_str().unwrap(),
            "-o",
            executable.to_str().unwrap(),
        ],
        target.path(),
    );
    run(executable.to_str().unwrap(), &[], target.path());
}

#[test]
fn projection_manifest_generates_real_chip_and_records_its_hash() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let manifest_path = target.path().join("projection.json");
    let mapping = mapping();
    fs::write(
        &manifest_path,
        serde_json::to_vec_pretty(&json!({
            "schema_version": 1,
            "projections": [{
                "chip": mapping.chip,
                "profile": mapping.alias,
                "rust_target": mapping.rust_target,
                "status": "projected",
                "patch": mapping.patch,
                "source_hashes": {"models_sha256": "0".repeat(64)},
            }],
        }))
        .unwrap(),
    )
    .unwrap();
    let native_data = official.path().join("data");
    let mut request = request(official.path(), &output, &[], &lock);
    request.include_test = false;
    request.projection_manifest = Some(&manifest_path);
    request.native_data = Some(&native_data);

    generate_repository(request).unwrap();

    assert!(output.join("src/chips/gd32f103c8/pac.rs").is_file());
    let generated: serde_json::Value =
        serde_json::from_slice(&fs::read(output.join("generation.json")).unwrap()).unwrap();
    assert_eq!(
        generated["projection_manifest_sha256"],
        mcu_compat_gen::hash::sha256_file(&manifest_path).unwrap()
    );
}

#[test]
fn test_mapping_requires_explicit_opt_in() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];
    let mut request = request(official.path(), &output, &mappings, &lock);
    request.include_test = false;

    let error = generate_repository(request).unwrap_err();

    assert!(error.to_string().contains("--include-test"), "{error:#}");
    assert!(!output.exists());
}

#[test]
fn native_generation_has_no_test_chip_or_feature() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mut request = request(official.path(), &output, &[], &lock);
    request.include_test = false;

    generate_repository(request).unwrap();

    assert!(!output.join("src/chips/gd32f103c8").exists());
    assert_eq!(
        fs::read_to_string(output.join("src/compat.rs")).unwrap(),
        "pub static COMPATIBLE_CHIPS: &[(&str, &str)] = &[];\n"
    );
    let cargo = fs::read_to_string(output.join("Cargo.toml")).unwrap();
    assert!(!cargo.contains("gd32f103c8"));
    let all_chips = fs::read_to_string(output.join("src/all_chips.rs")).unwrap();
    assert!(!all_chips.contains("gd32f103c8"));
    let manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(output.join("generation.json")).unwrap()).unwrap();
    assert_eq!(manifest["include_test"], false);
    assert_eq!(manifest["chips"], serde_json::json!([]));
}

#[test]
fn generation_manifest_records_locked_inputs_and_real_chips() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];

    generate_repository(request(official.path(), &output, &mappings, &lock)).unwrap();

    let manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(output.join("generation.json")).unwrap()).unwrap();
    assert_eq!(manifest["schema"], 1);
    assert_eq!(
        manifest["source_lock_sha256"],
        format!("{:x}", Sha256::digest(lock.to_toml().unwrap().as_bytes()))
    );
    assert_eq!(
        manifest["upstream"]["stm32_data_generated"],
        lock.upstream.stm32_data_generated
    );
    assert_eq!(manifest["target_db"]["revision"], lock.target_db.revision);
    assert_eq!(manifest["include_test"], true);
    assert_eq!(manifest["chips"][0]["chip"], "gd32f103c8");
    assert_eq!(manifest["chips"][0]["profile"], "stm32f103c8");
    assert_eq!(
        manifest["chips"][0]["projection_sha256"]
            .as_str()
            .unwrap()
            .len(),
        64
    );
}

#[test]
fn generated_repository_diff_is_limited_to_the_whitelist() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];

    generate_repository(request(official.path(), &output, &mappings, &lock)).unwrap();

    let baseline = file_tree(&official.path().join("stm32-metapac"));
    let generated = file_tree(&output);
    let paths: BTreeSet<_> = baseline.keys().chain(generated.keys()).collect();
    let mut unexpected = Vec::new();
    for path in paths {
        if baseline.get(path) == generated.get(path) {
            continue;
        }
        let allowed = matches!(
            path.as_str(),
            "build.rs" | "Cargo.toml" | "src/compat.rs" | "generation.json"
        ) || path.starts_with("src/chips/gd32")
            || path.starts_with("src/chips/compat_metadata_");
        if !allowed {
            unexpected.push(path.clone());
        }
    }
    assert!(unexpected.is_empty(), "出现白名单外差异：{unexpected:?}");

    let official_cargo =
        fs::read_to_string(official.path().join("stm32-metapac/Cargo.toml")).unwrap();
    let generated_cargo = fs::read_to_string(output.join("Cargo.toml")).unwrap();
    let retain_semantic_lines = |contents: &str| {
        contents
            .lines()
            .filter(|line| {
                !line.starts_with("repository = ") && !line.starts_with("description = ")
            })
            .collect::<Vec<_>>()
            .join("\n")
    };
    assert_eq!(
        retain_semantic_lines(&official_cargo),
        retain_semantic_lines(&generated_cargo)
    );
    assert!(
        generated_cargo
            .contains("repository = \"https://github.com/itswenb/embassy-mcu-compat-generated\"")
    );
    assert!(generated_cargo.contains("description = \"支持厂商兼容 MCU 的 STM32 外设访问包。\""));
}

#[test]
fn repeated_generation_has_the_same_tree_hash() {
    let (official, lock) = official_fixture();
    let first_root = tempfile::tempdir().unwrap();
    let second_root = tempfile::tempdir().unwrap();
    let first = first_root.path().join("generated");
    let second = second_root.path().join("generated");
    let mappings = [mapping()];

    generate_repository(request(official.path(), &first, &mappings, &lock)).unwrap();
    generate_repository(request(official.path(), &second, &mappings, &lock)).unwrap();

    assert_eq!(sha256_tree(&first).unwrap(), sha256_tree(&second).unwrap());
}

#[test]
fn source_lock_hash_drift_fails_before_publication() {
    let (official, lock) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];
    let mut request = request(official.path(), &output, &mappings, &lock);
    request.source_lock_sha256 =
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff".into();

    let error = generate_repository(request).unwrap_err();

    assert!(error.to_string().contains("来源锁 SHA-256"), "{error:#}");
    assert!(!output.exists());
}

#[test]
fn cargo_reports_unknown_chip_and_rejects_wrong_stm32_feature() {
    let unknown = build_script_failure("unknown32", "stm32f103c8");
    assert!(unknown.contains("EMBASSY_MCU_COMPAT_CHIP"), "{unknown}");
    assert!(unknown.contains("unknown32"), "{unknown}");
    assert!(unknown.contains("gd32f103c8"), "{unknown}");

    let wrong = build_script_failure("gd32f103c8", "stm32f103cb");
    assert!(wrong.contains("stm32f103c8"), "{wrong}");

    let (success, stderr) = build_script_result("gd32f103c8", "stm32f103c8");
    assert!(success, "匹配的 STM32 profile 应成功：{stderr}");
}

fn build_script_failure(chip: &str, feature: &str) -> String {
    let (success, stderr) = build_script_result(chip, feature);
    assert!(!success, "错误路径意外编译成功");
    stderr
}

fn build_script_result(chip: &str, feature: &str) -> (bool, String) {
    let temp = tempfile::tempdir().unwrap();
    fs::create_dir_all(temp.path().join("src")).unwrap();
    fs::copy(
        "tests/fixtures/metapac/build.rs",
        temp.path().join("build.rs"),
    )
    .unwrap();
    fs::copy(
        "tests/fixtures/metapac/src/compat.rs",
        temp.path().join("src/compat.rs"),
    )
    .unwrap();
    fs::write(temp.path().join("src/lib.rs"), "#![no_std]\n").unwrap();
    fs::write(
        temp.path().join("Cargo.toml"),
        concat!(
            "[package]\n",
            "name = \"build-selection-check\"\n",
            "version = \"0.0.0\"\n",
            "edition = \"2024\"\n",
            "\n",
            "[features]\n",
            "stm32f103c8 = []\n",
            "stm32f103cb = []\n",
        ),
    )
    .unwrap();

    let output = Command::new("cargo")
        .args(["check", "--offline", "--quiet", "--features", feature])
        .env("EMBASSY_MCU_COMPAT_CHIP", chip)
        .env("CARGO_TARGET_DIR", temp.path().join("target"))
        .current_dir(temp.path())
        .output()
        .unwrap();
    (
        output.status.success(),
        String::from_utf8(output.stderr).unwrap(),
    )
}

fn file_tree(root: &Path) -> BTreeMap<String, Vec<u8>> {
    WalkDir::new(root)
        .into_iter()
        .map(Result::unwrap)
        .filter(|entry| entry.file_type().is_file())
        .map(|entry| {
            let relative = entry.path().strip_prefix(root).unwrap();
            (
                relative.to_string_lossy().replace('\\', "/"),
                fs::read(entry.path()).unwrap(),
            )
        })
        .collect()
}
