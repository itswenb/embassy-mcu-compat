use std::fs;
use std::path::Path;
use std::process::Command;

use mcu_compat_gen::generate::{GenerateRequest, generate_repository};
use mcu_compat_gen::mapping::Mapping;
use serde_json::json;
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

fn official_fixture() -> (tempfile::TempDir, String) {
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
    (temp, revision)
}

fn request<'a>(
    official: &'a Path,
    output: &'a Path,
    mappings: &'a [Mapping],
    revision: &'a str,
) -> GenerateRequest<'a> {
    GenerateRequest {
        official_generated: official,
        output,
        mappings,
        expected_revision: revision,
    }
}

#[test]
fn generates_real_private_chip_from_an_official_alias() {
    let (official, revision) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];

    generate_repository(request(official.path(), &output, &mappings, &revision)).unwrap();

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
    let (official, _) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mappings = [mapping()];

    let error = generate_repository(request(
        official.path(),
        &output,
        &mappings,
        &"0".repeat(40),
    ))
    .unwrap_err();

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

    let error = generate_repository(request(
        Path::new("does-not-exist"),
        &output,
        &mappings,
        "missing",
    ))
    .unwrap_err();

    assert!(error.to_string().contains("非空"), "{error:#}");
    assert_eq!(fs::read(output.join("keep")).unwrap(), b"untouched");
}

#[test]
fn rejects_invalid_chip_patch_before_publishing() {
    let (official, revision) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mut invalid = mapping();
    invalid.patch = json!({"cores": "不是数组"});
    let mappings = [invalid];

    let error =
        generate_repository(request(official.path(), &output, &mappings, &revision)).unwrap_err();

    assert!(error.to_string().contains("Chip"), "{error:#}");
    assert!(!output.exists());
}

#[test]
fn real_name_cannot_be_overridden_by_the_patch() {
    let (official, revision) = official_fixture();
    let target = tempfile::tempdir().unwrap();
    let output = target.path().join("generated");
    let mut renamed = mapping();
    renamed.patch = json!({"name": "STM32F103C8"});
    let mappings = [renamed];

    generate_repository(request(official.path(), &output, &mappings, &revision)).unwrap();

    let metadata = fs::read_to_string(output.join("src/chips/gd32f103c8/metadata.rs")).unwrap();
    assert!(metadata.contains("name: \"GD32F103C8\""));
}

#[test]
fn rejects_missing_register_input_before_publishing() {
    let (official, revision) = official_fixture();
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
        generate_repository(request(official.path(), &output, &mappings, &revision)).unwrap_err();

    assert!(error.to_string().contains("missing_v1.json"), "{error:#}");
    assert!(!output.exists());
}
