use std::collections::BTreeSet;
use std::fs;
use std::io::Write;
use std::path::{Component, Path};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

use crate::target_db::{Device, Selector};

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct SourceLock {
    pub schema: u32,
    pub index: IndexLock,
    pub target_db: TargetDbLock,
    pub selectors: Vec<Selector>,
    pub tools: ToolLock,
    pub upstream: UpstreamLock,
    #[serde(default)]
    pub packs: Vec<PackLock>,
    #[serde(default)]
    pub devices: Vec<Device>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct IndexLock {
    pub url: String,
    pub timestamp: String,
    pub sha256: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct TargetDbLock {
    pub url: String,
    pub revision: String,
    pub schema: u32,
    pub source_index_sha256: String,
    pub devices_sha256: String,
    pub metadata_sha256: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct ToolLock {
    pub cmsis_toolbox: String,
    pub cpackget: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct UpstreamLock {
    pub embassy: String,
    pub stm32_data: String,
    pub stm32_data_generated: String,
    pub chiptool: String,
    pub stm32_metapac_version: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct PackLock {
    pub vendor: String,
    pub name: String,
    pub version: String,
    pub url: String,
    pub archive: String,
    pub archive_sha256: String,
    pub tree_sha256: String,
    pub pdsc: String,
}

impl SourceLock {
    pub fn read(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let contents = fs::read_to_string(path)
            .with_context(|| format!("读取来源锁 {} 失败", path.display()))?;
        Self::parse(&contents).with_context(|| format!("解析来源锁 {} 失败", path.display()))
    }

    pub fn parse(contents: &str) -> Result<Self> {
        let mut lock: Self = toml::from_str(contents).context("解析来源锁 TOML 失败")?;
        lock.normalize();
        lock.validate()?;
        Ok(lock)
    }

    pub fn to_toml(&self) -> Result<String> {
        let mut lock = self.clone();
        lock.normalize();
        lock.validate()?;
        let mut contents = toml::to_string_pretty(&lock).context("编码来源锁 TOML 失败")?;
        if !contents.ends_with('\n') {
            contents.push('\n');
        }
        Ok(contents)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        let contents = self.to_toml()?;
        Self::parse(&contents).context("写入前自校验来源锁失败")?;

        let parent = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent).with_context(|| format!("创建 {} 失败", parent.display()))?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent)
            .with_context(|| format!("在 {} 创建临时锁文件失败", parent.display()))?;
        temporary
            .write_all(contents.as_bytes())
            .with_context(|| format!("写入 {} 的临时文件失败", path.display()))?;
        temporary
            .as_file()
            .sync_all()
            .with_context(|| format!("同步 {} 的临时文件失败", path.display()))?;
        temporary
            .persist(path)
            .map_err(|error| error.error)
            .with_context(|| format!("原子替换 {} 失败", path.display()))?;
        Ok(())
    }

    pub fn validate(&self) -> Result<()> {
        if self.schema != 1 {
            bail!("不支持的来源锁 schema：{}", self.schema);
        }
        if self.target_db.schema != 1 {
            bail!("不支持的目标数据库 schema：{}", self.target_db.schema);
        }
        if self.index.sha256 != self.target_db.source_index_sha256 {
            bail!(
                "目标数据库索引 SHA-256 不匹配：索引为 {}，目标数据库为 {}",
                self.index.sha256,
                self.target_db.source_index_sha256
            );
        }
        if self.selectors.is_empty() {
            bail!("来源锁至少需要一个来源选择器");
        }

        let mut packs = BTreeSet::new();
        for pack in &self.packs {
            if !packs.insert((&pack.vendor, &pack.name, &pack.version)) {
                bail!(
                    "来源锁包含重复 Pack：{}.{}@{}",
                    pack.vendor,
                    pack.name,
                    pack.version
                );
            }
            validate_relative(&pack.archive)?;
            validate_relative(&pack.pdsc)?;
        }

        let mut chips = BTreeSet::new();
        for device in &self.devices {
            if !chips.insert(&device.chip) {
                bail!("来源锁包含重复器件：{}", device.chip);
            }
        }
        Ok(())
    }

    fn normalize(&mut self) {
        self.selectors.sort_by(|left, right| {
            (&left.vendor, &left.pack_pattern).cmp(&(&right.vendor, &right.pack_pattern))
        });
        self.packs.sort_by(|left, right| {
            (&left.vendor, &left.name, &left.version).cmp(&(
                &right.vendor,
                &right.name,
                &right.version,
            ))
        });
        self.devices
            .sort_by(|left, right| left.chip.cmp(&right.chip));
    }
}

fn validate_relative(value: &str) -> Result<()> {
    let path = Path::new(value);
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        bail!("锁文件路径必须是规范相对路径：{value:?}");
    }
    Ok(())
}
