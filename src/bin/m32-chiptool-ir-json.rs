use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use walkdir::WalkDir;

fn convert_tree(input: &Path, output: &Path) -> Result<usize> {
    let mut converted = 0;
    for entry in WalkDir::new(input) {
        let entry = entry.with_context(|| format!("遍历 {} 失败", input.display()))?;
        if !entry.file_type().is_file()
            || entry.path().extension().and_then(|value| value.to_str()) != Some("yaml")
        {
            continue;
        }
        let relative = entry
            .path()
            .strip_prefix(input)
            .with_context(|| format!("计算 {} 相对路径失败", entry.path().display()))?;
        let target = output.join(relative).with_extension("json");
        let value: serde_yaml::Value = serde_yaml::from_slice(
            &fs::read(entry.path())
                .with_context(|| format!("读取 {} 失败", entry.path().display()))?,
        )
        .with_context(|| format!("解析 {} 失败", entry.path().display()))?;
        let parent = target.parent().context("JSON 输出路径缺少父目录")?;
        fs::create_dir_all(parent).with_context(|| format!("创建 {} 失败", parent.display()))?;
        let mut bytes = serde_json::to_vec_pretty(&value)
            .with_context(|| format!("编码 {} 失败", target.display()))?;
        bytes.push(b'\n');
        fs::write(&target, bytes).with_context(|| format!("写入 {} 失败", target.display()))?;
        converted += 1;
    }
    if converted == 0 {
        bail!("{} 中没有 chiptool YAML IR", input.display());
    }
    Ok(converted)
}

fn main() -> Result<()> {
    let mut args = std::env::args_os().skip(1).map(PathBuf::from);
    let input = args.next().context("缺少 YAML 输入目录")?;
    let output = args.next().context("缺少 JSON 输出目录")?;
    if args.next().is_some() {
        bail!("只接受 YAML 输入目录和 JSON 输出目录两个参数");
    }
    println!("converted={}", convert_tree(&input, &output)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 递归转换_chiptool_ir_并保留结构() {
        let temporary = tempfile::tempdir().unwrap();
        let input = temporary.path().join("yaml/nested");
        let output = temporary.path().join("json");
        fs::create_dir_all(&input).unwrap();
        fs::write(
            input.join("timer.yaml"),
            "block/TIMER:\n  items:\n  - name: CTL\n    byte_offset: 0\n",
        )
        .unwrap();

        assert_eq!(
            convert_tree(&temporary.path().join("yaml"), &output).unwrap(),
            1
        );
        let value: serde_json::Value =
            serde_json::from_slice(&fs::read(output.join("nested/timer.json")).unwrap()).unwrap();
        assert_eq!(value["block/TIMER"]["items"][0]["name"], "CTL");
    }
}
