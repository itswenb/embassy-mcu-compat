#!/usr/bin/env python3
"""把 GD32 选型手册与 CMSIS Pack 归一为 series/device/part_number 关系。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import analyze_gigadevice_builder_models as builder_models
import build_gigadevice_mcu_data as mcu_data
import gigadevice_builder
import gigadevice_sources as common


MODEL_RE = re.compile(r"GD32[A-Za-z0-9]+")
PART_RE = re.compile(r"^(GD32[A-Z0-9]+?)([A-Z]\d)(TR|TA|TB|[A-Z]|\d[A-Z])?$")


def _part_match(part_number: str) -> re.Match[str] | None:
    match = PART_RE.fullmatch(part_number)
    return match if match is not None and len(match.group(1)) >= 10 else None


def device_from_part(part_number: str) -> str | None:
    """移除 GD32 封装/包装后缀，返回代码生成使用的 device 主键。"""
    match = _part_match(part_number)
    return match.group(1) if match is not None else None


def product_part_numbers(report: dict[str, object]) -> set[str]:
    """读取官网产品选择器中的完整料号，并验证其能归一为 A7 device。"""
    products = report.get("products")
    if not isinstance(products, list):
        raise ValueError("产品选择器报告缺少 products")
    parts = []
    for product in products:
        if not isinstance(product, dict):
            raise ValueError("产品选择器条目结构无效")
        part = str(product.get("part_number", "")).upper()
        if not part.startswith("GD32A7") or device_from_part(part) is None:
            raise ValueError(f"产品选择器料号无法归一：{part!r}")
        parts.append(part)
    if len(parts) != len(set(parts)):
        raise ValueError("产品选择器料号重复")
    return set(parts)


def builder_supplemental_models(
    matrices: list[dict[str, object]],
    existing_tokens: set[str],
    authoritative_parts: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    """从 Builder 封装矩阵补齐公开选型清单尚未收录的型号模式。"""
    result: dict[str, dict[str, object]] = {}
    authoritative_parts = authoritative_parts or set()
    for matrix in matrices:
        if bool(matrix["generic_family"]):
            continue
        token = str(matrix["model_pattern"]).upper()
        token = token if token.startswith("GD32") else f"GD32{token}"
        if token in existing_tokens:
            continue
        pattern = re.compile(
            "^"
            + "".join(
                "[A-Z0-9]" if character == "X" else re.escape(character)
                for character in token
            )
            + "$"
        )
        if any(pattern.fullmatch(part) for part in authoritative_parts):
            continue
        device = device_from_part(token)
        kind = "part_pattern"
        if device is None:
            incomplete = re.fullmatch(r"(GD32[A-Z0-9]+[A-Z]\d)[A-Z]", token)
            if incomplete is None:
                continue
            device = incomplete.group(1)
            kind = "package_pattern"
        row = result.setdefault(
            token, {"device": device, "kind": kind, "matrix_paths": []}
        )
        if row["device"] != device or row["kind"] != kind:
            raise ValueError(f"Builder 型号模式归一冲突：{token}")
        paths = row["matrix_paths"]
        assert isinstance(paths, list)
        paths.append(str(matrix["path"]))
    for row in result.values():
        paths = row["matrix_paths"]
        assert isinstance(paths, list)
        paths.sort()
    return result


def apply_builder_targets(
    models: dict[str, object], builder_firmware: dict[str, object]
) -> None:
    raw_devices = models.get("devices")
    plugins = builder_firmware.get("plugins")
    if not isinstance(raw_devices, list) or not isinstance(plugins, list):
        raise ValueError("Builder 目标补充输入结构无效")
    for device in raw_devices:
        assert isinstance(device, dict)
        if device.get("core") is not None and device.get("rust_target") is not None:
            continue
        matches = [
            plugin
            for plugin in plugins
            if isinstance(plugin, dict)
            and mcu_data.series_matches_device(str(plugin["series"]), str(device["id"]))
        ]
        if not matches:
            if device.get("source") == "embedded-builder":
                raise ValueError(f"Builder 新型号无法匹配固件内核：{device['id']}")
            continue
        if len(matches) != 1:
            raise ValueError(f"型号无法唯一匹配 Builder 固件内核：{device['id']}")
        plugin = matches[0]
        if plugin.get("core") is None or plugin.get("rust_target") is None:
            if device.get("source") == "embedded-builder":
                raise ValueError(f"Builder 固件缺少内核/目标：{device['id']}")
            continue
        if device.get("core") not in {None, plugin["core"]} or device.get(
            "rust_target"
        ) not in {None, plugin["rust_target"]}:
            raise ValueError(f"型号与 Builder 固件内核/目标冲突：{device['id']}")
        device["core"] = plugin["core"]
        device["rust_target"] = plugin["rust_target"]


def apply_riscv_targets(
    models: dict[str, object], riscv: dict[str, object]
) -> None:
    raw_devices = models.get("devices")
    libraries = riscv.get("libraries")
    if not isinstance(raw_devices, list) or not isinstance(libraries, list):
        raise ValueError("RISC-V 目标补充输入结构无效")
    for device in raw_devices:
        assert isinstance(device, dict)
        matches = [
            library
            for library in libraries
            if isinstance(library, dict)
            and mcu_data.series_matches_device(str(library["series"]), str(device["id"]))
        ]
        if not matches:
            continue
        if len(matches) != 1:
            raise ValueError(f"RISC-V 型号匹配多个 Firmware 来源：{device['id']}")
        library = matches[0]
        expected = (str(library["isa"]), str(library["rust_target"]))
        actual = (device.get("core"), device.get("rust_target"))
        if any(value is not None for value in actual) and actual != expected:
            raise ValueError(f"RISC-V 型号目标来源冲突：{device['id']}")
        device["core"], device["rust_target"] = expected


def apply_iar_targets(models: dict[str, object], iar: dict[str, object]) -> None:
    raw_devices = models.get("devices")
    iar_devices = iar.get("devices")
    if not isinstance(raw_devices, list) or not isinstance(iar_devices, list):
        raise ValueError("IAR 目标补充输入结构无效")
    by_id = {str(row["id"]): row for row in iar_devices}
    if len(by_id) != len(iar_devices):
        raise ValueError("IAR 目标 device 重复")
    for device in raw_devices:
        assert isinstance(device, dict)
        source = by_id.get(str(device["id"]))
        if source is None:
            continue
        expected = (source.get("core"), source.get("rust_target"))
        if not all(isinstance(value, str) and value for value in expected):
            raise ValueError(f"IAR 型号缺少内核/目标：{device['id']}")
        actual = (device.get("core"), device.get("rust_target"))
        if any(value is not None for value in actual) and actual != expected:
            raise ValueError(f"IAR 型号目标来源冲突：{device['id']}")
        device["core"], device["rust_target"] = expected


def _common_value(records: list[dict[str, object]], key: str) -> object:
    values = {record.get(key) for record in records}
    return values.pop() if len(values) == 1 else None


def _series_matches(token: str, devices: set[str]) -> list[str]:
    prefix = re.split(r"[xX]", token, maxsplit=1)[0]
    return sorted(device for device in devices if device.startswith(prefix))


def normalize_models(
    tokens: set[str],
    records: list[dict[str, object]],
    supplemental_models: dict[str, dict[str, object]] | None = None,
    external_device_sources: dict[str, str] | None = None,
) -> dict[str, object]:
    supplemental_models = supplemental_models or {}
    external_device_sources = external_device_sources or {}
    invalid = sorted(token for token in tokens if MODEL_RE.fullmatch(token) is None)
    if invalid:
        raise ValueError(f"选型手册包含非法型号：{', '.join(invalid)}")

    cmsis_names = [str(record["device"]) for record in records]
    duplicates = sorted(name for name in set(cmsis_names) if cmsis_names.count(name) > 1)
    if duplicates:
        raise ValueError(f"CMSIS device 重复：{', '.join(duplicates)}")

    part_devices = {
        token: device
        for token in tokens
        if (device := device_from_part(token)) is not None
    }
    part_devices.update(
        {token: str(row["device"]) for token, row in supplemental_models.items()}
    )
    records_by_device: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record, cmsis_name in zip(records, cmsis_names, strict=True):
        records_by_device[
            part_devices.get(cmsis_name) or device_from_part(cmsis_name) or cmsis_name
        ].append(record)

    for device in part_devices.values():
        records_by_device.setdefault(device, [])

    devices = set(records_by_device)
    part_numbers: dict[str, list[str]] = defaultdict(list)
    part_patterns: dict[str, list[str]] = defaultdict(list)
    for part_number, device in part_devices.items():
        target = part_patterns if part_number in supplemental_models else part_numbers
        target[device].append(part_number)

    device_rows = []
    for device in sorted(devices):
        source_records = records_by_device[device]
        source_packs = sorted(
            {
                (str(record.get("source_pack_name")), str(record.get("source_pack_version")))
                for record in source_records
            }
        )
        device_rows.append(
            {
                "id": device,
                "feature": device.lower(),
                "support_state": "catalogued",
                "source": (
                    "cmsis-pack"
                    if source_records
                    else "embedded-builder"
                    if part_patterns[device]
                    else external_device_sources.get(device, "selection-guide")
                ),
                "core": _common_value(source_records, "core") if source_records else None,
                "rust_target": (
                    _common_value(source_records, "rust_target") if source_records else None
                ),
                "cmsis_devices": sorted(str(record["device"]) for record in source_records),
                "source_packs": [
                    {"name": name, "version": version} for name, version in source_packs
                ],
                "part_numbers": sorted(part_numbers[device]),
                "part_patterns": sorted(part_patterns[device]),
            }
        )

    entries = []
    for token in sorted(tokens):
        if token in supplemental_models:
            row = supplemental_models[token]
            entries.append(
                {
                    "id": token,
                    "kind": row["kind"],
                    "device": row["device"],
                    "matrix_paths": row["matrix_paths"],
                }
            )
        elif token in part_devices:
            match = _part_match(token)
            assert match is not None
            entries.append(
                {
                    "id": token,
                    "kind": "part_number",
                    "device": part_devices[token],
                    "order_suffix": "".join(value or "" for value in match.groups()[1:]),
                }
            )
        elif token in devices:
            entries.append({"id": token, "kind": "device", "device": token})
        elif matches := _series_matches(token, devices):
            entries.append({"id": token, "kind": "series", "devices": matches})
        else:
            entries.append({"id": token, "kind": "catalog_only"})

    counts = defaultdict(int)
    for entry in entries:
        counts[str(entry["kind"])] += 1
    supplemental = sum(not records_by_device[device] for device in devices)
    return {
        "schema_version": 1,
        "summary": {
            "catalog_entries": len(entries),
            "cmsis_records": len(records),
            "cmsis_devices": len(set(cmsis_names)),
            "normalized_devices": len(devices),
            "supplemental_devices": supplemental,
            "part_numbers": counts["part_number"],
            "part_patterns": counts["part_pattern"],
            "package_patterns": counts["package_pattern"],
            "builder_supplemental_devices": len(
                {str(row["device"]) for row in supplemental_models.values()}
            ),
            "series_entries": counts["series"],
            "device_entries": counts["device"],
            "catalog_only_entries": counts["catalog_only"],
            "unresolved_catalog_entries": 0,
        },
        "devices": device_rows,
        "catalog_entries": entries,
    }


def _read_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog", type=Path, default=repo_root / "reports/gigadevice-catalog.json"
    )
    parser.add_argument(
        "--target-db",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/target-db-v1/latest/devices.jsonl",
    )
    parser.add_argument(
        "--builder-lock",
        type=Path,
        default=repo_root / "sources/gigadevice/builder.lock.json",
    )
    parser.add_argument(
        "--builder-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/builder-resources",
    )
    parser.add_argument(
        "--builder-firmware",
        type=Path,
        default=repo_root / "reports/gigadevice-builder-firmware.json",
    )
    parser.add_argument(
        "--programmer-data",
        type=Path,
        default=repo_root / "reports/gigadevice-programmer-data.json",
    )
    parser.add_argument(
        "--products",
        type=Path,
        default=repo_root / "reports/gigadevice-products.json",
    )
    parser.add_argument(
        "--riscv",
        type=Path,
        default=repo_root / "reports/gigadevice-riscv.json",
    )
    parser.add_argument(
        "--iar", type=Path, default=repo_root / "reports/gigadevice-iar-a7.json"
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-models.json"
    )
    parser.add_argument("--minimum-catalog-entries", type=int, default=782)
    parser.add_argument("--minimum-cmsis-devices", type=int, default=598)
    parser.add_argument("--show-prefix")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    raw_tokens = catalog["all"]
    if not isinstance(raw_tokens, list) or len(raw_tokens) != len(set(raw_tokens)):
        raise ValueError("选型手册型号索引必须是无重复列表")
    tokens = set(map(str, raw_tokens))
    programmer = json.loads(args.programmer_data.read_text(encoding="utf-8"))
    programmer_devices = programmer.get("devices")
    if not isinstance(programmer_devices, list):
        raise ValueError("Programmer 数据缺少 devices")
    programmer_parts = {
        str(part)
        for device in programmer_devices
        if isinstance(device, dict)
        for part in device.get("part_numbers", [])
    }
    product_parts = product_part_numbers(
        json.loads(args.products.read_text(encoding="utf-8"))
    )
    all_tokens = tokens | programmer_parts | product_parts
    lock = json.loads(args.builder_lock.read_text(encoding="utf-8"))
    root = gigadevice_builder.find_extracted_root(
        args.builder_cache, str(lock["builder"]["sha256"])
    )
    records = _read_records(args.target_db)
    matrices = builder_models._matrices(root)
    base = normalize_models(all_tokens, records)
    unmatched = builder_models.build_report(base, matrices)["unmatched_matrices"]
    assert isinstance(unmatched, list)
    supplemental = builder_supplemental_models(
        unmatched,
        all_tokens,
        authoritative_parts=programmer_parts | product_parts,
    )
    record_devices = {str(record["device"]) for record in records}
    catalog_devices = {
        device
        for token in tokens
        if (device := device_from_part(token)) is not None
    } | (tokens & record_devices)
    external_sources = {
        device: "product-selector"
        for part in product_parts
        if (device := device_from_part(part)) is not None
        and device not in catalog_devices
    }
    external_sources.update(
        {
        str(device["id"]): "programmer"
        for device in programmer_devices
        if isinstance(device, dict) and str(device["id"]) not in catalog_devices
        }
    )
    result = normalize_models(
        all_tokens | set(supplemental),
        records,
        supplemental,
        external_device_sources=external_sources,
    )
    apply_builder_targets(
        result, json.loads(args.builder_firmware.read_text(encoding="utf-8"))
    )
    apply_iar_targets(result, json.loads(args.iar.read_text(encoding="utf-8")))
    apply_riscv_targets(result, json.loads(args.riscv.read_text(encoding="utf-8")))
    if args.show_prefix is not None:
        prefix = args.show_prefix.upper()
        print(
            json.dumps(
                {
                    "catalog_tokens": [token for token in raw_tokens if str(token).upper().startswith(prefix)],
                    "devices": [row for row in result["devices"] if str(row["id"]).upper().startswith(prefix)],
                    "catalog_entries": [row for row in result["catalog_entries"] if str(row["id"]).upper().startswith(prefix)],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    summary = result["summary"]
    assert isinstance(summary, dict)
    if int(summary["catalog_entries"]) < args.minimum_catalog_entries:
        raise ValueError("选型手册型号数量低于覆盖门限")
    if int(summary["cmsis_devices"]) < args.minimum_cmsis_devices:
        raise ValueError("CMSIS device 数量低于覆盖门限")
    if int(summary["unresolved_catalog_entries"]) != 0:
        raise ValueError("仍有未分类的选型手册型号")
    common._write_text_atomic(
        args.output, json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"型号报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
