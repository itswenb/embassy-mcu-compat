#!/usr/bin/env python3
"""顺序编译去重后的 GD32 metadata 与 metapac PAC。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import compile_gigadevice_pacs as pac_compile
import gigadevice_sources as common


def report_is_reusable(
    report: dict[str, object],
    summary: dict[str, int],
    provenance: dict[str, str],
    rustc: str,
) -> bool:
    current_summary = report.get("summary")
    current_provenance = report.get("provenance")
    return (
        report.get("rustc") == rustc
        and isinstance(current_summary, dict)
        and isinstance(current_provenance, dict)
        and all(current_summary.get(key) == value for key, value in summary.items())
        and all(
            current_provenance.get(key) == value
            for key, value in provenance.items()
        )
    )


def exact_targets(
    models: dict[str, object], devices: list[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    raw_models = models.get("devices")
    if not isinstance(raw_models, list):
        raise ValueError("型号报告缺少 devices")
    by_id = {
        str(model["id"]).upper(): model
        for model in raw_models
        if isinstance(model, dict) and "id" in model
    }
    result = []
    missing = []
    for device in devices:
        device = device.upper()
        model = by_id.get(device)
        target = model.get("rust_target") if model is not None else None
        if isinstance(target, str) and target:
            result.append((device, target))
        else:
            missing.append(device)
    return result, missing


def riscv_targets(
    models: dict[str, object], devices: list[str]
) -> list[tuple[str, str]]:
    targets, missing = exact_targets(models, devices)
    if missing or any(not target.startswith("riscv32") for _, target in targets):
        raise ValueError(f"RISC-V 型号缺少精确 Rust target：{missing}")
    return targets


def validate_riscv_pacs(metapac: Path, devices: list[str]) -> int:
    forbidden = ("cortex_m", "vector_table.interrupts", "__INTERRUPTS")
    for device in devices:
        pac = metapac / "src/chips" / device.lower() / "pac.rs"
        if not pac.is_file():
            raise ValueError(f"RISC-V 芯片缺少 PAC：{device}")
        source = pac.read_text(encoding="utf-8")
        found = [value for value in forbidden if value in source]
        if found:
            raise ValueError(f"RISC-V PAC 包含 ARM 专用内容：{device}:{found}")
        if "pub const fn number" not in source:
            raise ValueError(f"RISC-V PAC 缺少架构中立的中断编号方法：{device}")
    return len(devices)


def _rust_string(path: Path) -> str:
    return json.dumps(path.as_posix())


def validation_sources(
    metapac: Path, source_root: Path | None = None
) -> tuple[list[str], list[str], list[str]]:
    chips_root = metapac / "src/chips"
    devices = sorted(path.name for path in chips_root.iterdir() if path.is_dir())
    if not devices:
        raise ValueError("metapac 不包含芯片目录")
    source_root = source_root or metapac.resolve()
    groups: dict[str, str] = {}
    metadata_groups: dict[str, str] = {}
    for device in devices:
        chip = chips_root / device
        pac = chip / "pac.rs"
        metadata = chip / "metadata.rs"
        if not pac.is_file() or not metadata.is_file():
            raise ValueError(f"芯片缺少 PAC 或 metadata：{device}")
        digest = common._sha256(pac)
        groups.setdefault(digest, device)
        metadata_text = metadata.read_text(encoding="utf-8")
        include = re.search(r'include!\("\.\./(metadata_\d+\.rs)"\);', metadata_text)
        name = re.search(
            r'pub static METADATA:\s*Metadata\s*=\s*Metadata\s*\{.*?\bname:\s*"([^"]+)"',
            metadata_text,
            re.DOTALL,
        )
        if name is None or name.group(1).lower() != device:
            raise ValueError(f"芯片 metadata 的名称无效：{device}")
        if include is not None and not (chips_root / include.group(1)).is_file():
            raise ValueError(f"芯片 metadata 引用不存在：{device}:{include.group(1)}")
        normalized = metadata_text[: name.start(1)] + "<DEVICE>" + metadata_text[name.end(1) :]
        metadata_groups.setdefault(hashlib.sha256(normalized.encode()).hexdigest(), device)
    source_prefix = (
        "#![allow(dead_code, unused_imports, non_snake_case, "
        "non_camel_case_types, non_upper_case_globals)]\n"
    )
    metadata_sources = [
        source_prefix
        + "pub mod metadata {\n"
        + f"    include!({_rust_string(source_root / 'src/metadata.rs')});\n"
        + "}\n"
        + "mod chip_metadata {\n"
        + "    use crate::metadata::*;\n"
        + f"    include!({_rust_string(source_root / 'src/chips' / device / 'metadata.rs')});\n"
        + "}\nfn main() {}\n"
        for device in metadata_groups.values()
    ]
    pac_sources = [
        source_prefix
        + f"#[path = {_rust_string(source_root / 'src/common.rs')}]\n"
        + "pub mod common;\n"
        + f"#[path = {_rust_string(source_root / 'src/chips' / device / 'pac.rs')}]\n"
        + "mod pac;\nfn main() {}\n"
        for device in groups.values()
    ]
    return devices, pac_sources, metadata_sources


def _write_validation_crate(
    work: Path, pac_sources: list[str], metadata_sources: list[str]
) -> list[str]:
    work.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".metapac-check-", dir=work.parent) as directory:
        temporary = Path(directory)
        bins = temporary / "src/bin"
        bins.mkdir(parents=True)
        (temporary / "Cargo.toml").write_text(
            """[package]
