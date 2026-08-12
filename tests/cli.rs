use std::process::Command;

#[test]
fn exposes_only_the_supported_top_level_commands() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcu-compat-gen"))
        .arg("--help")
        .output()
        .unwrap();
    let stdout = String::from_utf8(output.stdout).unwrap();

    assert!(output.status.success());
    for command in ["sources", "audit", "generate"] {
        assert!(stdout.contains(command), "帮助中缺少 {command}");
    }
}

#[test]
fn sources_exposes_only_update() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcu-compat-gen"))
        .args(["sources", "--help"])
        .output()
        .unwrap();
    let stdout = String::from_utf8(output.stdout).unwrap();

    assert!(output.status.success());
    assert!(stdout.contains("update"));
}

#[test]
fn sources_update_enters_the_source_pipeline() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcu-compat-gen"))
        .args([
            "sources",
            "update",
            "--lock",
            "tests/fixtures/does-not-exist.toml",
        ])
        .output()
        .unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();

    assert!(!output.status.success());
    assert!(stderr.contains("读取来源锁"), "{stderr}");
    assert!(!stderr.contains("尚未实现"), "{stderr}");
}

#[test]
fn audit_enters_the_offline_audit_pipeline() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcu-compat-gen"))
        .args([
            "audit",
            "--frozen",
            "--lock",
            "tests/fixtures/does-not-exist.toml",
        ])
        .output()
        .unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();

    assert!(!output.status.success());
    assert!(stderr.contains("读取来源锁"), "{stderr}");
    assert!(!stderr.contains("尚未实现"), "{stderr}");
}
