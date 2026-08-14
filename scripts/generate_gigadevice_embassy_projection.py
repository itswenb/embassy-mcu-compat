#!/usr/bin/env python3
"""从 GD32 规范事实生成 Embassy 兼容投影。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import gigadevice_sources as common
from normalize_gigadevice_embassy_names import embassy_instance_name


REQUIRED_SYSTEM_PERIPHERALS = {"EXTI", "FLASH", "RCC"}


def normalized_instance_names(variant: dict[str, object]) -> set[str]:
    names = set()
    for instance in variant.get("instances", []):
        if not isinstance(instance, dict):
            raise ValueError("GD32 变体包含无效外设实例")
        mapped = embassy_instance_name(str(instance["name"]))
        if mapped is not None:
            names.add(mapped[0])
    return names


def _candidate_names(candidate: dict[str, object]) -> set[str]:
    cores = candidate.get("cores")
    if not isinstance(cores, list) or len(cores) != 1:
        return set()
    peripherals = cores[0].get("peripherals")
    if not isinstance(peripherals, list):
        return set()
    return {
        str(peripheral["name"])
        for peripheral in peripherals
        if isinstance(peripheral, dict) and "name" in peripheral
    }


def select_profile(
    model: dict[str, object],
    variant: dict[str, object],
    candidates: list[dict[str, object]],
    compatibility: dict[str, object],
) -> dict[str, object]:
    del compatibility
    target = str(model.get("rust_target", ""))
    if target.startswith("riscv"):
        return {"profile": None, "status": "blocked", "reasons": "RISC-V 不适用"}

    real = normalized_instance_names(variant)
    required = REQUIRED_SYSTEM_PERIPHERALS & real
    scored = []
    for candidate in candidates:
        names = _candidate_names(candidate)
        if not required <= names:
            continue
        name = str(candidate.get("name", "")).lower()
        if not name.startswith("stm32"):
            continue
        score = (len(real - names), len(names - real), name)
        scored.append((score, name))
    if not scored:
        return {
            "profile": None,
            "status": "blocked",
            "reasons": "没有通过核心系统外设门的 STM32 profile",
        }
    _, profile = min(scored)
    return {"profile": profile, "status": "projected", "reasons": ""}


def build_profile_report(
    models: dict[str, object],
    variants: dict[str, object],
    candidates: list[dict[str, object]],
    compatibility: dict[str, object],
) -> dict[str, object]:
    raw_models = models.get("devices")
    raw_variants = variants.get("variants")
    if not isinstance(raw_models, list) or not isinstance(raw_variants, list):
        raise ValueError("型号或变体报告格式无效")
    variants_by_device = {}
    for variant in raw_variants:
        if not isinstance(variant, dict):
            raise ValueError("变体记录无效")
        for device in variant.get("devices", []):
            if str(device) in variants_by_device:
                raise ValueError(f"型号属于多个变体：{device}")
            variants_by_device[str(device)] = variant

    profiles = []
    for model in sorted(raw_models, key=lambda row: str(row["id"])):
        if not isinstance(model, dict):
            raise ValueError("型号记录无效")
        device = str(model["id"])
        variant = variants_by_device.get(device)
        if variant is None:
            selected = {
                "profile": None,
                "status": "blocked",
                "reasons": "没有规范化硬件变体",
            }
        else:
            selected = select_profile(model, variant, candidates, compatibility)
        profiles.append(
            {
                "chip": device.lower(),
                "variant": str(variant["id"]) if variant is not None else None,
                "rust_target": model.get("rust_target"),
                **selected,
            }
        )
    return {
        "schema_version": 1,
        "summary": {
            "devices": len(profiles),
            "projected": sum(row["status"] == "projected" for row in profiles),
            "blocked": sum(row["status"] == "blocked" for row in profiles),
        },
        "profiles": profiles,
    }


def _load_candidates(directory: Path) -> list[dict[str, object]]:
    candidates = []
    for path in sorted(directory.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"STM32 chip 不是对象：{path}")
        candidates.append(value)
    if not candidates:
        raise ValueError(f"没有 STM32 chip 候选：{directory}")
    return candidates


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models", type=Path, default=root / "reports/gigadevice-models.json"
    )
    parser.add_argument(
        "--variants",
        type=Path,
        default=root / "reports/gigadevice-merged-firmware-variants.json",
    )
    parser.add_argument(
        "--register-compat",
        type=Path,
        default=root / "reports/gigadevice-stm32-register-compat.json",
    )
    parser.add_argument(
        "--official-chips",
        type=Path,
        default=root / ".cache/research/repos/stm32-data-generated/data/chips",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "reports/gigadevice-embassy-profiles.json",
    )
    args = parser.parse_args()
    report = build_profile_report(
        json.loads(args.models.read_text(encoding="utf-8")),
        json.loads(args.variants.read_text(encoding="utf-8")),
        _load_candidates(args.official_chips),
        json.loads(args.register_compat.read_text(encoding="utf-8")),
    )
    report["provenance"] = {
        "models_sha256": common._sha256(args.models),
        "variants_sha256": common._sha256(args.variants),
        "register_compat_sha256": common._sha256(args.register_compat),
        "official_chips_tree_sha256": common.tree_sha256(args.official_chips),
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(" ".join(f"{key}={value}" for key, value in report["summary"].items()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
