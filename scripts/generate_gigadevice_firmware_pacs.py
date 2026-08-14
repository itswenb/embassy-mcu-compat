#!/usr/bin/env python3
"""把设备条件 Firmware IR 转换为 chiptool 可消费的 SVD。"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import audit_gigadevice_svds as svd_audit
import compile_gigadevice_pacs as pac_compile
import gigadevice_sources as common


IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")
COMPILED_PAC_STATUSES = {"compiled", "cached"}


def _identifier(value: object, label: str) -> str:
    name = str(value)
    if IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"{label} 不是合法标识符：{name}")
    return name


def _text(parent: ET.Element, name: str, value: object) -> ET.Element:
    child = ET.SubElement(parent, name)
    child.text = str(value)
    return child


def register_parameter_stats(variant: dict[str, object]) -> dict[str, int]:
    registers = [
        register
        for layout in variant["layouts"]
        for register in layout["registers"]
    ]
    arrays = [register for register in registers if register.get("array_parameters")]
    bounded = [
        register
        for register in arrays
        if all(
            "end" in bounds or "indices" in bounds
            for bounds in register["array_parameters"].values()
        )
    ]
    return {
        "base_parameter_registers": sum(
            bool(register.get("base_parameters")) for register in registers
        ),
        "array_registers": len(arrays),
        "bounded_array_registers": len(bounded),
        "unbounded_array_registers": len(arrays) - len(bounded),
    }


def unbounded_array_parameters(variant: dict[str, object]) -> list[dict[str, str]]:
    return sorted(
        (
            {
                "layout": str(layout["id"]),
                "block": str(layout["block"]),
                "register": str(register["name"]),
                "parameter": str(parameter),
            }
            for layout in variant["layouts"]
            for register in layout["registers"]
            for parameter, bounds in register.get("array_parameters", {}).items()
            if "end" not in bounds and "indices" not in bounds
        ),
        key=lambda row: (
            row["block"],
            row["register"],
            row["parameter"],
            row["layout"],
        ),
    )


def variant_svd_bytes(variant: dict[str, object]) -> bytes:
    devices = variant.get("devices")
    raw_layouts = variant.get("layouts")
    raw_instances = variant.get("instances")
    raw_interrupts = variant.get("interrupts")
    if not all(isinstance(value, list) for value in (devices, raw_layouts, raw_instances, raw_interrupts)):
        raise ValueError("Firmware 变体缺少 devices/layouts/instances/interrupts")
    assert isinstance(devices, list)
    assert isinstance(raw_layouts, list)
    assert isinstance(raw_instances, list)
    assert isinstance(raw_interrupts, list)
    if not devices or not raw_instances:
        raise ValueError("Firmware 变体缺少设备或外设实例")

    layouts = {}
    for layout in raw_layouts:
        assert isinstance(layout, dict)
        layout_id = str(layout["id"])
        if layout_id in layouts:
            raise ValueError(f"Firmware 变体布局重复：{layout_id}")
        layouts[layout_id] = layout

    root = ET.Element("device", {"schemaVersion": "1.1"})
    _text(root, "name", _identifier(devices[0], "设备名"))
    _text(root, "version", "1.0")
    _text(root, "description", f"由宽松许可 Firmware 事实生成：{variant['id']}")
    _text(root, "addressUnitBits", 8)
    _text(root, "width", 32)
    peripherals = ET.SubElement(root, "peripherals")

    seen_instances = set()
    for instance_index, instance in enumerate(
        sorted(raw_instances, key=lambda row: (str(row["name"]), int(row["address"])))
    ):
        assert isinstance(instance, dict)
        name = _identifier(instance["name"], "外设实例名")
        if name in seen_instances:
            raise ValueError(f"外设实例名重复：{name}")
        seen_instances.add(name)
        layout_id = str(instance["layout"])
        if layout_id not in layouts:
            raise ValueError(f"外设实例 {name} 引用未知布局：{layout_id}")
        layout = layouts[layout_id]
        registers = layout.get("registers")
        fields = layout.get("fields")
        if not isinstance(registers, list) or not isinstance(fields, list):
            raise ValueError(f"布局 {layout_id} 缺少 registers/fields")

        peripheral = ET.SubElement(peripherals, "peripheral")
        _text(peripheral, "name", name)
        _text(peripheral, "description", f"Firmware 外设实例 {name}")
        _text(peripheral, "groupName", _identifier(layout["block"], "外设分组名"))
        _text(peripheral, "baseAddress", f"0x{int(instance['address']):08X}")

        fields_by_register: dict[str, list[dict[str, object]]] = {}
        for field in fields:
            assert isinstance(field, dict)
            fields_by_register.setdefault(str(field["register"]), []).append(field)
        register_names = {str(register["name"]) for register in registers}
        unknown_fields = set(fields_by_register) - register_names
        if unknown_fields:
            raise ValueError(
                f"布局 {layout_id} 位域引用未知寄存器：{', '.join(sorted(unknown_fields))}"
            )

        register_ends = []
        for register in registers:
            array_parameters = register.get("array_parameters", {})
            if not isinstance(array_parameters, dict):
                raise ValueError(f"寄存器 {register['name']} 数组参数无效")
            array_span = 0
            for bounds in array_parameters.values():
                if "indices" in bounds:
                    indices = list(map(int, bounds["indices"]))
                    if indices:
                        array_span += (
                            max(indices) - int(bounds["start"])
                        ) * int(bounds["stride"])
                elif "end" in bounds:
                    array_span += (
                        int(bounds["end"]) - int(bounds["start"])
                    ) * int(bounds["stride"])
            register_ends.append(
                int(register["offset"])
                + array_span
                + (int(register["width"]) + 7) // 8
            )
        maximum_end = max(register_ends)
        address_block = ET.SubElement(peripheral, "addressBlock")
        _text(address_block, "offset", "0x0")
        _text(address_block, "size", f"0x{maximum_end:X}")
        _text(address_block, "usage", "registers")

        if instance_index == 0:
            for interrupt in sorted(
                raw_interrupts,
                key=lambda row: (int(row["value"]), str(row["name"])),
            ):
                assert isinstance(interrupt, dict)
                node = ET.SubElement(peripheral, "interrupt")
                _text(node, "name", _identifier(interrupt["name"], "中断名"))
                _text(node, "value", int(interrupt["value"]))

        register_nodes = ET.SubElement(peripheral, "registers")
        seen_source_registers = set()
        seen_registers = set()
        for register in sorted(
            registers, key=lambda row: (int(row["offset"]), str(row["name"]))
        ):
            assert isinstance(register, dict)
            source_register_name = _identifier(register["name"], "寄存器名")
            if source_register_name in seen_source_registers:
                raise ValueError(f"布局 {layout_id} 寄存器名重复：{source_register_name}")
            seen_source_registers.add(source_register_name)
            offset = int(register["offset"])
            width = int(register["width"])
            if offset < 0 or width not in {8, 16, 32, 64}:
                raise ValueError(f"寄存器 {source_register_name} 的偏移或宽度无效")
            array_parameters = register.get("array_parameters", {})
            if not isinstance(array_parameters, dict):
                raise ValueError(f"寄存器 {source_register_name} 数组参数无效")
            emitted = [(source_register_name, offset, None)]
            if len(array_parameters) > 1:
                dimensions = []
                for parameter, bounds in array_parameters.items():
                    start = int(bounds["start"])
                    stride = int(bounds["stride"])
                    indices = (
                        list(map(int, bounds["indices"]))
                        if "indices" in bounds
                        else list(range(start, int(bounds["end"]) + 1))
                        if "end" in bounds
                        else []
                    )
                    if (
                        start < 0
                        or stride <= 0
                        or not indices
                        or indices != sorted(set(indices))
                        or indices[0] != start
                    ):
                        raise ValueError(f"寄存器 {source_register_name} 多维数组范围无效")
                    dimensions.append((str(parameter), start, stride, indices))
                emitted = []
                for values in itertools.product(*(item[3] for item in dimensions)):
                    emitted.append(
                        (
                            source_register_name + "_" + "_".join(map(str, values)),
                            offset
                            + sum(
                                (value - start) * stride
                                for value, (_, start, stride, _) in zip(
                                    values, dimensions, strict=True
                                )
                            ),
                            None,
                        )
                    )
            elif array_parameters and "indices" in next(iter(array_parameters.values())):
                bounds = next(iter(array_parameters.values()))
                start = int(bounds["start"])
                stride = int(bounds["stride"])
                indices = list(map(int, bounds["indices"]))
                if (
                    start < 0
                    or stride <= 0
                    or not indices
                    or indices != sorted(set(indices))
                    or indices[0] != start
                ):
                    raise ValueError(f"寄存器 {source_register_name} 稀疏数组范围无效")
                emitted = [
                    (
                        f"{source_register_name}_{index}",
                        offset + (index - start) * stride,
                        None,
                    )
                    for index in indices
                ]
            elif array_parameters and "end" in next(iter(array_parameters.values())):
                bounds = next(iter(array_parameters.values()))
                start = int(bounds["start"])
                end = int(bounds["end"])
                stride = int(bounds["stride"])
                if start < 0 or end < start or stride <= 0:
                    raise ValueError(f"寄存器 {source_register_name} 数组范围无效")
                emitted = [(source_register_name + "%s", offset, (start, end, stride))]

            selected_fields = fields_by_register.get(source_register_name, [])
            for register_name, register_offset, dimensions in emitted:
                if register_name in seen_registers:
                    raise ValueError(f"布局 {layout_id} 寄存器名重复：{register_name}")
                seen_registers.add(register_name)
                register_node = ET.SubElement(register_nodes, "register")
                if dimensions is not None:
                    start, end, stride = dimensions
                    _text(register_node, "dim", end - start + 1)
                    _text(register_node, "dimIncrement", f"0x{stride:X}")
                    _text(register_node, "dimIndex", f"{start}-{end}")
                _text(register_node, "name", register_name)
                _text(register_node, "description", f"Firmware 寄存器 {register_name}")
                _text(register_node, "addressOffset", f"0x{register_offset:X}")
                _text(register_node, "size", f"0x{width:X}")
                _text(register_node, "access", "read-write")

                if selected_fields:
                    field_nodes = ET.SubElement(register_node, "fields")
                    seen_fields = set()
                    for field in sorted(
                        selected_fields,
                        key=lambda row: (int(row["bit_offset"]), str(row["name"])),
                    ):
                        field_name = _identifier(field["name"], "位域名")
                        if field_name in seen_fields:
                            raise ValueError(f"寄存器 {register_name} 位域名重复：{field_name}")
                        seen_fields.add(field_name)
                        bit_offset = int(field["bit_offset"])
                        bit_size = int(field["bit_size"])
                        if bit_offset < 0 or bit_size <= 0 or bit_offset + bit_size > width:
                            raise ValueError(f"寄存器 {register_name} 位域范围无效：{field_name}")
                        field_node = ET.SubElement(field_nodes, "field")
                        _text(field_node, "name", field_name)
                        _text(field_node, "description", f"Firmware 位域 {field_name}")
                        _text(field_node, "bitOffset", bit_offset)
                        _text(field_node, "bitWidth", bit_size)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _svd_path(variant: dict[str, object], directory: Path) -> tuple[Path, str]:
    data = variant_svd_bytes(variant)
    sha256 = hashlib.sha256(data).hexdigest()
    path = directory / f"{variant['id']}-{sha256[:16]}.svd"
    if path.is_file():
        if common._sha256(path) != sha256:
            raise ValueError(f"Firmware SVD 缓存哈希不一致：{path}")
    else:
        common._write_text_atomic(path, data.decode("utf-8"))
    return path, sha256


def generate(
    variants_report: dict[str, object],
    chiptool_root: Path,
    target_dir: Path,
    svd_dir: Path,
    generated_cache: Path,
    compile_cache: Path,
) -> dict[str, object]:
    variants = variants_report.get("variants")
    if not isinstance(variants, list):
        raise ValueError("Firmware 变体报告缺少 variants")
    binary, revision = svd_audit._build_chiptool(chiptool_root, target_dir)
    rustc_version = pac_compile._rustc_version()
    results = []
    for variant in sorted(variants, key=lambda row: str(row["id"])):
        assert isinstance(variant, dict)
        svd, svd_sha256 = _svd_path(variant, svd_dir)
        stats = svd_audit.svd_stats(svd)
        generate_status, marker = svd_audit._generate(
            binary,
            svd,
            svd_sha256,
            svd_sha256,
            revision,
            generated_cache,
        )
        compile_status = "missing"
        compile_details = None
        if generate_status != "failed":
            output = (
                generated_cache
                / f"n{svd_audit.NORMALIZATION_VERSION}-{svd_sha256[:16]}-{revision[:12]}"
            )
            compile_status, compile_details = pac_compile._compile(
                output / "lib.rs", rustc_version, compile_cache
            )
        results.append(
            {
                "id": variant["id"],
                "series": variant["series"],
                "devices": variant["devices"],
                "source_issues": variant["source_issues"],
                "svd": {
                    "path": svd.name,
                    "sha256": svd_sha256,
                    **stats,
                },
                "chiptool_status": generate_status,
                "chiptool": marker,
                "compile_status": compile_status,
                "compile": compile_details,
                "unbounded_arrays": unbounded_array_parameters(variant),
                **register_parameter_stats(variant),
            }
        )

    failed = sum(
        result["chiptool_status"] == "failed"
        or result["compile_status"] == "failed"
        for result in results
    )
    return {
        "schema_version": 1,
        "chiptool": {
            "repository": "https://github.com/embassy-rs/chiptool",
            "revision": revision,
        },
        "rustc": rustc_version,
        "summary": {
            "variants": len(results),
            "devices": sum(len(result["devices"]) for result in results),
            "generated_or_cached": sum(
                result["chiptool_status"] != "failed" for result in results
            ),
            "compiled_or_cached": sum(
                result["compile_status"] in COMPILED_PAC_STATUSES
                for result in results
            ),
            "failed": failed,
            "peripherals": sum(result["svd"]["peripherals"] for result in results),
            "interrupts": sum(result["svd"]["interrupts"] for result in results),
            "registers": sum(result["svd"]["registers"] for result in results),
            "fields": sum(result["svd"]["fields"] for result in results),
            "base_parameter_registers": sum(
                result["base_parameter_registers"] for result in results
            ),
            "array_registers": sum(result["array_registers"] for result in results),
            "bounded_array_registers": sum(
                result["bounded_array_registers"] for result in results
            ),
            "unbounded_array_registers": sum(
                result["unbounded_array_registers"] for result in results
            ),
            "variants_with_blocking_source_issues": sum(
                any(
                    issue.get("conflict_status") == "known-blocking"
                    for issue in result["source_issues"]
                )
                for result in results
            ),
        },
        "pacs": results,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variants",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-variants.json",
    )
    parser.add_argument(
        "--chiptool-root",
        type=Path,
        default=repo_root / ".cache/research/repos/chiptool",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=repo_root / ".cache/tools/chiptool-target",
    )
    parser.add_argument(
        "--svd-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/firmware-svd-v1",
    )
    parser.add_argument(
        "--generated-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/chiptool-firmware-v1",
    )
    parser.add_argument(
        "--compile-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/firmware-pac-compile-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-pac-compile.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    variants = json.loads(args.variants.read_text(encoding="utf-8"))
    report = generate(
        variants,
        args.chiptool_root,
        args.target_dir,
        args.svd_dir,
        args.generated_cache,
        args.compile_cache,
    )
    report["provenance"] = {
        "path": args.variants.name,
        "sha256": common._sha256(args.variants),
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Firmware PAC 编译报告：{args.output}")
    expected = variants["summary"]
    if (
        int(summary["variants"]) != int(expected["variants"])
        or int(summary["devices"]) != int(expected["devices"])
        or int(summary["compiled_or_cached"]) != int(summary["variants"])
        or int(summary["failed"]) != 0
    ):
        raise ValueError("Firmware PAC 生成或类型检查未闭合全部变体")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        ET.ParseError,
        json.JSONDecodeError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
