use std::collections::BTreeMap;
use std::fs;

use mcu_compat_gen::hash::sha256_file;
use mcu_compat_gen::lock::{PackLock, SourceLock};
use mcu_compat_gen::mapping::{
    Evidence, Mapping, REQUIRED_EVIDENCE, Scope, audit_mapping, load_mappings,
};
use mcu_compat_gen::merge_patch::apply_merge_patch;
use mcu_compat_gen::target_db::{Device, Processor};
use serde_json::json;

fn source_lock() -> SourceLock {
    let mut lock = SourceLock::read("tests/fixtures/source-lock.toml").unwrap();
    lock.devices = vec![Device {
        chip: "gd32f103c8".into(),
        original_name: "GD32F103C8".into(),
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
            endian: Some("Little-endian".into()),
            rust_target: Some("thumbv7m-none-eabi".into()),
        }],
    }];
    lock
}

fn test_mapping() -> Mapping {
    Mapping::read("compat/gigadevice/gd32f103c8.json").unwrap()
}

#[test]
fn merge_patch_follows_rfc_7396() {
    let mut target = json!({
        "object": {"kept": 1, "changed": 2, "removed": 3},
        "array": [1, 2],
        "scalar": "old"
    });
    let patch = json!({
        "object": {"changed": 20, "removed": null, "added": 4},
        "array": [9],
        "scalar": {"nested": true}
    });

    apply_merge_patch(&mut target, &patch);

    assert_eq!(
        target,
        json!({
            "object": {"kept": 1, "changed": 20, "added": 4},
            "array": [9],
            "scalar": {"nested": true}
        })
    );
}

#[test]
fn merge_patch_replaces_non_object_targets_before_merging() {
    let mut target = json!([1, 2]);
    apply_merge_patch(&mut target, &json!({"created": true}));
    assert_eq!(target, json!({"created": true}));
}

#[test]
fn test_mapping_is_valid_but_never_release_ready() {
    let mapping = test_mapping();
    let audit = audit_mapping(
        &mapping,
        "gd32f103c8",
        &source_lock(),
        &[std::path::Path::new(".")],
    )
    .unwrap();

    assert_eq!(mapping.scope, Scope::Test);
    assert!(!audit.ready());
    assert!(
        audit
            .blockers
            .iter()
            .any(|reason| reason.contains("test-only"))
    );
}

#[test]
fn mapping_rejects_filename_alias_source_and_target_mismatches() {
    let lock = source_lock();
    let roots = [std::path::Path::new(".")];

    let mut mapping = test_mapping();
    assert!(audit_mapping(&mapping, "other", &lock, &roots).is_err());

    mapping.alias = "STM32F103C8".into();
    assert!(audit_mapping(&mapping, "gd32f103c8", &lock, &roots).is_err());

    mapping.alias = "stm32f103c8".into();
    mapping.source.device = "GD32F103CB".into();
    assert!(audit_mapping(&mapping, "gd32f103c8", &lock, &roots).is_err());

    mapping.source.device = "GD32F103C8".into();
    mapping.rust_target = "thumbv7em-none-eabi".into();
    assert!(audit_mapping(&mapping, "gd32f103c8", &lock, &roots).is_err());
}

#[test]
fn release_mapping_lists_every_missing_evidence_gate() {
    let mut mapping = test_mapping();
    mapping.scope = Scope::Release;
    let audit = audit_mapping(
        &mapping,
        "gd32f103c8",
        &source_lock(),
        &[std::path::Path::new(".")],
    )
    .unwrap();

    assert!(!audit.ready());
    for category in REQUIRED_EVIDENCE {
        assert!(
            audit
                .blockers
                .iter()
                .any(|reason| reason.contains(category)),
            "缺少 {category} blocker：{:?}",
            audit.blockers
        );
    }
}

