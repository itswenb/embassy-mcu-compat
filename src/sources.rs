use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result, bail};
use roxmltree::Document;

use crate::hash::{sha256_file, sha256_tree, verify_file};
use crate::lock::{PackLock, SourceLock};
use crate::target_db::{Device, load_inventory};

pub fn update_sources(lock_path: &Path, cache_dir: &Path) -> Result<()> {
    let mut lock = SourceLock::read(lock_path)?;
    fs::create_dir_all(cache_dir)
        .with_context(|| format!("创建来源缓存 {} 失败", cache_dir.display()))?;

    let target_db = cache_dir.join("cmsis-rust-target-db");
    checkout_repository(&lock.target_db.url, &lock.target_db.revision, &target_db)?;
    let devices_path = target_db.join("data/devices.jsonl");
    let metadata_path = target_db.join("data/metadata.json");
    verify_file(&devices_path, &lock.target_db.devices_sha256)?;
    verify_file(&metadata_path, &lock.target_db.metadata_sha256)?;

    verify_cpackget_version(&lock.tools.cpackget)?;
    let pack_root = cache_dir.join("cmsis");
    fs::create_dir_all(&pack_root)
        .with_context(|| format!("创建 Pack 根目录 {} 失败", pack_root.display()))?;
    run_checked("cpackget", &cpackget_init_args(&pack_root, &lock.index.url))?;

    let index_path = pack_root.join(".Web/index.pidx");
    verify_file(&index_path, &lock.index.sha256)?;
    lock.index.timestamp = index_timestamp(&index_path)?;
    let devices = load_inventory(&target_db.join("data"), &lock.index.sha256, &lock.selectors)?;
    let pack_devices = group_packs(&devices)?;

    let mut packs = Vec::new();
    for ((vendor, name, version), device) in pack_devices {
        for component in [&vendor, &name, &version] {
            validate_pack_coordinate(component)?;
        }
        let id = pack_id(&vendor, &name, &version);
        run_checked("cpackget", &cpackget_add_args(&pack_root, &id))?;

        let archive = pack_archive_path(&pack_root, &vendor, &name, &version);
        let installed = pack_install_path(&pack_root, &vendor, &name, &version);
        let pdsc = safe_join(&installed, &device.source_pdsc)?;
        let url = pack_url(&pdsc, &version)?;
        packs.push(PackLock {
            vendor,
            name,
            version,
            url,
            archive: relative_path(&pack_root, &archive)?,
            archive_sha256: sha256_file(&archive)?,
            tree_sha256: sha256_tree(&installed)?,
            pdsc: relative_path(&pack_root, &pdsc)?,
        });
    }

    lock.packs = packs;
    lock.devices = devices;
    lock.save(lock_path)
}

pub fn cpackget_init_args(pack_root: &Path, index_url: &str) -> Vec<OsString> {
    [
        OsString::from("-C"),
        OsString::from("1"),
        OsString::from("-R"),
        pack_root.as_os_str().to_owned(),
        OsString::from("init"),
        OsString::from(index_url),
        OsString::from("--all-pdsc-files"),
    ]
    .into()
}

pub fn cpackget_add_args(pack_root: &Path, id: &str) -> Vec<OsString> {
    [
        OsString::from("-C"),
        OsString::from("1"),
        OsString::from("-R"),
        pack_root.as_os_str().to_owned(),
        OsString::from("add"),
        OsString::from(id),
    ]
    .into()
}

pub fn pack_id(vendor: &str, name: &str, version: &str) -> String {
    format!("{vendor}::{name}@{version}")
}

pub fn validate_pack_coordinate(value: &str) -> Result<()> {
    if value.is_empty()
        || matches!(value, "." | "..")
        || value.contains('/')
        || value.contains('\\')
    {
        bail!("Pack 坐标包含不安全的路径成分：{value:?}");
    }
    Ok(())
}

pub fn pack_archive_path(root: &Path, vendor: &str, name: &str, version: &str) -> PathBuf {
    root.join(".Download")
        .join(format!("{vendor}.{name}.{version}.pack"))
}

pub fn pack_install_path(root: &Path, vendor: &str, name: &str, version: &str) -> PathBuf {
    root.join(vendor).join(name).join(version)
}

