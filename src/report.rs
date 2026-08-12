use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Write;
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

use crate::lock::SourceLock;
use crate::mapping::{CheckedMapping, Scope, load_mappings};
use crate::sources::verify_sources;
use crate::target_db::{Device, Processor};

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct InventoryReport {
    pub schema: u32,
    pub source_index_sha256: String,
    pub target_db_revision: String,
    pub summary: InventorySummary,
    pub devices: Vec<InventoryDevice>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
pub struct InventorySummary {
    pub packs: usize,
    pub devices: usize,
    pub ready: usize,
    pub blocked: usize,
    pub unmapped: usize,
    pub not_applicable: usize,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct InventoryDevice {
    pub chip: String,
    pub original_name: String,
    pub pack: String,
    pub processors: Vec<Processor>,
    pub status: InventoryStatus,
    pub alias: Option<String>,
    pub scope: Option<Scope>,
    pub reasons: Vec<String>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InventoryStatus {
    Ready,
    Blocked,
    Unmapped,
    NotApplicable,
}

pub fn run_audit(
    lock_path: &Path,
    cache_dir: &Path,
    compat_dir: &Path,
    output: &Path,
    frozen: bool,
) -> Result<InventorySummary> {
    if !frozen {
        bail!("审计必须使用 --frozen，来源更新只能通过 sources update 完成");
    }
    let lock = SourceLock::read(lock_path)?;
    verify_sources(&lock, cache_dir)?;

    let project_root = lock_path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let pack_root = cache_dir.join("cmsis");
    let mappings = load_mappings(compat_dir, &lock, &[project_root, &pack_root])?;
    let report = build_report(&lock, &mappings)?;
    write_report(output, &report, true)?;
    Ok(report.summary)
}

pub fn build_report(
    lock: &SourceLock,
    mappings: &BTreeMap<String, CheckedMapping>,
) -> Result<InventoryReport> {
    lock.validate()?;
    let chips: BTreeSet<_> = lock
        .devices
        .iter()
        .map(|device| device.chip.as_str())
        .collect();
    for (chip, mapping) in mappings {
        if chip != &mapping.mapping.chip {
            bail!(
                "映射索引键 {chip} 与映射型号 {} 不一致",
                mapping.mapping.chip
            );
        }
        if !chips.contains(chip.as_str()) {
            bail!("映射 {chip} 不在来源锁器件闭包中");
        }
    }

    let mut devices = lock.devices.clone();
    devices.sort_by(|left, right| left.chip.cmp(&right.chip));
    let devices: Vec<_> = devices
        .iter()
        .map(|device| report_device(device, mappings.get(&device.chip)))
        .collect();
    let summary = summarize(lock.packs.len(), &devices);

    Ok(InventoryReport {
        schema: 1,
        source_index_sha256: lock.index.sha256.clone(),
        target_db_revision: lock.target_db.revision.clone(),
        summary,
        devices,
    })
}

pub fn render_report(report: &InventoryReport) -> Result<Vec<u8>> {
    let mut bytes = serde_json::to_vec_pretty(report).context("编码审计报告 JSON 失败")?;
    bytes.push(b'\n');
    Ok(bytes)
}

pub fn write_report(path: &Path, report: &InventoryReport, frozen: bool) -> Result<()> {
    let bytes = render_report(report)?;
    if frozen && path.exists() {
        let current =
            fs::read(path).with_context(|| format!("读取现有报告 {} 失败", path.display()))?;
        if current == bytes {
            return Ok(());
        }
        let parent = output_parent(path);
        let mut candidate = tempfile::Builder::new()
            .prefix(".inventory.candidate-")
            .tempfile_in(parent)
            .with_context(|| format!("在 {} 创建候选报告失败", parent.display()))?;
        candidate
            .write_all(&bytes)
            .context("写入 candidate 报告失败")?;
        candidate
            .as_file()
            .sync_all()
            .context("同步 candidate 报告失败")?;
        let (_, candidate_path) = candidate.keep().context("保留 candidate 报告失败")?;
        bail!(
            "冻结审计报告 {} 发生漂移；candidate 已写入 {}",
            path.display(),
            candidate_path.display()
        );
    }

    let parent = output_parent(path);
    fs::create_dir_all(parent)
        .with_context(|| format!("创建报告目录 {} 失败", parent.display()))?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)
        .with_context(|| format!("在 {} 创建临时报告失败", parent.display()))?;
    temporary
        .write_all(&bytes)
        .with_context(|| format!("写入临时报告 {} 失败", path.display()))?;
    temporary
        .as_file()
        .sync_all()
        .with_context(|| format!("同步临时报告 {} 失败", path.display()))?;
    temporary
        .persist(path)
        .map_err(|error| error.error)
        .with_context(|| format!("原子替换报告 {} 失败", path.display()))?;
    Ok(())
}

fn report_device(device: &Device, mapping: Option<&CheckedMapping>) -> InventoryDevice {
    let pack = format!(
        "{}.{}@{}",
        device.pack_vendor, device.pack_name, device.pack_version
    );
    let not_applicable = not_applicable_reasons(device);
    let (status, alias, scope, reasons) = if !not_applicable.is_empty() {
        (InventoryStatus::NotApplicable, None, None, not_applicable)
    } else if let Some(mapping) = mapping {
        let mut reasons = mapping.audit.blockers.clone();
        if mapping.mapping.scope == Scope::Test
            && !reasons.iter().any(|reason| reason.contains("test-only"))
        {
            reasons.push("test-only 映射不能进入发布生成物".to_owned());
        }
        let status = if mapping.ready() {
            InventoryStatus::Ready
        } else {
            InventoryStatus::Blocked
        };
        (
            status,
            Some(mapping.mapping.alias.clone()),
            Some(mapping.mapping.scope),
            reasons,
        )
    } else {
        (
            InventoryStatus::Unmapped,
            None,
            None,
            vec!["未声明兼容映射".to_owned()],
        )
    };

    InventoryDevice {
        chip: device.chip.clone(),
        original_name: device.original_name.clone(),
        pack,
        processors: device.processors.clone(),
        status,
        alias,
        scope,
        reasons,
    }
}

fn not_applicable_reasons(device: &Device) -> Vec<String> {
    let mut reasons = Vec::new();
    if device.processors.len() != 1 {
        reasons.push(format!(
            "当前兼容 schema 不支持 {} 个处理器的器件",
            device.processors.len()
        ));
        return reasons;
    }
    let processor = &device.processors[0];
    if processor.rust_target.is_none() {
        reasons.push("无法解析 Rust target".to_owned());
    }
    if processor.endian.as_deref() != Some("Little-endian") {
        reasons.push(format!("不支持的字节序：{:?}", processor.endian));
    }
    reasons
}

fn summarize(packs: usize, devices: &[InventoryDevice]) -> InventorySummary {
    let mut summary = InventorySummary {
        packs,
        devices: devices.len(),
        ready: 0,
        blocked: 0,
        unmapped: 0,
        not_applicable: 0,
    };
    for device in devices {
        match device.status {
            InventoryStatus::Ready => summary.ready += 1,
            InventoryStatus::Blocked => summary.blocked += 1,
            InventoryStatus::Unmapped => summary.unmapped += 1,
            InventoryStatus::NotApplicable => summary.not_applicable += 1,
        }
    }
    summary
}

fn output_parent(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}
