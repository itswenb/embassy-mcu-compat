use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct Selector {
    pub vendor: String,
    pub pack_pattern: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct Processor {
    pub name: Option<String>,
    pub core: Option<String>,
    pub fpu: Option<String>,
    pub endian: Option<String>,
    pub rust_target: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct Device {
    pub chip: String,
    pub original_name: String,
    pub device_kind: String,
    pub parent_device: Option<String>,
    pub pack_vendor: String,
    pub pack_name: String,
    pub pack_version: String,
    pub source_pdsc: String,
    pub processors: Vec<Processor>,
}

#[derive(Debug, Deserialize)]
struct Metadata {
    schema_version: u32,
    source_index_sha256: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Record {
    device: String,
    device_kind: String,
    parent_device: Option<String>,
    processor: Option<String>,
    core: Option<String>,
    fpu: Option<String>,
    endian: Option<String>,
    rust_target: Option<String>,
    source_pack_vendor: String,
    source_pack_name: String,
    source_pack_version: Option<String>,
    source_pdsc: String,
}

pub fn load_inventory(
    root: &Path,
    index_sha256: &str,
    selectors: &[Selector],
) -> Result<Vec<Device>> {
    let metadata_path = root.join("metadata.json");
    let metadata: Metadata = serde_json::from_reader(
        File::open(&metadata_path)
            .with_context(|| format!("读取 {} 失败", metadata_path.display()))?,
    )
    .with_context(|| format!("解析 {} 失败", metadata_path.display()))?;

    if metadata.schema_version != 1 {
        bail!("不支持的目标数据库 schema：{}", metadata.schema_version);
    }
    if metadata.source_index_sha256.as_deref() != Some(index_sha256) {
        bail!(
            "目标数据库索引 SHA-256 不匹配：期望 {index_sha256}，实际 {:?}",
            metadata.source_index_sha256
        );
    }

    let records_path = root.join("devices.jsonl");
    let records = BufReader::new(
        File::open(&records_path)
            .with_context(|| format!("读取 {} 失败", records_path.display()))?,
    );
    let mut selected = Vec::new();
    for (index, line) in records.lines().enumerate() {
        let line = line
            .with_context(|| format!("读取 {} 第 {} 行失败", records_path.display(), index + 1))?;
        if line.trim().is_empty() {
            continue;
        }
        let record: Record = serde_json::from_str(&line)
            .with_context(|| format!("解析 {} 第 {} 行失败", records_path.display(), index + 1))?;
        if selectors.iter().any(|selector| {
            record.source_pack_vendor == selector.vendor
                && wildcard_match(&selector.pack_pattern, &record.source_pack_name)
        }) {
            selected.push(record);
        }
    }

    normalize(selected)
}

pub fn wildcard_match(pattern: &str, value: &str) -> bool {
    let Some((prefix, suffix)) = pattern.split_once('*') else {
        return pattern == value;
    };
    if suffix.contains('*') {
        return false;
    }
    value.len() >= prefix.len() + suffix.len()
        && value.starts_with(prefix)
        && value.ends_with(suffix)
}

fn normalize(records: Vec<Record>) -> Result<Vec<Device>> {
    let variant_parents: BTreeSet<_> = records
        .iter()
        .filter(|record| record.device_kind == "variant")
        .filter_map(|record| {
            record.parent_device.as_ref().map(|parent| {
                (
                    record.source_pack_vendor.clone(),
                    record.source_pack_name.clone(),
                    record.source_pack_version.clone(),
                    parent.clone(),
                )
            })
        })
        .collect();
    let mut devices: BTreeMap<(String, String, String, String), Device> = BTreeMap::new();

    for record in records {
        let version = record
            .source_pack_version
            .clone()
            .ok_or_else(|| anyhow::anyhow!("{} 缺少 Pack 版本", record.device))?;
        let parent_key = (
            record.source_pack_vendor.clone(),
            record.source_pack_name.clone(),
            Some(version.clone()),
            record.device.clone(),
        );
        if record.device_kind == "device" && variant_parents.contains(&parent_key) {
            continue;
        }

        let chip = canonical_chip(&record.device)?;
        let key = (
            record.source_pack_vendor.clone(),
            record.source_pack_name.clone(),
            version.clone(),
            chip.clone(),
        );
        let device = devices.entry(key).or_insert_with(|| Device {
            chip,
            original_name: record.device.clone(),
            device_kind: record.device_kind.clone(),
            parent_device: record.parent_device.clone(),
            pack_vendor: record.source_pack_vendor.clone(),
            pack_name: record.source_pack_name.clone(),
            pack_version: version,
            source_pdsc: record.source_pdsc.clone(),
            processors: Vec::new(),
        });

        if device.original_name != record.device
            || device.device_kind != record.device_kind
            || device.parent_device != record.parent_device
            || device.source_pdsc != record.source_pdsc
        {
            bail!("规范型号 {} 存在冲突记录", device.chip);
        }
        device.processors.push(Processor {
            name: record.processor,
            core: record.core,
            fpu: record.fpu,
            endian: record.endian,
            rust_target: record.rust_target,
        });
    }

    for device in devices.values_mut() {
        device.processors.sort();
        device.processors.dedup();
    }

    let devices: Vec<_> = devices.into_values().collect();
    let mut origins = BTreeMap::new();
    for device in &devices {
        let origin = (&device.pack_vendor, &device.pack_name, &device.pack_version);
        if let Some(previous) = origins.insert(&device.chip, origin)
            && previous != origin
        {
            bail!(
                "规范型号 {} 同时来自多个 Pack：{}.{}@{} 与 {}.{}@{}",
                device.chip,
                previous.0,
                previous.1,
                previous.2,
                origin.0,
                origin.1,
                origin.2
            );
        }
    }

    Ok(devices)
}

fn canonical_chip(value: &str) -> Result<String> {
    if value.is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
    {
        bail!("器件名包含不支持的字符：{value:?}");
    }
    Ok(value.to_ascii_lowercase())
}
