#!/usr/bin/env python3
"""合并独立 Firmware 与 Embedded Builder 的器件变体。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import gigadevice_sources as common


def _by_device(
    report: dict[str, object], aliases: dict[str, str]
) -> dict[str, dict[str, object]]:
    result = {}
    for variant in report["variants"]:
        assert isinstance(variant, dict)
        for device in variant["devices"]:
            raw_name = str(device)
            if raw_name not in aliases:
                raise ValueError(f"Firmware 变体 device 不在规范型号中：{raw_name}")
            name = aliases[raw_name]
            if name in result and result[name] is not variant:
                raise ValueError(f"同一来源包含重复 Firmware device：{name}")
            result[name] = variant
    return result


def _resource_signature(record: dict[str, object]) -> tuple[object, ...] | None:
    compile_entries = []
    for entry in record.get("compile", []):
        file = entry.get("file")
        if not isinstance(file, dict) or not file.get("sha256"):
            return None
        compile_entries.append(
            (tuple(sorted(str(entry.get("define", "")).split())), str(file["sha256"]))
        )
    debug_entries = []
    for entry in record.get("debug", []):
        file = entry.get("file")
        if not isinstance(file, dict) or not file.get("sha256"):
            return None
        debug_entries.append(str(file["sha256"]))
    if not compile_entries or not debug_entries:
        return None
    return tuple(sorted(compile_entries)), tuple(sorted(debug_entries))


def _resource_candidates(
    resources: dict[str, object],
    aliases: dict[str, str],
    sources: dict[str, dict[str, dict[str, object]]],
) -> tuple[dict[str, tuple[object, ...]], dict[tuple[object, ...], list[tuple[str, dict[str, object]]]]]:
    signatures = {}
    for record in resources.get("devices", []):
        raw_device = str(record["device"])
        if raw_device not in aliases:
            continue
        signature = _resource_signature(record)
        if signature is None:
            continue
        device = aliases[raw_device]
        if device in signatures and signatures[device] != signature:
            raise ValueError(f"CMSIS Pack 型号存在冲突资源签名：{device}")
        signatures[device] = signature

    candidates: dict[tuple[object, ...], dict[tuple[str, str], dict[str, object]]] = defaultdict(dict)
    for source, variants_by_device in sources.items():
        for device, variant in variants_by_device.items():
            signature = signatures.get(device)
            if signature is None:
                continue
            compile_defines = {entry[0] for entry in signature[0]}
            variant_defines = tuple(sorted(map(str, variant.get("defines", []))))
            if variant_defines and variant_defines not in compile_defines:
                continue
            candidates[signature][(source, str(variant["id"]))] = variant
    return signatures, {
        signature: [(source, variant) for (source, _), variant in values.items()]
        for signature, values in candidates.items()
    }


def merge_reports(
    models: dict[str, object],
    official: dict[str, object],
    builder: dict[str, object],
    resources: dict[str, object],
) -> dict[str, object]:
    raw_models = models["devices"]
    if not isinstance(raw_models, list):
        raise ValueError("规范型号报告缺少 devices")
    aliases = {
        alias: str(model["id"])
        for model in raw_models
        for alias in [str(model["id"]), *map(str, model.get("cmsis_devices", []))]
    }
    official_by_device = _by_device(official, aliases)
    builder_by_device = _by_device(builder, aliases)
    resource_signatures, resource_candidates = _resource_candidates(
        resources,
        aliases,
        {"official": official_by_device, "builder": builder_by_device},
    )
    selected: dict[tuple[str, str], list[str]] = defaultdict(list)
    inferred: dict[tuple[str, str], list[str]] = defaultdict(list)
    source_reports = {"official": official, "builder": builder}
    missing = []
    for model in raw_models:
        assert isinstance(model, dict)
        device = str(model["id"])
        if str(model.get("source")) == "embedded-builder" and device in builder_by_device:
            source, variant = "builder", builder_by_device[device]
        elif device in official_by_device:
            source, variant = "official", official_by_device[device]
        elif device in builder_by_device:
            source, variant = "builder", builder_by_device[device]
        else:
            candidates = resource_candidates.get(resource_signatures.get(device), [])
            official_candidates = [
                candidate for candidate in candidates if candidate[0] == "official"
            ]
            if official_candidates:
                candidates = official_candidates
            if len(candidates) != 1:
                missing.append({"device": device, "reason": "firmware-source-missing"})
                continue
            source, variant = candidates[0]
            inferred[(source, str(variant["id"]))].append(device)
        selected[(source, str(variant["id"]))].append(device)

    variants = []
    for (source, variant_id), devices in sorted(selected.items()):
        source_variant = next(
            variant
            for variant in source_reports[source]["variants"]
            if str(variant["id"]) == variant_id
        )
        assert isinstance(source_variant, dict)
        variants.append(
            {
                **source_variant,
                "id": f"{source}-{variant_id}",
                "source_kind": source,
                "source_variant_id": variant_id,
                "devices": sorted(devices),
                "inferred_devices": sorted(inferred[(source, variant_id)]),
            }
        )
    selected_devices = {device for variant in variants for device in variant["devices"]}
    missing_devices = {row["device"] for row in missing}
    expected_devices = {str(model["id"]) for model in raw_models}
    if selected_devices & missing_devices or selected_devices | missing_devices != expected_devices:
        raise ValueError("合并 Firmware 变体未闭包全部规范 device")
    missing.sort(key=lambda row: str(row["device"]))
    return {
        "schema_version": 1,
        "summary": {
            "normalized_devices": len(raw_models),
            "variants": len(variants),
            "devices": len(selected_devices),
            "missing_devices": len(missing),
            "official_devices": sum(
                len(variant["devices"])
                for variant in variants
                if variant["source_kind"] == "official"
            ),
            "builder_devices": sum(
                len(variant["devices"])
                for variant in variants
                if variant["source_kind"] == "builder"
            ),
            "inferred_devices": sum(len(devices) for devices in inferred.values()),
            "variants_with_rcu": sum(
                variant.get("rcu") is not None for variant in variants
            ),
        },
        "missing_devices": missing,
        "variants": variants,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=repo_root / "reports/gigadevice-models.json")
    parser.add_argument(
        "--official",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-variants.json",
    )
    parser.add_argument(
        "--builder",
        type=Path,
        default=repo_root / "reports/gigadevice-builder-variants.json",
    )
    parser.add_argument(
        "--resources",
        type=Path,
        default=repo_root / "reports/gigadevice-pack-resources.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-merged-firmware-variants.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    inputs = [args.models, args.official, args.builder, args.resources]
    report = merge_reports(
        *(json.loads(path.read_text(encoding="utf-8")) for path in inputs)
    )
    report["provenance"] = {path.name: common._sha256(path) for path in inputs}
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    if int(summary["devices"]) + int(summary["missing_devices"]) != int(
        summary["normalized_devices"]
    ):
        raise ValueError("合并 Firmware 变体与缺口未闭合全部规范设备")
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"合并 Firmware 变体：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
