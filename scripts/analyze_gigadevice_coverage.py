#!/usr/bin/env python3
"""汇总 GD32 官方固件、CMSIS Pack、Builder 与选型手册的覆盖证据。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import gigadevice_sources as common
import gigadevice_builder


def classify_catalog_tokens(tokens: set[str], devices: set[str]) -> dict[str, object]:
    exact: list[str] = []
    order_code: list[dict[str, str]] = []
    aggregate: list[str] = []
    unmatched: list[str] = []
    ordered_devices = sorted(devices, key=lambda device: (-len(device), device))
    for token in sorted(tokens):
        if token in devices:
            exact.append(token)
            continue
        prefix_match = next((device for device in ordered_devices if token.startswith(device)), None)
        if prefix_match is not None:
            order_code.append({"token": token, "device": prefix_match})
            continue
        prefix = token.split("x", 1)[0]
        if any(device.startswith(prefix) for device in devices):
            aggregate.append(token)
            continue
        unmatched.append(token)
    return {
        "exact": exact,
        "order_code": order_code,
        "aggregate": aggregate,
        "unmatched": unmatched,
    }


def source_license(text: str) -> str:
    if re.search(r"SPDX-License-Identifier:\s*Apache-2\.0", text, re.IGNORECASE) or re.search(
        r"Apache License,? Version 2\.0", text, re.IGNORECASE
    ):
        return "Apache-2.0"
    if re.search(r"SPDX-License-Identifier:\s*BSD-3-Clause", text, re.IGNORECASE) or (
        "Redistribution and use in source and binary forms" in text
        and "Neither the name" in text
    ):
        return "BSD-3-Clause"
    if re.search(r"SPDX-License-Identifier:\s*MIT", text, re.IGNORECASE):
        return "MIT"
    return "未识别"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _series_from_firmware_filename(filename: str) -> str:
    match = re.fullmatch(r"(GD32.+)_Firmware_Library_V\d+\.\d+\.\d+\.7z", filename)
    if match is None:
        raise ValueError(f"无法从固件文件名识别系列：{filename}")
    return match.group(1)


def _firmware_evidence(lock: dict[str, object], root: Path) -> list[dict[str, object]]:
    evidence = []
    for item in lock["firmware"]:  # type: ignore[index]
        assert isinstance(item, dict)
        filename = str(item["filename"])
        source_root = root / filename.removesuffix(".7z")
        if not (source_root / ".source.json").is_file():
            raise ValueError(f"固件尚未由脚本解包：{source_root}")
        files = [
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".c", ".h"}
            and any(part.casefold() == "firmware" for part in path.parts)
        ]
        licenses: Counter[str] = Counter()
        dmamux_requests = False
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            licenses[source_license(text[:16384])] += 1
            if re.search(r"DMAMUX.{0,80}(?:REQUEST|REQ)", text, re.IGNORECASE | re.DOTALL):
                dmamux_requests = True
        permissive = sum(licenses[name] for name in ("Apache-2.0", "BSD-3-Clause", "MIT"))
        evidence.append(
            {
                "series": _series_from_firmware_filename(filename),
                "version": item["version"],
                "document_id": item["document_id"],
                "source_files": len(files),
                "permissively_licensed_files": permissive,
                "unidentified_license_files": licenses["未识别"],
                "licenses": dict(sorted(licenses.items())),
                "has_dmamux_request_definitions": dmamux_requests,
            }
        )
    return sorted(evidence, key=lambda item: str(item["series"]).casefold())


def _addon_evidence(lock: dict[str, object], root: Path) -> list[dict[str, object]]:
    evidence = []
    for item in lock["addons"]:  # type: ignore[index]
        assert isinstance(item, dict)
        source_root = root / str(item["filename"]).removesuffix(".7z")
        inventory_path = source_root / "inventory.json"
        if not inventory_path.is_file():
            raise ValueError(f"AddOn 尚未由脚本解包：{source_root}")
        inventory = _read_json(inventory_path)
        packs = inventory["packs"]
        assert isinstance(packs, list)
        svd_files = [
            path for path in source_root.rglob("*") if path.is_file() and path.suffix.casefold() == ".svd"
        ]
        evidence.append(
            {
                "name": item["name"],
                "version": item["version"],
                "document_id": item["document_id"],
                "cmsis_pack_count": inventory["cmsis_pack_count"],
                "svd_file_count": len(svd_files),
                "packs": [
                    {
                        "filename": pack["filename"],
                        "pdsc": pack["pdsc"],
                        "sha256": pack["sha256"],
                    }
                    for pack in packs
                ],
            }
        )
    return sorted(evidence, key=lambda item: str(item["name"]).casefold())


def _tag_count(path: Path, name: str) -> int:
    root = ET.parse(path).getroot()
    return sum(1 for element in root.iter() if element.tag.rsplit("}", 1)[-1] == name)


def _builder_evidence(lock: dict[str, object], cache: Path) -> dict[str, object]:
    builder = lock["builder"]
    assert isinstance(builder, dict)
    root = gigadevice_builder.find_extracted_root(cache, str(builder["sha256"]))
    afio_groups: dict[str, dict[str, int]] = defaultdict(lambda: {"xml_files": 0, "pin_routes": 0})
    datasheet_groups: dict[str, dict[str, int]] = defaultdict(
        lambda: {"xml_files": 0, "pin_pads": 0}
    )
    for path in sorted((root / "AFIO").rglob("*.xml")):
        relative = path.relative_to(root / "AFIO")
        group = relative.parts[0].removesuffix(".xml")
        afio_groups[group]["xml_files"] += 1
        afio_groups[group]["pin_routes"] += _tag_count(path, "Pin")
    for path in sorted((root / "DataSheet").rglob("*.xml")):
        relative = path.relative_to(root / "DataSheet")
        group = relative.parts[0]
        datasheet_groups[group]["xml_files"] += 1
        datasheet_groups[group]["pin_pads"] += _tag_count(path, "PinPad")
    return {
        "version": builder["version"],
        "document_id": builder["document_id"],
        "archive_sha256": builder["sha256"],
        "afio_xml_count": sum(item["xml_files"] for item in afio_groups.values()),
        "datasheet_xml_count": sum(item["xml_files"] for item in datasheet_groups.values()),
        "afio_groups": dict(sorted(afio_groups.items())),
        "datasheet_groups": dict(sorted(datasheet_groups.items())),
    }


def _load_target_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _target_evidence(records: list[dict[str, object]]) -> dict[str, object]:
    packs: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        key = (str(record["source_pack_name"]), str(record["source_pack_version"]))
        item = packs.setdefault(key, {"devices": 0, "cores": Counter(), "rust_targets": Counter()})
        item["devices"] = int(item["devices"]) + 1
        item["cores"][str(record.get("core"))] += 1  # type: ignore[index]
        item["rust_targets"][str(record.get("rust_target"))] += 1  # type: ignore[index]
    return {
        "record_count": len(records),
        "unique_device_count": len({str(record["device"]) for record in records}),
        "core_counts": dict(sorted(Counter(str(record.get("core")) for record in records).items())),
        "rust_target_counts": dict(
            sorted(Counter(str(record.get("rust_target")) for record in records).items())
        ),
        "packs": [
            {
                "name": name,
                "version": version,
                "devices": item["devices"],
                "cores": dict(sorted(item["cores"].items())),  # type: ignore[union-attr]
                "rust_targets": dict(sorted(item["rust_targets"].items())),  # type: ignore[union-attr]
            }
            for (name, version), item in sorted(packs.items())
        ],
    }


def build_report(args: argparse.Namespace) -> dict[str, object]:
    firmware_lock = _read_json(args.firmware_lock)
    addon_lock = _read_json(args.addon_lock)
    builder_lock = _read_json(args.builder_lock)
    catalog = _read_json(args.catalog)
    target_records = _load_target_records(args.target_db)
    firmware = _firmware_evidence(firmware_lock, args.firmware_root)
    addons = _addon_evidence(addon_lock, args.addon_root)
    builder = _builder_evidence(builder_lock, args.builder_cache)
    tokens = set(map(str, catalog["all"]))  # type: ignore[arg-type]
    devices = {str(record["device"]) for record in target_records}
    mapping = classify_catalog_tokens(tokens, devices)
    unmatched = mapping["unmatched"]
    assert isinstance(unmatched, list)
    non_arm_series = sorted(
        item["series"]
        for item in firmware
        if str(item["series"]).startswith(("GD32VF", "GD32VW"))
    )
    return {
        "schema_version": 1,
        "summary": {
            "firmware_libraries": len(firmware),
            "addons": len(addons),
            "addons_with_cmsis_pack": sum(int(item["cmsis_pack_count"]) > 0 for item in addons),
            "latest_cmsis_packs": len({str(record["source_pack_name"]) for record in target_records}),
            "cmsis_svd_files": sum(int(item["svd_file_count"]) for item in addons),
            "cmsis_devices": len(devices),
            "selection_guide_tokens": len(tokens),
            "builder_afio_xml": builder["afio_xml_count"],
            "builder_datasheet_xml": builder["datasheet_xml_count"],
            "selection_tokens_unmatched_by_cmsis": len(unmatched),
        },
        "firmware": firmware,
        "addons": addons,
        "cmsis": _target_evidence(target_records),
        "builder": builder,
        "catalog_mapping": mapping,
        "architecture_boundary": {
            "non_arm_firmware_series": non_arm_series,
            "all_gd32_via_unmodified_embassy_stm32_metadata_only": False,
            "evidence": "GD32VF/GD32VW 属于非 Cortex-M 系列，而 embassy-stm32 的执行架构绑定 Cortex-M。",
        },
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-lock", type=Path, default=repo_root / "sources/gigadevice/firmware.lock.json")
    parser.add_argument(
        "--firmware-root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/firmware-sources-v1",
    )
    parser.add_argument("--addon-lock", type=Path, default=repo_root / "sources/gigadevice/addons.lock.json")
    parser.add_argument(
        "--addon-root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/addon-packs-v1",
    )
    parser.add_argument("--builder-lock", type=Path, default=repo_root / "sources/gigadevice/builder.lock.json")
    parser.add_argument(
        "--builder-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/builder-resources",
    )
    parser.add_argument("--catalog", type=Path, default=repo_root / "reports/gigadevice-catalog.json")
    parser.add_argument(
        "--target-db",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/target-db-v1/latest/devices.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-source-coverage.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_report(args)
    summary = report["summary"]
    assert isinstance(summary, dict)
    expected = {
        "firmware_libraries": 33,
        "addons": 30,
        "addons_with_cmsis_pack": 29,
        "latest_cmsis_packs": 29,
        "cmsis_devices": 598,
        "selection_guide_tokens": 704,
    }
    for key, minimum in expected.items():
        if int(summary[key]) < minimum:
            raise ValueError(f"覆盖回退：{key}={summary[key]}，最低要求为 {minimum}")
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"覆盖报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
