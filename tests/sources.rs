use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use mcu_compat_gen::hash::{sha256_file, sha256_tree};
use mcu_compat_gen::lock::{PackLock, SourceLock};
use mcu_compat_gen::sources::{
    cpackget_add_args, cpackget_init_args, index_timestamp, pack_archive_path, pack_id,
    pack_install_path, pack_url, validate_pack_coordinate, verify_sources,
};
use mcu_compat_gen::target_db::{Selector, load_inventory};

#[test]
fn cpackget_commands_pin_concurrency_and_pack_root() {
    let root = Path::new("cache/cmsis");
    assert_eq!(
        cpackget_init_args(root, "https://www.keil.com/pack/index.pidx"),
        [
            OsString::from("-C"),
            OsString::from("1"),
            OsString::from("-R"),
            root.as_os_str().to_owned(),
            OsString::from("init"),
            OsString::from("https://www.keil.com/pack/index.pidx"),
            OsString::from("--all-pdsc-files"),
        ]
    );
    assert_eq!(
        cpackget_add_args(root, "GigaDevice::GD32F10x_DFP@2.0.3"),
        [
            OsString::from("-C"),
            OsString::from("1"),
            OsString::from("-R"),
            root.as_os_str().to_owned(),
            OsString::from("add"),
            OsString::from("GigaDevice::GD32F10x_DFP@2.0.3"),
        ]
    );
}

#[test]
fn pack_paths_follow_the_cmsis_pack_layout() {
    let root = Path::new("cache/cmsis");
    assert_eq!(
        pack_id("GigaDevice", "GD32F10x_DFP", "2.0.3"),
        "GigaDevice::GD32F10x_DFP@2.0.3"
    );
    assert_eq!(
        pack_archive_path(root, "GigaDevice", "GD32F10x_DFP", "2.0.3"),
        PathBuf::from("cache/cmsis/.Download/GigaDevice.GD32F10x_DFP.2.0.3.pack")
    );
    assert_eq!(
        pack_install_path(root, "GigaDevice", "GD32F10x_DFP", "2.0.3"),
        PathBuf::from("cache/cmsis/GigaDevice/GD32F10x_DFP/2.0.3")
    );
}

#[test]
fn reads_the_pack_index_timestamp() {
    assert_eq!(
        index_timestamp(Path::new("tests/fixtures/index.pidx")).unwrap(),
        "2026-08-08T04:00:41.4855875+00:00"
    );
}

#[test]
fn builds_the_archive_url_from_the_pdsc() {
    assert_eq!(
        pack_url(
            Path::new("tests/fixtures/pack/GigaDevice.GD32F10x_DFP.pdsc"),
            "2.0.3"
        )
        .unwrap(),
        "https://gd32mcu.com/data/documents/pack/GigaDevice.GD32F10x_DFP.2.0.3.pack"
    );
}

#[test]
fn rejects_pack_coordinates_that_can_escape_the_pack_root() {
    for value in ["", ".", "..", "../outside", "vendor/name", r"vendor\name"] {
        assert!(validate_pack_coordinate(value).is_err(), "{value:?}");
    }
    assert!(validate_pack_coordinate("GD32F10x_DFP").is_ok());
    assert!(validate_pack_coordinate("2.0.3-rc.1+build").is_ok());
}

#[test]
fn frozen_sources_are_verified_before_audit() {
    let temp = tempfile::tempdir().unwrap();
    let cache = temp.path();
    let data = cache.join("cmsis-rust-target-db/data");
    let pack_root = cache.join("cmsis");
    let index = pack_root.join(".Web/index.pidx");
    let installed = pack_install_path(&pack_root, "GigaDevice", "GD32F10x_DFP", "2.0.3");
    let pdsc = installed.join("GigaDevice.GD32F10x_DFP.pdsc");
    let archive = pack_archive_path(&pack_root, "GigaDevice", "GD32F10x_DFP", "2.0.3");

    fs::create_dir_all(&data).unwrap();
    fs::create_dir_all(index.parent().unwrap()).unwrap();
    fs::create_dir_all(&installed).unwrap();
    fs::create_dir_all(archive.parent().unwrap()).unwrap();
    fs::copy(
        "tests/fixtures/target-db/devices.jsonl",
        data.join("devices.jsonl"),
    )
    .unwrap();
    fs::copy("tests/fixtures/index.pidx", &index).unwrap();
    fs::copy("tests/fixtures/pack/GigaDevice.GD32F10x_DFP.pdsc", &pdsc).unwrap();
    fs::write(&archive, b"fixture pack").unwrap();

    let index_sha256 = sha256_file(&index).unwrap();
    fs::write(
        data.join("metadata.json"),
        format!("{{\"schema_version\":1,\"source_index_sha256\":\"{index_sha256}\"}}\n"),
    )
    .unwrap();
    let selectors = vec![Selector {
        vendor: "GigaDevice".into(),
        pack_pattern: "GD32F10x_DFP".into(),
    }];

    let mut lock = SourceLock::read("tests/fixtures/source-lock.toml").unwrap();
    lock.index.sha256 = index_sha256.clone();
    lock.index.timestamp = index_timestamp(&index).unwrap();
    lock.target_db.source_index_sha256 = index_sha256.clone();
    lock.target_db.devices_sha256 = sha256_file(&data.join("devices.jsonl")).unwrap();
    lock.target_db.metadata_sha256 = sha256_file(&data.join("metadata.json")).unwrap();
    lock.selectors = selectors;
    lock.devices = load_inventory(&data, &index_sha256, &lock.selectors).unwrap();
    lock.packs = vec![PackLock {
        vendor: "GigaDevice".into(),
        name: "GD32F10x_DFP".into(),
        version: "2.0.3".into(),
        url: "https://gd32mcu.com/data/documents/pack/GigaDevice.GD32F10x_DFP.2.0.3.pack".into(),
        archive: ".Download/GigaDevice.GD32F10x_DFP.2.0.3.pack".into(),
        archive_sha256: sha256_file(&archive).unwrap(),
        tree_sha256: sha256_tree(&installed).unwrap(),
        pdsc: "GigaDevice/GD32F10x_DFP/2.0.3/GigaDevice.GD32F10x_DFP.pdsc".into(),
    }];

    verify_sources(&lock, cache).unwrap();
    fs::write(&archive, b"drifted pack").unwrap();
    let error = verify_sources(&lock, cache).unwrap_err();
    assert!(error.to_string().contains("SHA-256"), "{error:#}");
}
