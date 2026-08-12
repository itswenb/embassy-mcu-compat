use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use anyhow::{Context, Result, bail};
use roxmltree::{Document, Node};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceFacts {
    pub memories: BTreeMap<String, MemoryRegion>,
    pub header: Option<String>,
    pub svd: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MemoryRegion {
    pub start: u64,
    pub size: u64,
}

pub fn read_device_facts(pdsc: &Path, pack_root: &Path, device: &str) -> Result<DeviceFacts> {
    pdsc.strip_prefix(pack_root).with_context(|| {
        format!(
            "PDSC {} 不在 Pack 根目录 {} 内",
            pdsc.display(),
            pack_root.display()
        )
    })?;
    let xml =
        fs::read_to_string(pdsc).with_context(|| format!("读取 PDSC {} 失败", pdsc.display()))?;
    let document =
        Document::parse(&xml).with_context(|| format!("解析 PDSC {} 失败", pdsc.display()))?;

    let candidates: Vec<_> = document
        .descendants()
        .filter(|node| {
            node.is_element()
                && ((node.tag_name().name() == "device" && node.attribute("Dname") == Some(device))
                    || (node.tag_name().name() == "variant"
                        && node.attribute("Dvariant") == Some(device)))
        })
        .collect();
    let candidate = match candidates.as_slice() {
        [] => bail!("PDSC {} 中不存在器件 {device}", pdsc.display()),
        [candidate] => *candidate,
        _ => bail!("PDSC {} 中器件 {device} 重复", pdsc.display()),
    };

    let levels: Vec<_> = document
        .descendants()
        .filter(|node| is_device_level(*node))
        .filter(|node| *node == candidate || candidate.ancestors().any(|parent| parent == *node))
        .collect();
    let mut facts = DeviceFacts {
        memories: BTreeMap::new(),
        header: None,
        svd: None,
    };
    for level in levels {
        apply_level(level, pack_root, &mut facts)?;
    }
    Ok(facts)
}

fn is_device_level(node: Node<'_, '_>) -> bool {
    node.is_element()
        && matches!(
            node.tag_name().name(),
            "family" | "subFamily" | "device" | "variant"
        )
}

fn apply_level(node: Node<'_, '_>, pack_root: &Path, facts: &mut DeviceFacts) -> Result<()> {
    let mut memories = BTreeMap::new();
    let mut header = None;
    let mut svd = None;

    for child in node.children().filter(Node::is_element) {
        match child.tag_name().name() {
            "memory" => {
                let key = child
                    .attribute("id")
                    .or_else(|| child.attribute("name"))
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| anyhow::anyhow!("memory 缺少 id 或 name"))?;
                let region = MemoryRegion {
                    start: parse_number(attribute(child, "start")?)?,
                    size: parse_number(attribute(child, "size")?)?,
                };
                if let Some(previous) = memories.insert(key.to_owned(), region)
                    && previous != region
                {
                    bail!("memory {key} 属性冲突");
                }
            }
            "compile" => {
                if let Some(value) = child.attribute("header") {
                    set_path(&mut header, value, pack_root, "compile@header")?;
                }
            }
            "debug" => {
                if let Some(value) = child.attribute("svd") {
                    set_path(&mut svd, value, pack_root, "debug@svd")?;
                }
            }
            _ => {}
        }
    }

    facts.memories.extend(memories);
    if header.is_some() {
        facts.header = header;
    }
    if svd.is_some() {
        facts.svd = svd;
    }
    Ok(())
}

fn attribute<'a>(node: Node<'a, '_>, name: &str) -> Result<&'a str> {
    node.attribute(name)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow::anyhow!("memory 缺少 {name}"))
}

fn parse_number(value: &str) -> Result<u64> {
    let parsed = if let Some(hex) = value
        .strip_prefix("0x")
        .or_else(|| value.strip_prefix("0X"))
    {
        u64::from_str_radix(hex, 16)
    } else {
        value.parse()
    };
    parsed.with_context(|| format!("无法解析整数 {value:?}"))
}

fn set_path(slot: &mut Option<String>, value: &str, pack_root: &Path, field: &str) -> Result<()> {
    let normalized = normalize_path(value, pack_root)?;
    if let Some(previous) = slot
        && previous != &normalized
    {
        bail!("同一层的 {field} 属性冲突：{previous:?} 与 {normalized:?}");
    }
    *slot = Some(normalized);
    Ok(())
}

fn normalize_path(value: &str, pack_root: &Path) -> Result<String> {
    let value = value.trim();
    if value.is_empty() || value.starts_with(['/', '\\']) {
        bail!("Pack 文件必须是规范相对路径：{value:?}");
    }
    let components: Vec<_> = value.split(['/', '\\']).collect();
    if components.iter().any(|component| {
        component.is_empty() || matches!(*component, "." | "..") || component.contains(':')
    }) {
        bail!("Pack 文件必须是规范相对路径：{value:?}");
    }
    let normalized = components.join("/");
    if !pack_root.join(&normalized).starts_with(pack_root) {
        bail!("Pack 文件路径越出根目录：{value:?}");
    }
    Ok(normalized)
}
