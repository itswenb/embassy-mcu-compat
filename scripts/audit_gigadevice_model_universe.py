#!/usr/bin/env python3
"""审计 GD32 型号全集是否被选型、Pack、Firmware 与 Builder 共同闭包。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import build_gigadevice_mcu_data as mcu_data
import gigadevice_sources as common


def build_report(
    models: dict[str, object],
    pack: dict[str, object],
    builder_models: dict[str, object],
    firmware_registers: dict[str, object],
    builder_firmware: dict[str, object],
    iar: dict[str, object] | None = None,
) -> dict[str, object]:
    model_rows = models["devices"]
    entries = models["catalog_entries"]
    pack_rows = pack["devices"]
    builder_rows = builder_models["devices"]
    libraries = firmware_registers["libraries"]
    plugins = builder_firmware["plugins"]
    iar_rows = [] if iar is None else iar.get("devices")
    if not all(isinstance(value, list) for value in (model_rows, entries, pack_rows, builder_rows, libraries, plugins, iar_rows)):
        raise ValueError("型号全集输入结构无效")
    iar_by_device = {str(row["id"]): row for row in iar_rows}
    if len(iar_by_device) != len(iar_rows):
        raise ValueError("IAR device 重复")

    model_by_id = {str(row["id"]): row for row in model_rows}
    if len(model_by_id) != len(model_rows):
        raise ValueError("规范 device 重复")
    cmsis_to_model = {
        str(cmsis): str(row["id"])
        for row in model_rows
        for cmsis in row.get("cmsis_devices", [])
    }
    pack_devices = {str(row["device"]) for row in pack_rows}
    orphan_pack = sorted(pack_devices - set(cmsis_to_model))
    builder_by_id = {str(row["id"]): row for row in builder_rows}
    missing_builder_rows = sorted(set(model_by_id) - set(builder_by_id))

    official_matches = {
        str(library["series"]): sorted(
            device
            for device in model_by_id
            if mcu_data.series_matches_device(str(library["series"]), device)
        )
        for library in libraries
    }
    builder_matches = {
        str(plugin["id"]): sorted(
            device
            for device in model_by_id
            if mcu_data.series_matches_device(str(plugin["series"]), device)
        )
        for plugin in plugins
    }
    orphan_libraries = sorted(series for series, devices in official_matches.items() if not devices)
    source_only_plugins = sorted(
        plugin for plugin, devices in builder_matches.items() if not devices
    )

    devices = []
    for device, model in sorted(model_by_id.items()):
        cmsis = {str(value) for value in model.get("cmsis_devices", [])}
        sources = []
        if source := model.get("source"):
            sources.append(str(source))
        if cmsis & pack_devices:
            sources.append("cmsis-pack")
        builder = builder_by_id.get(device, {})
        if str(builder.get("evidence", "none")) != "none":
            sources.append("embedded-builder-matrix")
        register_sources = [
            f"firmware:{series}"
            for series, matched in official_matches.items()
            if device in matched
        ] + [
            f"builder-firmware:{plugin}"
            for plugin, matched in builder_matches.items()
            if device in matched
        ]
        if cmsis & pack_devices:
            register_sources.insert(0, "cmsis-pack-svd")
        if device in iar_by_device:
            register_sources.insert(0, f"iar-svd:{iar_by_device[device]['svd']}")
        devices.append(
            {
                "id": device,
                "identification_sources": sources,
                "register_sources": register_sources,
                "accounted": bool(sources or register_sources),
            }
        )

    counts = Counter(str(entry["kind"]) for entry in entries)
    unmatched = builder_models.get("unmatched_matrices", [])
    if not isinstance(unmatched, list):
        raise ValueError("Builder 未匹配矩阵结构无效")
    return {
        "schema_version": 1,
        "summary": {
            "catalog_entries": len(entries),
            "normalized_devices": len(devices),
            "part_numbers": counts["part_number"],
            "part_patterns": counts["part_pattern"],
            "package_patterns": counts["package_pattern"],
            "pack_devices": len(pack_devices),
            "builder_matrices": int(builder_models["summary"]["builder_xml_files"]),
            "official_firmware_libraries": len(libraries),
            "builder_firmware_plugins": len(plugins),
            "orphan_pack_devices": len(orphan_pack),
            "missing_builder_device_rows": len(missing_builder_rows),
            "unmatched_builder_matrices": len(unmatched),
            "orphan_firmware_libraries": len(orphan_libraries),
            "source_only_builder_plugins": len(source_only_plugins),
            "orphan_builder_plugins": 0,
            "unaccounted_devices": sum(not row["accounted"] for row in devices),
            "devices_without_register_source": sum(
                not row["register_sources"] for row in devices
            ),
        },
        "orphan_pack_devices": orphan_pack,
        "missing_builder_device_rows": missing_builder_rows,
        "unmatched_builder_matrices": unmatched,
        "orphan_firmware_libraries": orphan_libraries,
        "source_only_builder_plugins": source_only_plugins,
        "orphan_builder_plugins": [],
        "devices": devices,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in (
        ("models", "reports/gigadevice-models.json"),
        ("pack", "reports/gigadevice-pack-resources.json"),
        ("builder-models", "reports/gigadevice-builder-models.json"),
        ("firmware-registers", "reports/gigadevice-firmware-registers.json"),
        ("builder-firmware", "reports/gigadevice-builder-firmware.json"),
        ("iar", "reports/gigadevice-iar-a7.json"),
    ):
        parser.add_argument(f"--{name}", type=Path, default=repo_root / default)
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-model-universe.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    inputs = [
        args.models,
        args.pack,
        args.builder_models,
        args.firmware_registers,
        args.builder_firmware,
        args.iar,
    ]
    report = build_report(*(json.loads(path.read_text(encoding="utf-8")) for path in inputs))
    report["provenance"] = {path.name: common._sha256(path) for path in inputs}
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    for key in (
        "orphan_pack_devices",
        "missing_builder_device_rows",
        "unmatched_builder_matrices",
        "orphan_firmware_libraries",
        "orphan_builder_plugins",
        "unaccounted_devices",
    ):
        if int(summary[key]) != 0:
            raise ValueError(f"型号全集仍有未闭包来源：{key}")
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"型号全集审计：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