pub fn index_timestamp(path: &Path) -> Result<String> {
    let xml = fs::read_to_string(path)
        .with_context(|| format!("读取 Pack Index {} 失败", path.display()))?;
    let document = Document::parse(&xml)
        .with_context(|| format!("解析 Pack Index {} 失败", path.display()))?;
    document
        .descendants()
        .find(|node| node.is_element() && node.tag_name().name() == "timestamp")
        .and_then(|node| node.text())
        .map(str::trim)
        .filter(|timestamp| !timestamp.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| anyhow::anyhow!("Pack Index {} 缺少 timestamp", path.display()))
}

pub fn pack_url(pdsc: &Path, version: &str) -> Result<String> {
    let xml =
        fs::read_to_string(pdsc).with_context(|| format!("读取 PDSC {} 失败", pdsc.display()))?;
    let document =
        Document::parse(&xml).with_context(|| format!("解析 PDSC {} 失败", pdsc.display()))?;
    let package = document
        .descendants()
        .find(|node| node.is_element() && node.tag_name().name() == "package")
        .ok_or_else(|| anyhow::anyhow!("PDSC {} 缺少 package", pdsc.display()))?;
    let vendor = child_text(package, "vendor")?;
    let name = child_text(package, "name")?;
    let base = child_text(package, "url")?;
    Ok(format!(
        "{}/{}.{}.{}.pack",
        base.trim_end_matches('/'),
        vendor,
        name,
        version
    ))
}

fn child_text(node: roxmltree::Node<'_, '_>, name: &str) -> Result<String> {
    node.children()
        .find(|child| child.is_element() && child.tag_name().name() == name)
        .and_then(|child| child.text())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| anyhow::anyhow!("package 缺少 {name}"))
}

fn group_packs(devices: &[Device]) -> Result<BTreeMap<(String, String, String), &Device>> {
    let mut packs = BTreeMap::new();
    for device in devices {
        let key = (
            device.pack_vendor.clone(),
            device.pack_name.clone(),
            device.pack_version.clone(),
        );
        if let Some(previous) = packs.insert(key.clone(), device)
            && previous.source_pdsc != device.source_pdsc
        {
            bail!("Pack {}.{}@{} 引用了多个 PDSC", key.0, key.1, key.2);
        }
    }
    Ok(packs)
}

fn verify_cpackget_version(expected: &str) -> Result<()> {
    let output = Command::new("cpackget")
        .arg("-V")
        .output()
        .context("运行 cpackget -V 失败；请安装固定版本的 CMSIS-Toolbox")?;
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    if !output.status.success() || !text.contains(expected) {
        bail!("cpackget 版本不匹配：需要 {expected}，实际输出为 {text:?}");
    }
    Ok(())
}

fn checkout_repository(url: &str, revision: &str, target: &Path) -> Result<()> {
    if !target.exists() {
        let parent = target
            .parent()
            .ok_or_else(|| anyhow::anyhow!("Git 目标路径没有父目录：{}", target.display()))?;
        fs::create_dir_all(parent).with_context(|| format!("创建 {} 失败", parent.display()))?;
        run_checked(
            "git",
            &[
                OsString::from("clone"),
                OsString::from("--no-checkout"),
                OsString::from(url),
                target.as_os_str().to_owned(),
            ],
        )?;
    }
    run_checked(
        "git",
        &[
            OsString::from("-C"),
            target.as_os_str().to_owned(),
            OsString::from("fetch"),
            OsString::from("origin"),
            OsString::from(revision),
        ],
    )?;
    run_checked(
        "git",
        &[
            OsString::from("-C"),
            target.as_os_str().to_owned(),
            OsString::from("checkout"),
            OsString::from("--detach"),
            OsString::from(revision),
        ],
    )
}

fn run_checked(program: &str, args: &[OsString]) -> Result<()> {
    let output = Command::new(program)
        .args(args)
        .output()
        .with_context(|| format!("运行 {program} 失败"))?;
    if !output.status.success() {
        bail!(
            "命令 {program} 失败（状态 {}）：{}{}",
            output.status,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(())
}

fn safe_join(root: &Path, relative: &str) -> Result<PathBuf> {
    let path = Path::new(relative);
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        bail!("PDSC 路径不是规范相对路径：{relative:?}");
    }
    Ok(root.join(path))
}

fn relative_path(root: &Path, path: &Path) -> Result<String> {
    let relative = path
        .strip_prefix(root)
        .with_context(|| format!("{} 不在 {} 内", path.display(), root.display()))?;
    let mut components = Vec::new();
    for component in relative.components() {
        let Component::Normal(component) = component else {
            bail!("路径不是规范相对路径：{}", relative.display());
        };
        components.push(
            component
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("路径不是 UTF-8：{}", relative.display()))?,
        );
    }
    Ok(components.join("/"))
}
