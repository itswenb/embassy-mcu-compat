#!/usr/bin/env python3
"""按 example 声明逐个编译代表性 GD32 型号并生成 memory.x。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tomllib
from pathlib import Path

import check_gigadevice_embassy_projection as projection_check


def load_examples(
    manifest: Path, projections: list[dict[str, object]]
) -> list[dict[str, object]]:
    features = tomllib.loads(manifest.read_text(encoding="utf-8")).get("features")
    if not isinstance(features, dict):
        raise ValueError(f"example 缺少 features：{manifest}")
    by_chip = {str(row["chip"]): row for row in projections}
    examples = []
    for chip, dependencies in features.items():
        if chip == "default":
            continue
        projection = by_chip.get(chip)
        if projection is None:
            raise ValueError(f"example 使用了未发布型号：{chip}")
        if not isinstance(dependencies, list):
            raise ValueError(f"example feature 不是数组：{chip}")
        expected = f'embassy-stm32/{projection["profile"]}'
        profiles = [
            value
            for value in dependencies
            if isinstance(value, str) and value.startswith("embassy-stm32/stm32")
        ]
        if profiles != [expected]:
            raise ValueError(f"{chip} 必须且只能使用 profile {projection['profile']}")
        if "embassy-stm32/memory-x" not in dependencies:
            raise ValueError(f"example 未为 {chip} 启用 memory-x")
        examples.append(projection)
    if not examples:
        raise ValueError("example 未声明真实型号")
    return examples


def compile_spec(
    manifest: Path,
    publication: Path,
    projection: dict[str, object],
    target_dir: Path,
    offline: bool,
) -> dict[str, object]:
    command = ["cargo", "check", "--quiet"]
    if offline:
        command.append("--offline")
    command.extend(
        [
            "--manifest-path",
            str(manifest),
            "--target",
            str(projection["rust_target"]),
            "--no-default-features",
            "--features",
            str(projection["chip"]),
            "--config",
            f"patch.crates-io.stm32-metapac.path={json.dumps(str(publication.resolve()))}",
        ]
    )
    environment = os.environ.copy()
    environment["EMBASSY_MCU_COMPAT_CHIP"] = str(projection["chip"])
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    environment["CARGO_INCREMENTAL"] = "0"
    return {"command": command, "environment": environment}


def _args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=root / "examples/gigadevice/Cargo.toml"
    )
    parser.add_argument(
        "--publication",
        type=Path,
        default=root / ".cache/generated/mcu-metapac-publication-v1",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=root / ".cache/tools/gigadevice-example-target",
    )
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    projections = projection_check._load_projections(args.publication)
    examples = load_examples(args.manifest, projections)
    for index, projection in enumerate(examples, 1):
        spec = compile_spec(
            args.manifest,
            args.publication,
            projection,
            args.target_dir,
            args.offline,
        )
        result = subprocess.run(
            spec["command"], env=spec["environment"], capture_output=True, text=True
        )
        if result.returncode != 0:
            print(result.stderr, flush=True)
            raise ValueError(f"example 编译失败：{projection['chip']}")
        print(f"已编译 example {index}/{len(examples)}：{projection['chip']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
