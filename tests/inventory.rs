use std::path::Path;
use std::{fs, io::Write};

use mcu_compat_gen::target_db::{self, Selector, load_inventory};

const ROOT: &str = "tests/fixtures/target-db";

#[test]
fn target_db_normalizes_variants_and_processors() {
    let selectors = [Selector {
        vendor: "GigaDevice".into(),
        pack_pattern: "*_DFP".into(),
    }];
    let devices = load_inventory(Path::new(ROOT), "fixture-index", &selectors).unwrap();

    assert_eq!(devices.len(), 4);
    assert!(devices.iter().any(|device| device.chip == "gd32f100c8"));
    assert!(!devices.iter().any(|device| device.chip == "gd32f103c8"));
    assert!(devices.iter().any(|device| device.chip == "gd32f103c8t6"));
    assert!(devices.iter().any(|device| device.chip == "gd32f103c8u6"));

    let multicore = devices
        .iter()
        .find(|device| device.chip == "gd32w515pi")
        .unwrap();
    assert_eq!(
        multicore
            .processors
            .iter()
            .map(|processor| processor.name.as_deref())
            .collect::<Vec<_>>(),
        [Some("CM33"), Some("WIFI")]
    );
}

#[test]
fn target_db_rejects_index_drift() {
    let error = load_inventory(
        Path::new(ROOT),
        "different-index",
        &[Selector {
            vendor: "GigaDevice".into(),
            pack_pattern: "*_DFP".into(),
        }],
    )
    .unwrap_err();

    assert!(error.to_string().contains("索引 SHA-256"));
}

#[test]
fn selector_supports_one_wildcard() {
    assert!(target_db::wildcard_match("*_DFP", "GD32F10x_DFP"));
    assert!(target_db::wildcard_match("GD32*", "GD32F10x_DFP"));
    assert!(target_db::wildcard_match("GD32*DFP", "GD32F10x_DFP"));
    assert!(!target_db::wildcard_match("GD32*DFP", "STM32F1xx_DFP"));
    assert!(!target_db::wildcard_match("*_*", "GD32F10x_DFP"));
}

#[test]
fn target_db_rejects_one_chip_from_multiple_selected_packs() {
    let source = Path::new(ROOT);
    let temp = tempfile::tempdir().unwrap();
    fs::copy(
        source.join("metadata.json"),
        temp.path().join("metadata.json"),
    )
    .unwrap();
    fs::copy(
        source.join("devices.jsonl"),
        temp.path().join("devices.jsonl"),
    )
    .unwrap();
    let mut data = fs::OpenOptions::new()
        .append(true)
        .open(temp.path().join("devices.jsonl"))
        .unwrap();
    data.write_all(
        concat!(
            r#"{"device":"GD32F100C8","device_kind":"device","parent_device":null,"processor":null,"core":"Cortex-M3","fpu":"NO_FPU","endian":"Little-endian","rust_target":"thumbv7m-none-eabi","source_pack_vendor":"GigaDevice","source_pack_name":"GD32F10x_ALT_DFP","source_pack_version":"1.0.0","source_pdsc":"GigaDevice.GD32F10x_ALT_DFP.pdsc"}"#,
            "\n"
        )
        .as_bytes(),
    )
    .unwrap();

    let error = load_inventory(
        temp.path(),
        "fixture-index",
        &[Selector {
            vendor: "GigaDevice".into(),
            pack_pattern: "*_DFP".into(),
        }],
    )
    .unwrap_err();

    assert!(error.to_string().contains("多个 Pack"));
    assert!(error.to_string().contains("gd32f100c8"));
}
