use std::fs;

use mcu_compat_gen::hash::{sha256_file, sha256_tree, verify_file};
use mcu_compat_gen::lock::SourceLock;

const FIXTURE: &str = "tests/fixtures/source-lock.toml";

#[test]
fn source_lock_round_trip_is_stable() {
    let lock = SourceLock::read(FIXTURE).unwrap();
    let first = lock.to_toml().unwrap();
    let parsed = SourceLock::parse(&first).unwrap();
    let second = parsed.to_toml().unwrap();

    assert_eq!(first, second);
    assert_eq!(lock, parsed);
    assert!(first.ends_with('\n'));
    assert!(!first.ends_with("\n\n"));
}

#[test]
fn source_lock_rejects_target_db_schema_and_index_drift() {
    let mut lock = SourceLock::read(FIXTURE).unwrap();
    lock.target_db.schema = 2;
    assert!(lock.validate().unwrap_err().to_string().contains("schema"));

    lock.target_db.schema = 1;
    lock.target_db.source_index_sha256 = "other-index".into();
    assert!(
        lock.validate()
            .unwrap_err()
            .to_string()
            .contains("索引 SHA-256")
    );
}

#[test]
fn file_verification_reports_path_and_both_hashes() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("input.bin");
    fs::write(&path, b"actual").unwrap();
    let actual = sha256_file(&path).unwrap();

    let error = verify_file(&path, "expected").unwrap_err().to_string();
    assert!(error.contains(&path.display().to_string()));
    assert!(error.contains("expected"));
    assert!(error.contains(&actual));
}

#[test]
fn tree_hash_is_deterministic_and_includes_paths() {
    let first = tempfile::tempdir().unwrap();
    let second = tempfile::tempdir().unwrap();
    for root in [first.path(), second.path()] {
        fs::create_dir(root.join("nested")).unwrap();
        fs::write(root.join("a"), b"one").unwrap();
        fs::write(root.join("nested/b"), b"two").unwrap();
    }
    assert_eq!(
        sha256_tree(first.path()).unwrap(),
        sha256_tree(second.path()).unwrap()
    );

    fs::rename(second.path().join("a"), second.path().join("renamed")).unwrap();
    assert_ne!(
        sha256_tree(first.path()).unwrap(),
        sha256_tree(second.path()).unwrap()
    );
}

#[test]
fn atomic_save_refuses_an_invalid_lock() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("sources.lock.toml");
    let mut lock = SourceLock::read(FIXTURE).unwrap();
    lock.schema = 2;

    assert!(lock.save(&path).is_err());
    assert!(!path.exists());
}

#[test]
fn checked_in_source_lock_is_valid() {
    SourceLock::read("sources.lock.toml").unwrap();
}

#[test]
fn checked_in_source_lock_uses_canonical_serialization() {
    let actual = fs::read_to_string("sources.lock.toml").unwrap();
    let expected = SourceLock::read("sources.lock.toml")
        .unwrap()
        .to_toml()
        .unwrap();
    let difference = actual
        .lines()
        .zip(expected.lines())
        .enumerate()
        .find(|(_, (actual, expected))| actual != expected);

    assert!(
        difference.is_none(),
        "首个差异：{:?}；实际/规范行数：{}/{}",
        difference.map(|(index, lines)| (index + 1, lines)),
        actual.lines().count(),
        expected.lines().count()
    );
    assert_eq!(actual.len(), expected.len());
}
