#!/usr/bin/env python3
"""扫描 embassy-stm32 对 STM32 名称与 Cortex-M 架构的静态绑定。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import gigadevice_sources as common


def scan_embassy_stm32(root: Path) -> dict[str, object]:
    source_files = sorted((root / "src").rglob("*.rs"))
    texts = {path: path.read_text(encoding="utf-8") for path in source_files}
    cfg_files = {
        path
        for path, text in texts.items()
        if any("cfg" in line and "stm32" in line.lower() for line in text.splitlines())
    }
    cfg_lines = sum(
        1
        for text in texts.values()
        for line in text.splitlines()
        if "cfg" in line and "stm32" in line.lower()
    )
    cortex_files = {
        path
        for path, text in texts.items()
        if re.search(r"\bcortex_m(?:::|\b)", text) is not None
    }
    build_rs = (root / "build.rs").read_text(encoding="utf-8")
    cargo_toml = (root / "Cargo.toml").read_text(encoding="utf-8")
    prefix_branches = len(re.findall(r"\.starts_with\(\s*\"stm32", build_rs, re.IGNORECASE))
    feature_gate_mentions = len(re.findall(r"CARGO_FEATURE_STM32", build_rs))
    unconditional_cortex = re.search(r"(?m)^\s*cortex-m\s*=", cargo_toml) is not None
    return {
        "rust_source_files": len(source_files),
        "stm32_cfg_files": len(cfg_files),
        "stm32_cfg_lines": cfg_lines,
        "cortex_m_source_files": len(cortex_files),
        "stm32_prefix_branches": prefix_branches,
        "cargo_feature_stm32_mentions": feature_gate_mentions,
        "unconditional_cortex_m_dependency": unconditional_cortex,
        "metadata_only_all_architectures_possible": not (
            unconditional_cortex or cortex_files or prefix_branches or cfg_files
        ),
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--embassy-root",
        type=Path,
        default=repo_root / ".cache/research/repos/embassy",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/embassy-stm32-boundary.json",
    )
    args = parser.parse_args()
    crate = args.embassy_root / "embassy-stm32"
    if not crate.is_dir():
        raise ValueError(f"embassy-stm32 不存在：{crate}")
    report = scan_embassy_stm32(crate)
    revision = subprocess.run(
        ["git", "-C", str(args.embassy_root), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    report = {
        "schema_version": 1,
        "embassy_revision": revision,
        "crate": "embassy-stm32",
        **report,
        "conclusion": (
            "仅替换 stm32-metapac metadata 不能覆盖非 Cortex-M GD32；"
            "同时，Cortex-M 型号仍需逐项验证 STM32 家族 cfg 与初始化路径。"
        ),
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in report.items() if isinstance(value, int)))
    print(f"边界报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