#[test]
fn release_mapping_is_ready_only_when_all_evidence_files_match() {
    let root = tempfile::tempdir().unwrap();
    fs::write(root.path().join("proof.txt"), "已人工核验\n").unwrap();
    let sha256 = sha256_file(&root.path().join("proof.txt")).unwrap();
    let evidence = Evidence {
        path: "proof.txt".into(),
        sha256,
        locator: "第 1 行".into(),
        result: "通过".into(),
    };
    let mut mapping = test_mapping();
    mapping.scope = Scope::Release;
    mapping.evidence = REQUIRED_EVIDENCE
        .iter()
        .map(|category| ((*category).to_owned(), evidence.clone()))
        .collect::<BTreeMap<_, _>>();

    let audit = audit_mapping(&mapping, "gd32f103c8", &source_lock(), &[root.path()]).unwrap();
    assert!(audit.ready(), "{:?}", audit.blockers);

    fs::write(root.path().join("proof.txt"), "发生漂移\n").unwrap();
    let drifted = audit_mapping(&mapping, "gd32f103c8", &source_lock(), &[root.path()]).unwrap();
    assert!(!drifted.ready());
    assert!(
        drifted
            .blockers
            .iter()
            .any(|reason| reason.contains("SHA-256"))
    );
}

#[test]
fn evidence_paths_cannot_escape_an_evidence_root() {
    let mut mapping = test_mapping();
    mapping.scope = Scope::Release;
    mapping.evidence.insert(
        "cpu".into(),
        Evidence {
            path: "../outside".into(),
            sha256: "0".repeat(64),
            locator: "任意".into(),
            result: "通过".into(),
        },
    );

    assert!(
        audit_mapping(
            &mapping,
            "gd32f103c8",
            &source_lock(),
            &[std::path::Path::new(".")]
        )
        .is_err()
    );
}

#[test]
fn loaded_release_mapping_must_reference_pdsc_header_and_svd_facts() {
    let temp = tempfile::tempdir().unwrap();
    let compat = temp.path().join("compat");
    let cmsis = temp.path().join("cmsis");
    let install = cmsis.join("GigaDevice/GD32F10x_DFP/2.0.3");
    let pdsc_relative = "GigaDevice/GD32F10x_DFP/2.0.3/GigaDevice.GD32F10x_DFP.pdsc";
    let header_relative = "GigaDevice/GD32F10x_DFP/2.0.3/Device/Include/device.h";
    let svd_relative = "GigaDevice/GD32F10x_DFP/2.0.3/SVD/mapped.svd";
    fs::create_dir_all(install.join("Device/Include")).unwrap();
    fs::create_dir_all(install.join("SVD")).unwrap();
    fs::create_dir_all(&compat).unwrap();
    fs::copy(
        "tests/fixtures/pdsc/mapped-device.pdsc",
        cmsis.join(pdsc_relative),
    )
    .unwrap();
    fs::write(cmsis.join(header_relative), "fixture header\n").unwrap();
    fs::write(cmsis.join(svd_relative), "fixture svd\n").unwrap();

    let evidence = |path: &str| Evidence {
        path: path.into(),
        sha256: sha256_file(&cmsis.join(path)).unwrap(),
        locator: "fixture".into(),
        result: "通过".into(),
    };
    let mut mapping = test_mapping();
    mapping.scope = Scope::Release;
    mapping.evidence = REQUIRED_EVIDENCE
        .iter()
        .map(|category| ((*category).to_owned(), evidence(pdsc_relative)))
        .collect();
    let mapping_path = compat.join("gd32f103c8.json");
    fs::write(&mapping_path, serde_json::to_vec_pretty(&mapping).unwrap()).unwrap();

    let mut lock = source_lock();
    lock.packs = vec![PackLock {
        vendor: "GigaDevice".into(),
        name: "GD32F10x_DFP".into(),
        version: "2.0.3".into(),
        url: "https://example.invalid/pack".into(),
        archive: ".Download/GigaDevice.GD32F10x_DFP.2.0.3.pack".into(),
        archive_sha256: "0".repeat(64),
        tree_sha256: "0".repeat(64),
        pdsc: pdsc_relative.into(),
    }];

    let blocked = load_mappings(&compat, &lock, &[cmsis.as_path()]).unwrap();
    assert!(
        blocked["gd32f103c8"]
            .audit
            .blockers
            .iter()
            .any(|reason| reason.contains("header"))
    );
    assert!(
        blocked["gd32f103c8"]
            .audit
            .blockers
            .iter()
            .any(|reason| reason.contains("SVD"))
    );

    mapping
        .evidence
        .insert("cpu".into(), evidence(header_relative));
    mapping
        .evidence
        .insert("registers".into(), evidence(svd_relative));
    fs::write(mapping_path, serde_json::to_vec_pretty(&mapping).unwrap()).unwrap();
    let ready = load_mappings(&compat, &lock, &[cmsis.as_path()]).unwrap();
    assert!(ready["gd32f103c8"].ready());
}
