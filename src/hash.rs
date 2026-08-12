use std::fs::File;
use std::io::{BufReader, Read};
use std::path::{Component, Path};

use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};
use walkdir::WalkDir;

pub fn sha256_file(path: &Path) -> Result<String> {
    let file = File::open(path).with_context(|| format!("读取 {} 失败", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = reader
            .read(&mut buffer)
            .with_context(|| format!("读取 {} 失败", path.display()))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

pub fn verify_file(path: &Path, expected: &str) -> Result<()> {
    let actual = sha256_file(path)?;
    if actual != expected {
        bail!(
            "{} 的 SHA-256 不匹配：期望 {expected}，实际 {actual}",
            path.display()
        );
    }
    Ok(())
}

pub fn sha256_tree(root: &Path) -> Result<String> {
    let mut files = Vec::new();
    for entry in WalkDir::new(root).follow_links(false) {
        let entry = entry.with_context(|| format!("遍历 {} 失败", root.display()))?;
        if entry.file_type().is_symlink() {
            bail!("目录哈希不接受符号链接：{}", entry.path().display());
        }
        if entry.file_type().is_file() {
            let relative = entry.path().strip_prefix(root).with_context(|| {
                format!("{} 不在 {} 内", entry.path().display(), root.display())
            })?;
            files.push((portable_path(relative)?, entry.into_path()));
        }
    }
    files.sort_by(|left, right| left.0.cmp(&right.0));

    let mut hasher = Sha256::new();
    for (relative, path) in files {
        let path_bytes = relative.as_bytes();
        hasher.update((path_bytes.len() as u64).to_be_bytes());
        hasher.update(path_bytes);
        let size = path
            .metadata()
            .with_context(|| format!("读取 {} 元数据失败", path.display()))?
            .len();
        hasher.update(size.to_be_bytes());

        let mut reader = BufReader::new(
            File::open(&path).with_context(|| format!("读取 {} 失败", path.display()))?,
        );
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = reader
                .read(&mut buffer)
                .with_context(|| format!("读取 {} 失败", path.display()))?;
            if count == 0 {
                break;
            }
            hasher.update(&buffer[..count]);
        }
    }

    Ok(format!("{:x}", hasher.finalize()))
}

fn portable_path(path: &Path) -> Result<String> {
    let mut result = String::new();
    for component in path.components() {
        let Component::Normal(component) = component else {
            bail!("目录哈希遇到非规范相对路径：{}", path.display());
        };
        let component = component
            .to_str()
            .ok_or_else(|| anyhow::anyhow!("路径不是 UTF-8：{}", path.display()))?;
        if !result.is_empty() {
            result.push('/');
        }
        result.push_str(component);
    }
    Ok(result)
}
