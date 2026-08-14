use std::fs;
use std::process::Command;

use serde_json::Value;

#[test]
fn 官方生成器消费真实staging目录() {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("data");
    let output = temp.path().join("output");
    let report = temp.path().join("report.json");
    fs::create_dir_all(data.join("chips")).unwrap();
    fs::create_dir_all(data.join("registers")).unwrap();
    let fixture = fs::read_to_string("tests/fixtures/upstream/chips/STM32F103C8.json").unwrap();
    fs::write(
        data.join("chips/GD32TESTARM.json"),
        fixture.replace("STM32F103C8", "GD32TESTARM"),
    )
    .unwrap();
    fs::write(
        data.join("chips/GD32TESTRISCV.json"),
        fixture
            .replace("STM32F103C8", "GD32TESTRISCV")
            .replace("\"cm3\"", "\"riscv32imac\""),
    )
    .unwrap();
    fs::copy(
        "tests/fixtures/upstream/registers/crc_v1.json",
        data.join("registers/crc_v1.json"),
    )
    .unwrap();

    let result = Command::new(env!("CARGO_BIN_EXE_m32-metapac-gen"))
        .arg("--data-dir")
        .arg(&data)
        .arg("--output")
        .arg(&output)
        .arg("--report")
        .arg(&report)
        .output()
        .unwrap();

    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let arm_pac = fs::read_to_string(output.join("src/chips/gd32testarm/pac.rs")).unwrap();
    let riscv_pac = fs::read_to_string(output.join("src/chips/gd32testriscv/pac.rs")).unwrap();
    assert!(arm_pac.contains("cortex_m :: interrupt :: InterruptNumber"));
    assert!(!riscv_pac.contains("cortex_m"));
    assert!(!riscv_pac.contains("vector_table.interrupts"));
    assert!(!riscv_pac.contains("__INTERRUPTS"));
    assert!(riscv_pac.contains("pub const fn number"));
    assert!(output.join("src/peripherals/crc_v1.rs").is_file());
    let report: Value = serde_json::from_slice(&fs::read(report).unwrap()).unwrap();
    assert_eq!(report["schema_version"], 1);
    assert_eq!(report["chips"], 2);
    assert_eq!(report["register_files"], 1);
    assert_eq!(report["data_tree_sha256"].as_str().unwrap().len(), 64);
    assert_eq!(report["output_tree_sha256"].as_str().unwrap().len(), 64);

    let check = Command::new("cargo")
        .args(["check", "--manifest-path"])
        .arg(output.join("Cargo.toml"))
        .args(["--features", "pac,metadata,gd32testarm"])
        .env("CARGO_TARGET_DIR", temp.path().join("target"))
        .output()
        .unwrap();
    assert!(
        check.status.success(),
        "{}",
        String::from_utf8_lossy(&check.stderr)
    );
}
