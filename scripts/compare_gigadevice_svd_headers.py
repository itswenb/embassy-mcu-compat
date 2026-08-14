#!/usr/bin/env python3
"""把 GD32 Pack SVD 与宽松许可 Firmware 头文件做数值交叉校验。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import audit_gigadevice_svds as svd_audit
import gigadevice_sources as common


PACK_SERIES_ALIASES = {"GD32H7xx": "GD32H73x_75x"}
BASE_SUFFIX_RE = re.compile(r"_BASE(?:_[A-Z0-9]+)?$")
CONFLICT_FIELDS = (
    "missing_svd_interrupts",
    "interrupt_name_conflicts",
    "named_base_conflicts",
)


def firmware_series_for_pack(pack_name: str) -> str:
    if not pack_name.endswith("_DFP"):
        raise ValueError(f"CMSIS Pack 名称缺少 _DFP 后缀：{pack_name}")
    series = pack_name.removesuffix("_DFP")
    return PACK_SERIES_ALIASES.get(series, series)


def _header_bases_by_name(base_addresses: dict[str, object]) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for raw_name, raw_value in base_addresses.items():
        name = BASE_SUFFIX_RE.sub("", raw_name)
        if name == raw_name:
            continue
        result.setdefault(name, set()).add(int(raw_value))
    return result


def _register_instances(library: dict[str, object]) -> dict[str, list[int]]:
    result: dict[str, set[int]] = {}
    headers = library.get("register_headers")
    if not isinstance(headers, list):
        raise ValueError("Firmware 寄存器报告缺少 register_headers")
    for header in headers:
        if not isinstance(header, dict) or not isinstance(header.get("instances"), dict):
            raise ValueError("Firmware 寄存器报告包含非法 instances")
        for name, value in header["instances"].items():
            result.setdefault(str(name), set()).add(int(value))
    return {name: sorted(values) for name, values in sorted(result.items())}


def compare_facts(
    svd: dict[str, object],
    header: dict[str, object],
    register_instances: dict[str, list[int]] | None = None,
) -> dict[str, object]:
    raw_svd_interrupts = svd["interrupts"]
    raw_header_interrupts = header["interrupts"]
    raw_svd_bases = svd["peripheral_base_addresses"]
    raw_header_bases = header["base_addresses"]
    assert isinstance(raw_svd_interrupts, list)
    assert isinstance(raw_header_interrupts, list)
    assert isinstance(raw_svd_bases, dict)
    assert isinstance(raw_header_bases, dict)

    svd_interrupt_values = {int(item["value"]) for item in raw_svd_interrupts}
    header_interrupt_values = {
        int(item["value"]) for item in raw_header_interrupts if int(item["value"]) >= 0
    }
    header_interrupts_by_name = {
        str(item["name"]): int(item["value"])
        for item in raw_header_interrupts
        if int(item["value"]) >= 0
    }
    interrupt_name_conflicts = []
    for item in raw_svd_interrupts:
        name = str(item["name"])
        value = int(item["value"])
        if name in header_interrupts_by_name and header_interrupts_by_name[name] != value:
            interrupt_name_conflicts.append(
                {"name": name, "svd": value, "header": header_interrupts_by_name[name]}
            )

    header_bases = _header_bases_by_name(raw_header_bases)
    named_base_matches = []
    named_base_conflicts = []
    for name, raw_value in sorted(raw_svd_bases.items()):
        value = int(raw_value)
        if name not in header_bases:
            continue
        values = header_bases[name]
        if value in values:
            named_base_matches.append(name)
        else:
            header_value: int | list[int]
            ordered = sorted(values)
            header_value = ordered[0] if len(ordered) == 1 else ordered
            named_base_conflicts.append(
                {"name": name, "svd": value, "header": header_value}
            )

    svd_base_values = {int(value) for value in raw_svd_bases.values()}
    header_base_values = {int(value) for value in raw_header_bases.values()}
    instances = {
        name: {int(value) for value in values}
        for name, values in (register_instances or {}).items()
    }
    named_instance_matches = []
    named_instance_conflicts = []
    for name, raw_value in sorted(raw_svd_bases.items()):
        if name not in instances:
            continue
        value = int(raw_value)
        if value in instances[name]:
            named_instance_matches.append(name)
        else:
            ordered = sorted(instances[name])
            firmware_value: int | list[int] = (
                ordered[0] if len(ordered) == 1 else ordered
            )
            named_instance_conflicts.append(
                {"name": name, "svd": value, "firmware": firmware_value}
            )
    instance_values = {value for values in instances.values() for value in values}
    missing_values = svd_interrupt_values - header_interrupt_values
    return {
        "missing_svd_interrupt_values": sorted(missing_values),
        "missing_svd_interrupts": sorted(
            (
                {"name": str(item["name"]), "value": int(item["value"])}
                for item in raw_svd_interrupts
                if int(item["value"]) in missing_values
            ),
            key=lambda item: (int(item["value"]), str(item["name"])),
        ),
        "interrupt_name_conflicts": interrupt_name_conflicts,
        "named_base_matches": named_base_matches,
        "named_base_conflicts": named_base_conflicts,
        "shared_base_address_values": len(svd_base_values & header_base_values),
        "named_instance_matches": named_instance_matches,
        "named_instance_conflicts": named_instance_conflicts,
        "shared_instance_address_values": len(svd_base_values & instance_values),
    }


def classify_conflict(
    comparison: dict[str, object], expected: dict[str, object] | None
) -> str:
    has_conflict = any(comparison.get(field, []) for field in CONFLICT_FIELDS)
    if not has_conflict:
        return "resolved" if expected is not None else "none"
    if expected is None or expected.get("resolution") not in {"block", "prefer-pack-svd"}:
        return "unexpected"
    for field in ("svd_sha256", "firmware_header_sha256", *CONFLICT_FIELDS):
        default: object = [] if field in CONFLICT_FIELDS else None
        if comparison.get(field, default) != expected.get(field, default):
            return "unexpected"
    return (
        "known-blocking"
        if expected.get("resolution") == "block"
        else "source-resolved"
    )


def _known_conflicts(data: dict[str, object]) -> dict[str, dict[str, object]]:
    if data.get("schema_version") != 1 or not isinstance(data.get("conflicts"), list):
        raise ValueError("已知 SVD/Firmware 冲突清单格式无效")
    result = {}
    for raw in data["conflicts"]:
        if not isinstance(raw, dict):
            raise ValueError("已知冲突清单包含非法条目")
        sha256 = str(raw.get("svd_sha256", ""))
        if re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise ValueError("已知冲突缺少有效 SVD SHA-256")
        if sha256 in result:
            raise ValueError(f"已知冲突重复 SVD SHA-256：{sha256}")
        if raw.get("resolution") not in {"block", "prefer-pack-svd"} or not str(raw.get("reason", "")).strip():
            raise ValueError(f"已知冲突 {sha256} 必须明确标记解决策略和原因")
        result[sha256] = raw
    return result


def svd_facts(data: bytes) -> dict[str, object]:
    root = ET.fromstring(data)
    peripherals: dict[str, int] = {}
    interrupts: dict[tuple[str, int], None] = {}
    for peripheral in root.iter():
        if svd_audit._tag(peripheral) != "peripheral":
            continue
        name = svd_audit._child_text(peripheral, "name")
        address = svd_audit._child_text(peripheral, "baseAddress")
        if name is None or address is None:
            raise ValueError("规范化 SVD peripheral 缺少 name/baseAddress")
        peripherals[name] = int(address, 0)
        for interrupt in peripheral:
            if svd_audit._tag(interrupt) != "interrupt":
                continue
            interrupt_name = svd_audit._child_text(interrupt, "name")
            value = svd_audit._child_text(interrupt, "value")
            if interrupt_name is None or value is None:
                raise ValueError("规范化 SVD interrupt 缺少 name/value")
            interrupts[(interrupt_name, int(value, 0))] = None
    return {
        "peripheral_base_addresses": dict(sorted(peripherals.items())),
        "interrupts": [
            {"name": name, "value": value}
            for name, value in sorted(interrupts, key=lambda item: (item[1], item[0]))
        ],
    }


def build_report(
    resources: dict[str, object],
    firmware: dict[str, object],
    registers: dict[str, object],
    known_conflicts: dict[str, object],
    pdsc_root: Path,
) -> dict[str, object]:
    raw_entries = resources.get("svd_files")
    raw_libraries = firmware.get("libraries")
    raw_register_libraries = registers.get("libraries")
    if (
        not isinstance(raw_entries, list)
        or not isinstance(raw_libraries, list)
        or not isinstance(raw_register_libraries, list)
    ):
        raise ValueError("输入报告缺少 svd_files 或 libraries")
    libraries = {str(item["series"]): item for item in raw_libraries}
    if len(libraries) != len(raw_libraries):
        raise ValueError("Firmware 报告包含重复系列")
    register_libraries = {
        str(item["series"]): item for item in raw_register_libraries
    }
    if len(register_libraries) != len(raw_register_libraries):
        raise ValueError("Firmware 寄存器报告包含重复系列")
    expected_conflicts = _known_conflicts(known_conflicts)
    seen_conflicts = set()

    rows = []
    for raw_entry in raw_entries:
        assert isinstance(raw_entry, dict)
        pack_name = str(raw_entry["source_pack_name"])
        series = firmware_series_for_pack(pack_name)
        library = libraries.get(series)
        if library is None:
            rows.append(
                {
                    "source_pack_name": pack_name,
                    "path": raw_entry["path"],
                    "status": "missing-firmware-library",
                    "firmware_series": series,
                }
            )
            continue
        register_library = register_libraries.get(series)
        if register_library is None:
            rows.append(
                {
                    "source_pack_name": pack_name,
                    "path": raw_entry["path"],
                    "status": "missing-firmware-register-library",
                    "firmware_series": series,
                }
            )
            continue
        headers = library["device_headers"]
        if not isinstance(headers, list) or len(headers) != 1:
            raise ValueError(f"Firmware 系列 {series} 必须恰有一个器件头文件")
        header = headers[0]
        assert isinstance(header, dict)
        if header.get("publishable") is not True:
            raise ValueError(f"Firmware 系列 {series} 的器件头文件许可不允许发布派生事实")
        if (
            register_library.get("archive_sha256") != library.get("archive_sha256")
            or register_library.get("device_header_sha256") != header.get("sha256")
        ):
            raise ValueError(f"Firmware 系列 {series} 的寄存器报告来源哈希不一致")
        instances = _register_instances(register_library)
        source = svd_audit._source_path(pdsc_root, raw_entry)
        normalized, _ = svd_audit.normalized_svd_bytes(source)
        facts = svd_facts(normalized)
        comparison = compare_facts(facts, header, instances)
        expected = expected_conflicts.get(str(raw_entry["sha256"]))
        conflict_status = classify_conflict(
            {
                "svd_sha256": raw_entry["sha256"],
                "firmware_header_sha256": header["sha256"],
                **comparison,
            },
            expected,
        )
        if expected is not None:
            seen_conflicts.add(str(raw_entry["sha256"]))
        rows.append(
            {
                "source_pack_name": pack_name,
                "source_pack_version": raw_entry["source_pack_version"],
                "path": raw_entry["path"],
                "svd_sha256": raw_entry["sha256"],
                "firmware_series": series,
                "firmware_header_path": header["path"],
                "firmware_header_sha256": header["sha256"],
                "firmware_register_tree_sha256": register_library["tree_sha256"],
                "firmware_instance_names": len(instances),
                "status": "compared",
                "conflict_status": conflict_status,
                "svd_peripherals": len(facts["peripheral_base_addresses"]),
                "svd_interrupt_values": len(
                    {int(item["value"]) for item in facts["interrupts"]}
                ),
                **comparison,
            }
        )

    compared = [row for row in rows if row["status"] == "compared"]
    stale = (set(expected_conflicts) - seen_conflicts) | {
        str(row["svd_sha256"])
        for row in compared
        if row["conflict_status"] == "resolved"
    }
    return {
        "schema_version": 1,
        "summary": {
            "svd_files": len(rows),
            "compared": len(compared),
            "missing_firmware_libraries": sum(
                row["status"] == "missing-firmware-library" for row in rows
            ),
            "missing_firmware_register_libraries": sum(
                row["status"] == "missing-firmware-register-library" for row in rows
            ),
            "svds_with_missing_interrupt_values": sum(
                bool(row["missing_svd_interrupt_values"]) for row in compared
            ),
            "interrupt_name_conflicts": sum(
                len(row["interrupt_name_conflicts"]) for row in compared
            ),
            "named_base_conflicts": sum(
                len(row["named_base_conflicts"]) for row in compared
            ),
            "named_instance_matches": sum(
                len(row["named_instance_matches"]) for row in compared
            ),
            "named_instance_conflicts": sum(
                len(row["named_instance_conflicts"]) for row in compared
            ),
            "known_blocking_conflicts": sum(
                row["conflict_status"] == "known-blocking" for row in compared
            ),
            "source_resolved_conflicts": sum(
                row["conflict_status"] == "source-resolved" for row in compared
            ),
            "unexpected_conflicts": sum(
                row["conflict_status"] == "unexpected" for row in compared
            ),
            "stale_known_conflicts": len(stale),
        },
        "stale_known_conflict_sha256": sorted(stale),
        "comparisons": rows,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resources",
        type=Path,
        default=repo_root / "reports/gigadevice-pack-resources.json",
    )
    parser.add_argument(
        "--firmware",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-headers.json",
    )
    parser.add_argument(
        "--registers",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-registers.json",
    )
    parser.add_argument(
        "--known-conflicts",
        type=Path,
        default=repo_root / "sources/gigadevice/svd-header-conflicts.json",
    )
    parser.add_argument(
        "--pdsc-root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/addon-packs-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-svd-header-comparison.json",
    )
    parser.add_argument("--minimum-svd-files", type=int, default=43)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_report(
        json.loads(args.resources.read_text(encoding="utf-8")),
        json.loads(args.firmware.read_text(encoding="utf-8")),
        json.loads(args.registers.read_text(encoding="utf-8")),
        json.loads(args.known_conflicts.read_text(encoding="utf-8")),
        args.pdsc_root,
    )
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"SVD/Firmware 对照报告：{args.output}")
    if int(summary["svd_files"]) < args.minimum_svd_files:
        raise ValueError("SVD 数量低于交叉校验门限")
    for key in (
        "missing_firmware_libraries",
        "missing_firmware_register_libraries",
        "named_instance_conflicts",
        "unexpected_conflicts",
        "stale_known_conflicts",
    ):
        if int(summary[key]) != 0:
            raise ValueError(f"SVD/Firmware 交叉校验未通过：{key}={summary[key]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
