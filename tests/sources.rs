use std::ffi::OsString;
use std::path::{Path, PathBuf};

use mcu_compat_gen::sources::{
    cpackget_add_args, cpackget_init_args, index_timestamp, pack_archive_path, pack_id,
    pack_install_path, pack_url, validate_pack_coordinate,
};

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
