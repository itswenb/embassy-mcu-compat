use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Component, Path};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use walkdir::WalkDir;

use crate::hash::verify_file;
use crate::lock::SourceLock;
use crate::pdsc::read_device_facts;
use crate::sources::pack_install_path;

pub const REQUIRED_EVIDENCE: [&str; 11] = [
    "cpu",
    "memory",
    "interrupts",
    "registers",
    "rcc",
    "flash",
    "pins",
    "dma",
    "alias_cfg",
    "license",
    "hardware",
];

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct Mapping {
    pub schema: u32,
    pub chip: String,
    pub alias: String,
    pub rust_target: String,
    pub scope: Scope,
    pub source: MappingSource,
    #[serde(default)]
    pub names: Names,
    #[serde(default)]
    pub evidence: BTreeMap<String, Evidence>,
    #[serde(default)]
    pub patch: Value,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Scope {
    Test,
    Release,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct MappingSource {
    pub pack: String,
    pub device: String,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
pub struct Names {
    #[serde(default)]
    pub peripherals: BTreeMap<String, String>,
    #[serde(default)]
    pub interrupts: BTreeMap<String, String>,
    #[serde(default)]
    pub signals: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct Evidence {
    pub path: String,
    pub sha256: String,
    pub locator: String,
    pub result: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MappingAudit {
    pub blockers: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckedMapping {
    pub mapping: Mapping,
    pub audit: MappingAudit,
}

impl Mapping {
    pub fn read(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let contents =
            fs::read(path).with_context(|| format!("读取兼容映射 {} 失败", path.display()))?;
        serde_json::from_slice(&contents)
            .with_context(|| format!("解析兼容映射 {} 失败", path.display()))
    }
}

impl MappingAudit {
    pub fn ready(&self) -> bool {
        self.blockers.is_empty()
    }
}

impl CheckedMapping {
    pub fn ready(&self) -> bool {
        self.mapping.scope == Scope::Release && self.audit.ready()
    }
}

pub fn load_mappings(
    directory: &Path,
    lock: &SourceLock,
    evidence_roots: &[&Path],
) -> Result<BTreeMap<String, CheckedMapping>> {
    let mut paths = Vec::new();
    for entry in WalkDir::new(directory).follow_links(false) {
        let entry =
            entry.with_context(|| format!("遍历兼容映射目录 {} 失败", directory.display()))?;
        if entry.file_type().is_symlink() {
            bail!("兼容映射目录不接受符号链接：{}", entry.path().display());
        }
        if entry.file_type().is_file()
            && entry
                .path()
                .extension()
                .and_then(|extension| extension.to_str())
                .is_some_and(|extension| extension.eq_ignore_ascii_case("json"))
        {
            paths.push(entry.into_path());
        }
    }
    paths.sort();

    let mut mappings = BTreeMap::new();
    for path in paths {
        let stem = path
            .file_stem()
            .and_then(|stem| stem.to_str())
            .ok_or_else(|| anyhow::anyhow!("映射文件名不是 UTF-8：{}", path.display()))?;
        let mapping = Mapping::read(&path)?;
        let mut audit = audit_mapping(&mapping, stem, lock, evidence_roots)?;
        audit_pdsc_facts(&mapping, lock, evidence_roots, &mut audit.blockers)?;
        let chip = mapping.chip.clone();
        if mappings
            .insert(chip.clone(), CheckedMapping { mapping, audit })
            .is_some()
        {
            bail!("兼容映射目录包含重复型号：{chip}");
        }
    }
    Ok(mappings)
}

fn audit_pdsc_facts(
    mapping: &Mapping,
    lock: &SourceLock,
    roots: &[&Path],
    blockers: &mut Vec<String>,
) -> Result<()> {
    let device = lock
        .devices
        .iter()
        .find(|device| device.chip == mapping.chip)
        .ok_or_else(|| anyhow::anyhow!("来源锁中不存在器件 {}", mapping.chip))?;
    let pack = lock
        .packs
        .iter()
        .find(|pack| {
            pack.vendor == device.pack_vendor
                && pack.name == device.pack_name
                && pack.version == device.pack_version
        })
        .ok_or_else(|| anyhow::anyhow!("来源锁中不存在映射 {} 的 Pack", mapping.chip))?;
    let matches: Vec<_> = roots
        .iter()
        .filter(|root| root.join(&pack.pdsc).is_file())
        .collect();
    let root = match matches.as_slice() {
        [] => bail!("映射 {} 的锁定 PDSC 不存在：{}", mapping.chip, pack.pdsc),
        [root] => **root,
        _ => bail!("映射 {} 的锁定 PDSC 在多个根中存在", mapping.chip),
    };
    let installed = pack_install_path(root, &pack.vendor, &pack.name, &pack.version);
    let facts = read_device_facts(&root.join(&pack.pdsc), &installed, &mapping.source.device)?;

    if mapping.scope == Scope::Test {
        return Ok(());
    }
    if facts.memories.is_empty() {
        blockers.push("PDSC 未提供 memory 事实".to_owned());
    }
    if mapping
        .evidence
        .get("memory")
        .map(|evidence| evidence.path.as_str())
        != Some(pack.pdsc.as_str())
    {
        blockers.push(format!("memory 证据必须引用锁定 PDSC：{}", pack.pdsc));
    }

    let prefix = format!("{}/{}/{}/", pack.vendor, pack.name, pack.version);
    for (label, path) in [("header", facts.header), ("SVD", facts.svd)] {
        let Some(path) = path else {
            blockers.push(format!("PDSC 未解析到 {label} 路径"));
            continue;
        };
        let relative = format!("{prefix}{path}");
        if !mapping
            .evidence
            .values()
            .any(|evidence| evidence.path == relative)
        {
            blockers.push(format!("没有证据引用 PDSC 解析的 {label}：{relative}"));
        }
    }
    Ok(())
}

pub fn audit_mapping(
    mapping: &Mapping,
    filename_stem: &str,
    lock: &SourceLock,
    evidence_roots: &[&Path],
) -> Result<MappingAudit> {
    validate_mapping_reference(mapping, filename_stem, lock)?;
    let allowed: BTreeSet<_> = REQUIRED_EVIDENCE.into_iter().collect();
    for category in mapping.evidence.keys() {
        if !allowed.contains(category.as_str()) {
            bail!("映射 {} 包含未知证据类别 {category:?}", mapping.chip);
        }
    }

    let mut blockers = Vec::new();
    if mapping.scope == Scope::Test {
        blockers.push("test-only 映射不能进入发布生成物".to_owned());
    } else {
        for category in REQUIRED_EVIDENCE {
            match mapping.evidence.get(category) {
                Some(evidence) => {
                    validate_evidence_shape(category, evidence)?;
                    audit_evidence_file(category, evidence, evidence_roots, &mut blockers)?;
                }
                None => blockers.push(format!("缺少 {category} 证据")),
            }
        }
    }

    Ok(MappingAudit { blockers })
}

fn validate_mapping_reference(
    mapping: &Mapping,
    filename_stem: &str,
    lock: &SourceLock,
) -> Result<()> {
    if mapping.schema != 1 {
        bail!(
            "映射 {} 使用不支持的 schema {}",
            mapping.chip,
            mapping.schema
        );
    }
    if !is_canonical_name(&mapping.chip) {
        bail!("真实型号不是规范小写名称：{:?}", mapping.chip);
    }
    if mapping.chip != filename_stem {
        bail!(
            "映射型号 {} 与文件名 {} 不一致",
            mapping.chip,
            filename_stem
        );
    }
    if !mapping.alias.starts_with("stm32") || !is_canonical_name(&mapping.alias) {
        bail!("alias 不是规范 STM32 型号：{:?}", mapping.alias);
    }

    let device = lock
        .devices
        .iter()
        .find(|device| device.chip == mapping.chip)
        .ok_or_else(|| anyhow::anyhow!("来源锁中不存在器件 {}", mapping.chip))?;
    let expected_pack = format!(
        "{}.{}@{}",
        device.pack_vendor, device.pack_name, device.pack_version
    );
    if mapping.source.pack != expected_pack {
        bail!(
            "映射 {} 的 Pack 不匹配：期望 {}，实际 {}",
            mapping.chip,
            expected_pack,
            mapping.source.pack
        );
    }
    if mapping.source.device != device.original_name {
        bail!(
            "映射 {} 的来源器件不匹配：期望 {}，实际 {}",
            mapping.chip,
            device.original_name,
            mapping.source.device
        );
    }

    let targets: BTreeSet<_> = device
        .processors
        .iter()
        .map(|processor| processor.rust_target.as_deref())
        .collect();
    if targets.len() != 1 || !targets.contains(&Some(mapping.rust_target.as_str())) {
        bail!(
            "映射 {} 的 Rust target {} 与来源事实 {:?} 不一致",
            mapping.chip,
            mapping.rust_target,
            targets
        );
    }
    Ok(())
}

fn validate_evidence_shape(category: &str, evidence: &Evidence) -> Result<()> {
    validate_relative_path(&evidence.path)?;
    if evidence.sha256.len() != 64
        || !evidence
            .sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        bail!("{category} 证据的 SHA-256 不是 64 位小写十六进制");
    }
    Ok(())
}

fn audit_evidence_file(
    category: &str,
    evidence: &Evidence,
    roots: &[&Path],
    blockers: &mut Vec<String>,
) -> Result<()> {
    if evidence.locator.trim().is_empty() {
        blockers.push(format!("{category} 证据缺少定位"));
    }
    if evidence.result.trim().is_empty() {
        blockers.push(format!("{category} 证据缺少结论"));
    }

    let matches: Vec<_> = roots
        .iter()
        .map(|root| root.join(&evidence.path))
        .filter(|path| path.is_file())
        .collect();
    match matches.as_slice() {
        [] => blockers.push(format!("{category} 证据文件不存在：{}", evidence.path)),
        [path] => {
            if let Err(error) = verify_file(path, &evidence.sha256) {
                blockers.push(format!("{category} 证据校验失败：{error:#}"));
            }
        }
        _ => blockers.push(format!(
            "{category} 证据路径在多个根中存在：{}",
            evidence.path
        )),
    }
    Ok(())
}

fn validate_relative_path(value: &str) -> Result<()> {
    let path = Path::new(value);
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        bail!("证据路径必须是规范相对路径：{value:?}");
    }
    Ok(())
}

fn is_canonical_name(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}
