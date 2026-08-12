use std::collections::BTreeMap;
use std::path::Path;
use std::{fs, io::Write};

use mcu_compat_gen::lock::{PackLock, SourceLock};
use mcu_compat_gen::mapping::{CheckedMapping, Mapping, MappingAudit, Scope};
use mcu_compat_gen::report::{InventoryStatus, build_report, render_report, write_report};
use mcu_compat_gen::target_db::{self, Selector, load_inventory};
use mcu_compat_gen::target_db::{Device, Processor};

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

fn report_device(chip: &str, endian: &str) -> Device {
    Device {
        chip: chip.into(),
        original_name: chip.to_ascii_uppercase(),
        device_kind: "device".into(),
        parent_device: None,
        pack_vendor: "GigaDevice".into(),
        pack_name: "GD32F10x_DFP".into(),
        pack_version: "2.0.3".into(),
        source_pdsc: "GigaDevice.GD32F10x_DFP.pdsc".into(),
        processors: vec![Processor {
            name: None,
            core: Some("Cortex-M3".into()),
            fpu: Some("NO_FPU".into()),
            endian: Some(endian.into()),
            rust_target: Some("thumbv7m-none-eabi".into()),
        }],
    }
}

fn report_mapping(chip: &str, scope: Scope, blockers: &[&str]) -> CheckedMapping {
    let mut mapping = Mapping::read("compat/gigadevice/gd32f103c8.json").unwrap();
    mapping.chip = chip.into();
    mapping.source.device = chip.to_ascii_uppercase();
    mapping.scope = scope;
    CheckedMapping {
        mapping,
        audit: MappingAudit {
            blockers: blockers.iter().map(|reason| (*reason).to_owned()).collect(),
        },
    }
}

fn report_fixture() -> (SourceLock, BTreeMap<String, CheckedMapping>) {
    let mut lock = SourceLock::read("tests/fixtures/source-lock.toml").unwrap();
    lock.packs = vec![PackLock {
        vendor: "GigaDevice".into(),
        name: "GD32F10x_DFP".into(),
        version: "2.0.3".into(),
        url: "https://example.invalid/GigaDevice.GD32F10x_DFP.2.0.3.pack".into(),
        archive: ".Download/GigaDevice.GD32F10x_DFP.2.0.3.pack".into(),
        archive_sha256: "0".repeat(64),
        tree_sha256: "0".repeat(64),
        pdsc: "GigaDevice/GD32F10x_DFP/2.0.3/GigaDevice.GD32F10x_DFP.pdsc".into(),
    }];
    lock.devices = vec![
        report_device("gd32badendian", "Big-endian"),
        report_device("gd32blocked", "Little-endian"),
        report_device("gd32ready", "Little-endian"),
        report_device("gd32unmapped", "Little-endian"),
    ];
    let mappings = BTreeMap::from([
        (
            "gd32blocked".into(),
            report_mapping("gd32blocked", Scope::Test, &["test-only"]),
        ),
        (
            "gd32ready".into(),
            report_mapping("gd32ready", Scope::Release, &[]),
        ),
    ]);
    (lock, mappings)
}

#[test]
fn report_assigns_exactly_one_closed_status_to_every_device() {
    let (lock, mappings) = report_fixture();
    let report = build_report(&lock, &mappings).unwrap();

    assert_eq!(report.summary.packs, 1);
    assert_eq!(report.summary.devices, 4);
    assert_eq!(report.summary.not_applicable, 1);
    assert_eq!(report.summary.blocked, 1);
    assert_eq!(report.summary.ready, 1);
    assert_eq!(report.summary.unmapped, 1);
    assert_eq!(
        report
            .devices
            .iter()
            .map(|device| (device.chip.as_str(), device.status))
            .collect::<Vec<_>>(),
        [
            ("gd32badendian", InventoryStatus::NotApplicable),
            ("gd32blocked", InventoryStatus::Blocked),
            ("gd32ready", InventoryStatus::Ready),
            ("gd32unmapped", InventoryStatus::Unmapped),
        ]
    );
}

#[test]
fn report_json_is_deterministic_and_newline_terminated() {
    let (lock, mappings) = report_fixture();
    let report = build_report(&lock, &mappings).unwrap();
    let first = render_report(&report).unwrap();
    let second = render_report(&report).unwrap();

    assert_eq!(first, second);
    assert!(first.ends_with(b"\n"));
}

#[test]
fn frozen_report_never_overwrites_drift() {
    let (lock, mappings) = report_fixture();
    let report = build_report(&lock, &mappings).unwrap();
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("inventory.json");

    write_report(&path, &report, true).unwrap();
    let original = fs::read(&path).unwrap();
    write_report(&path, &report, true).unwrap();

    let mut drifted = report;
    drifted.devices[0].reasons.push("人为漂移".into());
    let error = write_report(&path, &drifted, true).unwrap_err();
    assert!(error.to_string().contains("candidate"));
    assert_eq!(fs::read(path).unwrap(), original);
}

#[test]
fn derived_update_replaces_the_report_before_it_is_frozen_again() {
    let (lock, mappings) = report_fixture();
    let mut report = build_report(&lock, &mappings).unwrap();
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("inventory.json");
    write_report(&path, &report, false).unwrap();

    report.devices[0].reasons.push("来源已更新".into());
    write_report(&path, &report, false).unwrap();

    assert_eq!(fs::read(&path).unwrap(), render_report(&report).unwrap());
    write_report(&path, &report, true).unwrap();
}