name = "gigadevice-metapac-validation"
version = "0.0.0"
edition = "2024"
publish = false

[dependencies]
cortex-m = "0.7.6"

[features]
defmt = []
rt = []
""",
            encoding="utf-8",
        )
        targets = []
        for kind, sources in (("pac", pac_sources), ("metadata", metadata_sources)):
            for index, source in enumerate(sources):
                target = f"{kind}_{index:04}"
                (bins / f"{target}.rs").write_text(source, encoding="utf-8")
                targets.append(target)
        (temporary / "generation.json").write_text(
            '{"schema_version":1}\n', encoding="utf-8"
        )
        if work.exists():
            if not (work / "generation.json").is_file():
                raise ValueError(f"拒绝覆盖非本脚本生成目录：{work}")
            shutil.rmtree(work)
        temporary.rename(work)
    return targets


def _check_targets(
    work: Path, target_dir: Path, targets: list[str], offline: bool
) -> None:
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    environment["CARGO_INCREMENTAL"] = "0"
    for index, target in enumerate(targets, 1):
        command = [
            "cargo",
            "check",
            "--quiet",
            "--manifest-path",
            str(work / "Cargo.toml"),
            "--bin",
            target,
        ]
        if offline:
            command.insert(2, "--offline")
        result = subprocess.run(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if result.returncode != 0:
            raise ValueError(f"metapac 目标编译失败：{target}\n{result.stderr[-8000:]}")
        if index % 10 == 0 or index == len(targets):
            print(f"已编译 {index}/{len(targets)} 个去重目标")


def _check_exact_features(
    metapac: Path,
    target_dir: Path,
    targets: list[tuple[str, str]],
    offline: bool,
) -> None:
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    environment["CARGO_INCREMENTAL"] = "0"
    for index, (device, target) in enumerate(targets, 1):
        command = ["cargo", "check", "--quiet"]
        if offline:
            command.append("--offline")
        command.extend(
            [
                "-Z",
                "build-std=core",
                "--manifest-path",
                str(metapac / "Cargo.toml"),
                "--target",
                target,
                "--no-default-features",
                "--features",
                f"pac,metadata,{device.lower()}",
            ]
        )
        result = subprocess.run(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if result.returncode != 0:
            raise ValueError(
                f"metapac feature 真实目标编译失败：{device}:{target}\n{result.stderr[-8000:]}"
            )
        if index % 25 == 0 or index == len(targets):
            print(f"已按真实目标编译 feature {index}/{len(targets)}：{device}")


def _args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metapac-dir",
        type=Path,
        default=root / ".cache/generated/gigadevice-metapac-v1",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=root / ".cache/generated/gigadevice-metapac-validation-v1",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=root / ".cache/tools/gigadevice-metapac-validation-target",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "reports/gigadevice-metapac-compile.json",
    )
    parser.add_argument(
        "--models",
        type=Path,
        default=root / "reports/gigadevice-models.json",
    )
    parser.add_argument(
        "--feature-target-dir",
        type=Path,
        default=root / ".cache/tools/gigadevice-feature-metapac-target",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    manifest = json.loads(
        (args.metapac_dir / ".m32-metapac-generation.json").read_text(encoding="utf-8")
    )
    riscv_devices = manifest.get("riscv_devices")
    if not isinstance(riscv_devices, list) or not all(
        isinstance(device, str) for device in riscv_devices
    ):
        raise ValueError("metapac 生成标记缺少 RISC-V 型号清单")
    riscv_validated = validate_riscv_pacs(args.metapac_dir, riscv_devices)
    if riscv_validated != int(manifest.get("riscv_chips", -1)):
        raise ValueError("RISC-V PAC 审计数量与生成标记不一致")
    model_report = json.loads(args.models.read_text(encoding="utf-8"))
    exact_riscv_targets = riscv_targets(model_report, riscv_devices)
    cargo = tomllib.loads((args.metapac_dir / "Cargo.toml").read_text(encoding="utf-8"))
    relative_root = Path(os.path.relpath(args.metapac_dir, args.work_dir / "src/bin"))
    devices, pac_sources, metadata_sources = validation_sources(
        args.metapac_dir, relative_root
    )
    exact_feature_targets, missing_feature_targets = exact_targets(model_report, devices)
    expected_devices = int(model_report["summary"]["normalized_devices"])
    if int(manifest["chips"]) != len(devices) or len(devices) != expected_devices:
        raise ValueError("metapac 芯片数未与生成标记和型号清单闭合")
    features = cargo.get("features")
    if not isinstance(features, dict) or any(device not in features for device in devices):
        raise ValueError("metapac Cargo feature 未覆盖全部芯片")
    rustc = pac_compile._rustc_version()
    source_hash = hashlib.sha256()
    for source in pac_sources + metadata_sources:
        source_hash.update(len(source).to_bytes(8, "big"))
        source_hash.update(source.encode())
    summary = {
        "devices": len(devices),
        "features_validated": len(devices),
        "metadata_wrappers_validated": len(devices),
        "metadata_groups": len(metadata_sources),
        "metadata_groups_compiled": len(metadata_sources),
        "pac_groups": len(pac_sources),
        "pac_groups_compiled": len(pac_sources),
        "riscv_pacs_validated": riscv_validated,
        "riscv_features_compiled_for_exact_target": len(exact_riscv_targets),
        "features_compiled_for_exact_target": len(exact_feature_targets),
        "features_missing_exact_target": len(missing_feature_targets),
        "failed": 0,
    }
    provenance = {
        "generation_marker_sha256": common._sha256(
            args.metapac_dir / ".m32-metapac-generation.json"
        ),
        "cargo_toml_sha256": common._sha256(args.metapac_dir / "Cargo.toml"),
        "models_sha256": common._sha256(args.models),
        "validation_source_sha256": source_hash.hexdigest(),
    }
    if args.output.is_file() and not args.force:
        try:
            existing = json.loads(args.output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if isinstance(existing, dict) and report_is_reusable(
            existing, summary, provenance, rustc
        ):
            print(f"复用已验证的 {len(devices)} 机型编译报告：{args.output}")
            return 0
    targets = _write_validation_crate(args.work_dir, pac_sources, metadata_sources)
    _check_targets(args.work_dir, args.target_dir, targets, args.offline)
    _check_exact_features(
        args.metapac_dir,
        args.feature_target_dir,
        exact_feature_targets,
        args.offline,
    )
    report = {
        "schema_version": 1,
        "summary": summary,
        "rustc": rustc,
        "provenance": provenance,
        "missing_exact_target": missing_feature_targets,
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"devices={len(devices)} metadata_groups={len(metadata_sources)} "
        f"pac_groups={len(pac_sources)} failed=0"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
