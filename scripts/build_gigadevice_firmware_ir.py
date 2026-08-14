#!/usr/bin/env python3
"""把宽松许可 Firmware 寄存器事实归一为可审计的外设布局 IR。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import gigadevice_sources as common


def _register_family(name: object) -> str:
    return re.sub(r"\d+", "", str(name)).casefold()


def infer_array_bounds(
    registers: list[dict[str, object]],
) -> list[dict[str, object]]:
    result = [
        {
            **register,
            **(
                {
                    "array_parameters": {
                        parameter: dict(bounds)
                        for parameter, bounds in register["array_parameters"].items()
                    }
                }
                if register.get("array_parameters")
                else {}
            ),
        }
        for register in registers
    ]
    scalars = [register for register in result if not register.get("array_parameters")]
    for register in result:
        parameters = register.get("array_parameters")
        if not isinstance(parameters, dict) or len(parameters) != 1:
            continue
        bounds = next(iter(parameters.values()))
        if "end" in bounds or "indices" in bounds:
            if "indices" in bounds:
                continue
            count = int(bounds["end"]) - int(bounds["start"]) + 1
            offsets = sorted(
                {
                    int(candidate["offset"])
                    for candidate in scalars
                    if int(candidate["width"]) == int(register["width"])
                    and _register_family(candidate["name"])
                    == _register_family(register["name"])
                }
            )
            stride = int(bounds["stride"])
            if (
                bounds.get("bound_evidence") == "source-comment-range"
                and len(offsets) == count
                and all(
                    right - left == stride
                    for left, right in zip(offsets, offsets[1:])
                )
            ):
                register["offset"] = offsets[0]
                bounds["address_evidence"] = "scalar-register-sequence"
            continue
        start = int(bounds["start"])
        stride = int(bounds["stride"])
        if register["name"] == "OP_BYTE":
            block_offsets = sorted(
                {
                    int(candidate["offset"])
                    for candidate in scalars
                    if int(candidate["width"]) == int(register["width"])
                }
            )
            expected = (
                list(range(block_offsets[0], block_offsets[-1] + stride, stride))
                if block_offsets
                else []
            )
            if (
                len(block_offsets) > 1
                and block_offsets == expected
                and block_offsets[0] == int(register["offset"])
            ):
                bounds.update(
                    {
                        "end": start + len(block_offsets) - 1,
                        "bound_evidence": "scalar-block-sequence",
                    }
                )
                continue
        family = _register_family(register["name"])
        offsets = {
            int(candidate["offset"])
            for candidate in scalars
            if int(candidate["width"]) == int(register["width"])
            and _register_family(candidate["name"]) == family
        }
        end = start - 1
        for index in range(start, start + 512):
            offset = int(register["offset"]) + (index - start) * stride
            if offset not in offsets:
                break
            end = index
        if end > start:
            bounds.update(
                {
                    "end": end,
                    "bound_evidence": "scalar-register-sequence",
                }
            )
            continue
        sorted_offsets = sorted(offsets)
        indices = [
            start + (offset - int(register["offset"])) // stride
            for offset in sorted_offsets
            if (offset - int(register["offset"])) % stride == 0
        ]
        if (
            len(sorted_offsets) > 1
            and len(indices) == len(sorted_offsets)
            and indices == list(range(indices[0], indices[-1] + 1))
            and indices[0] >= 0
        ):
            register["offset"] = sorted_offsets[0]
            bounds.update(
                {
                    "start": indices[0],
                    "end": indices[-1],
                    "bound_evidence": "scalar-register-sequence",
                }
            )
    return result


def _source(header: dict[str, object]) -> dict[str, str]:
    return {"path": str(header["path"]), "sha256": str(header["sha256"])}


def _layout(header: dict[str, object], block: str) -> dict[str, object]:
    register_blocks = header["register_blocks"]
    assert isinstance(register_blocks, dict)
    registers = [
        {key: value for key, value in row.items() if key != "owner"}
        for row in header["registers"]
        if register_blocks.get(str(row["name"])) == block
    ]
    registers = infer_array_bounds(registers)
    registers.sort(
        key=lambda row: (str(row["name"]), int(row["offset"]), int(row["width"]))
    )
    register_names = {str(row["name"]) for row in registers}
    fields = [
        dict(row)
        for row in header["fields"]
        if str(row["register"]) in register_names
    ]
    fields.sort(key=lambda row: (str(row["register"]), str(row["name"])))
    facts = {"block": block, "registers": registers, "fields": fields}
    encoded = json.dumps(
        facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    return {"id": f"{block.lower()}-{sha256[:16]}", "sha256": sha256, **facts}


def _merge_fragments(
    fragment_ids: set[str], fragments: dict[str, dict[str, object]]
) -> tuple[dict[str, object] | None, dict[str, list[str]]]:
    selected = [fragments[fragment_id] for fragment_id in sorted(fragment_ids)]
    blocks = {str(fragment["block"]) for fragment in selected}
    conflicts: dict[str, list[str]] = {}
    if len(blocks) != 1:
        conflicts["blocks"] = sorted(blocks)
        return None, conflicts

    register_variants: dict[str, dict[str, dict[str, object]]] = {}
    field_variants: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for fragment in selected:
        for row in fragment["registers"]:
            encoded = json.dumps(row, ensure_ascii=False, sort_keys=True)
            register_variants.setdefault(str(row["name"]), {})[encoded] = row
        for row in fragment["fields"]:
            key = (str(row["register"]), str(row["name"]))
            encoded = json.dumps(row, ensure_ascii=False, sort_keys=True)
            field_variants.setdefault(key, {})[encoded] = row
    conflicting_registers = sorted(
        name for name, variants in register_variants.items() if len(variants) > 1
    )
    conflicting_fields = sorted(
        f"{register}.{name}"
        for (register, name), variants in field_variants.items()
        if len(variants) > 1
    )
    if conflicting_registers:
        conflicts["registers"] = conflicting_registers
    if conflicting_fields:
        conflicts["fields"] = conflicting_fields
    if conflicts:
        return None, conflicts

    registers = infer_array_bounds(
        [next(iter(variants.values())) for variants in register_variants.values()]
    )
    registers.sort(
        key=lambda row: (str(row["name"]), int(row["offset"]), int(row["width"]))
    )
    fields = [next(iter(variants.values())) for variants in field_variants.values()]
    fields.sort(key=lambda row: (str(row["register"]), str(row["name"])))
    block = next(iter(blocks))
    facts = {"block": block, "registers": registers, "fields": fields}
    encoded = json.dumps(
        facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    sources = {
        (str(source["path"]), str(source["sha256"]))
        for fragment in selected
        for source in fragment["sources"]
    }
    return (
        {
            "id": f"{block.lower()}-{sha256[:16]}",
            "sha256": sha256,
            **facts,
            "sources": [
                {"path": path, "sha256": source_sha256}
                for path, source_sha256 in sorted(sources)
            ],
        },
        {},
    )


def _specialize_layout(
    layout: dict[str, object],
    instance: str,
    evidence: list[dict[str, object]],
) -> dict[str, object]:
    selected = [row for row in evidence if row.get("instance") == instance]
    if not selected:
        return layout
    result = copy.deepcopy(layout)
    registers = {str(row["name"]): row for row in result["registers"]}
    sources = result["sources"]
    assert isinstance(sources, list)
    for row in selected:
        register_name = str(row["register"])
        parameter = str(row["parameter"])
        register = registers.get(register_name)
        if register is None:
            raise ValueError(f"实例 {instance} 的数组证据引用未知寄存器：{register_name}")
        bounds = register.get("array_parameters", {}).get(parameter)
        if not isinstance(bounds, dict):
            raise ValueError(f"实例 {instance} 的数组证据引用未知参数：{register_name}.{parameter}")
        end = int(row["end"])
        if "indices" in bounds or ("end" in bounds and int(bounds["end"]) != end):
            raise ValueError(f"实例 {instance} 的数组证据与布局冲突：{register_name}.{parameter}")
        bounds.update({"end": end, "bound_evidence": row["bound_evidence"]})
        source = row.get("source")
        if not isinstance(source, dict):
            raise ValueError(f"实例 {instance} 的数组证据缺少来源")
        if source not in sources:
            sources.append(dict(source))
    sources.sort(key=lambda row: (str(row["path"]), str(row["sha256"])))
    facts = {
        "block": result["block"],
        "registers": result["registers"],
        "fields": result["fields"],
    }
    encoded = json.dumps(
        facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    result.update(
        {"id": f"{str(result['block']).lower()}-{sha256[:16]}", "sha256": sha256}
    )
    return result


def build_library_ir(library: dict[str, object]) -> dict[str, object]:
    raw_headers = library.get("register_headers")
    if not isinstance(raw_headers, list):
        raise ValueError("Firmware 寄存器系列缺少 register_headers")
    instance_array_bounds = library.get("instance_array_bounds", [])
    if not isinstance(instance_array_bounds, list) or not all(
        isinstance(row, dict) for row in instance_array_bounds
    ):
        raise ValueError("Firmware 寄存器系列的实例数组范围格式无效")

    fragments: dict[str, dict[str, object]] = {}
    instances: dict[tuple[str, int], dict[str, object]] = {}
    source_issues = []
    for header in raw_headers:
        if not isinstance(header, dict):
            raise ValueError("Firmware 寄存器系列包含非法头文件记录")
        issues = {
            key: header.get(key, [])
            for key in (
                "unresolved_registers",
                "unassigned_instances",
                "unassigned_registers",
                "invalid_fields",
            )
            if header.get(key, [])
        }
        if issues:
            source_issues.append({**_source(header), **issues})

        raw_instances = header.get("instances")
        instance_blocks = header.get("instance_blocks")
        if not isinstance(raw_instances, dict) or not isinstance(instance_blocks, dict):
            raise ValueError("Firmware 寄存器头文件缺少实例或 block 映射")
        for block in sorted(set(map(str, instance_blocks.values()))):
            layout = _layout(header, block)
            layout_id = str(layout["id"])
            if layout_id not in fragments:
                fragments[layout_id] = {**layout, "sources": []}
            layout_sources = fragments[layout_id]["sources"]
            assert isinstance(layout_sources, list)
            source = _source(header)
            if source not in layout_sources:
                layout_sources.append(source)

            for name, raw_address in raw_instances.items():
                if instance_blocks.get(name) != block:
                    continue
                key = (str(name), int(raw_address))
                instance = instances.setdefault(
                    key,
                    {
                        "name": key[0],
                        "address": key[1],
                        "blocks": set(),
                        "layouts": set(),
                        "sources": [],
                    },
                )
                instance["blocks"].add(block)
                instance["layouts"].add(layout_id)
                if source not in instance["sources"]:
                    instance["sources"].append(source)

    layouts = {}
    instance_rows = []
    conflicts = []
    for instance in instances.values():
        instance["blocks"] = sorted(instance["blocks"])
        instance["sources"].sort(key=lambda row: (row["path"], row["sha256"]))
        fragment_ids = set(instance.pop("layouts"))
        instance["fragments"] = sorted(fragment_ids)
        layout, issue = _merge_fragments(fragment_ids, fragments)
        if layout is not None:
            layout = _specialize_layout(
                layout, str(instance["name"]), instance_array_bounds
            )
            layout_id = str(layout["id"])
            if layout_id not in layouts:
                layouts[layout_id] = layout
            else:
                known_sources = layouts[layout_id]["sources"]
                assert isinstance(known_sources, list)
                for source in layout["sources"]:
                    if source not in known_sources:
                        known_sources.append(source)
                known_sources.sort(key=lambda row: (row["path"], row["sha256"]))
            instance["layout"] = layout_id
        else:
            conflicts.append(
                {
                    "name": instance["name"],
                    "address": instance["address"],
                    "fragments": sorted(fragment_ids),
                    **issue,
                }
            )
        instance_rows.append(instance)
    layout_rows = sorted(layouts.values(), key=lambda row: str(row["id"]))
    instance_rows.sort(key=lambda row: (str(row["name"]), int(row["address"])))
    conflicts.sort(key=lambda row: (str(row["name"]), int(row["address"])))

    return {
        "series": library["series"],
        "archive_sha256": library["archive_sha256"],
        "tree_sha256": library["tree_sha256"],
        "device_header_sha256": library["device_header_sha256"],
        "layouts": layout_rows,
        "instances": instance_rows,
        "instance_layout_conflicts": conflicts,
        "source_issues": source_issues,
    }


def build_report(registers: dict[str, object]) -> dict[str, object]:
    raw_libraries = registers.get("libraries")
    if not isinstance(raw_libraries, list):
        raise ValueError("Firmware 寄存器报告缺少 libraries")
    libraries = [build_library_ir(library) for library in raw_libraries]
    libraries.sort(key=lambda row: str(row["series"]).casefold())
    return {
        "schema_version": 1,
        "summary": {
            "firmware_libraries": len(libraries),
            "layouts": sum(len(library["layouts"]) for library in libraries),
            "instances": sum(len(library["instances"]) for library in libraries),
            "instance_layout_conflicts": sum(
                len(library["instance_layout_conflicts"]) for library in libraries
            ),
            "source_issues": sum(len(library["source_issues"]) for library in libraries),
        },
        "libraries": libraries,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registers",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-registers.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-ir.json",
    )
    parser.add_argument("--minimum-firmware-libraries", type=int, default=33)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_report(json.loads(args.registers.read_text(encoding="utf-8")))
    report["provenance"] = {
        "path": args.registers.name,
        "sha256": common._sha256(args.registers),
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Firmware 外设 IR：{args.output}")
    if int(summary["firmware_libraries"]) < args.minimum_firmware_libraries:
        raise ValueError("Firmware 外设 IR 系列数量低于门限")
    if int(summary["instance_layout_conflicts"]) != 0:
        raise ValueError("Firmware 外设 IR 仍有实例布局冲突")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
