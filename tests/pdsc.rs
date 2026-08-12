use std::fs;
use std::path::Path;

use mcu_compat_gen::pdsc::{MemoryRegion, read_device_facts};

const FIXTURE: &str = "tests/fixtures/pdsc/mapped-device.pdsc";

fn write_pdsc(xml: &str) -> (tempfile::TempDir, std::path::PathBuf) {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fixture.pdsc");
    fs::write(&path, xml).unwrap();
    (temp, path)
}

#[test]
fn resolves_only_the_selected_device_inheritance_path() {
    let root = Path::new("tests/fixtures/pdsc");
    let facts = read_device_facts(Path::new(FIXTURE), root, "GD32F103C8").unwrap();

    assert_eq!(facts.header.as_deref(), Some("Device/Include/device.h"));
    assert_eq!(facts.svd.as_deref(), Some("SVD/mapped.svd"));
    assert_eq!(
        facts.memories,
        [
            (
                "CCMRAM".into(),
                MemoryRegion {
                    start: 268_435_456,
                    size: 4_096,
                },
            ),
            (
                "IRAM1".into(),
                MemoryRegion {
                    start: 0x2000_0000,
                    size: 0x8000,
                },
            ),
            (
                "IROM1".into(),
                MemoryRegion {
                    start: 0x0800_0000,
                    size: 0x20_000,
                },
            ),
        ]
        .into(),
    );
    assert!(!facts.memories.contains_key("OTHER"));
}

#[test]
fn rejects_missing_or_duplicate_devices() {
    let root = Path::new("tests/fixtures/pdsc");
    let missing = read_device_facts(Path::new(FIXTURE), root, "MISSING").unwrap_err();
    assert!(missing.to_string().contains("不存在"), "{missing:#}");

    let (temp, path) = write_pdsc(
        r#"<package><devices><family Dfamily="F">
          <device Dname="DUPLICATE"/><device Dname="DUPLICATE"/>
        </family></devices></package>"#,
    );
    let duplicate = read_device_facts(&path, temp.path(), "DUPLICATE").unwrap_err();
    assert!(duplicate.to_string().contains("重复"), "{duplicate:#}");
}

#[test]
fn rejects_paths_that_escape_the_pack_root() {
    let (temp, path) = write_pdsc(
        r#"<package><devices><family Dfamily="F"><device Dname="ESCAPE">
          <compile header="../outside.h"/>
        </device></family></devices></package>"#,
    );

    let error = read_device_facts(&path, temp.path(), "ESCAPE").unwrap_err();
    assert!(error.to_string().contains("相对路径"), "{error:#}");
}

#[test]
fn rejects_conflicting_memory_in_one_level() {
    let (temp, path) = write_pdsc(
        r#"<package><devices><family Dfamily="F"><device Dname="CONFLICT">
          <memory id="IRAM1" start="0x20000000" size="0x1000"/>
          <memory id="IRAM1" start="0x20000000" size="0x2000"/>
        </device></family></devices></package>"#,
    );

    let error = read_device_facts(&path, temp.path(), "CONFLICT").unwrap_err();
    assert!(
        error.to_string().contains("memory IRAM1 属性冲突"),
        "{error:#}"
    );
}
