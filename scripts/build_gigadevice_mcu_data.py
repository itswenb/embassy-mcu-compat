#!/usr/bin/env python3
"""把 GD32 全量型号及来源证据汇总为规范 mcu-data 状态清单。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import compare_gigadevice_svd_headers as svd_compare
import gigadevice_sources as common


COMPILED_PAC_STATUSES = {"compiled", "cached"}


def _series_patterns(series: str) -> list[str]:
    parts = series.split("_")
    patterns = [parts[0]]
    if len(parts) == 2 and re.fullmatch(r"\d+[xX]", parts[1]):
        prefix = re.sub(r"\d+[xX]$", "", parts[0])
        patterns.append(prefix + parts[1])
    return patterns


def series_matches_device(series: str, device: str) -> bool:
    for pattern in _series_patterns(series):
        pattern = pattern.upper()
        expression = "".join(
            "[A-Z0-9]" if character in "xX" else re.escape(character)
            for character in pattern
        )
        if re.fullmatch(expression + r"[A-Z0-9]*", device.upper()) is not None:
            return True
    return False


def choose_firmware_series(
    device: str, source_packs: list[dict[str, object]], available: set[str]
) -> str | None:
    hinted = []
    for pack in source_packs:
        name = str(pack["name"])
        try:
            series = svd_compare.firmware_series_for_pack(name)
        except ValueError:
            continue
        pack_series = name.removesuffix("_DFP")
        if series in available and (
            pack_series == series or series_matches_device(series, device)
        ):
            hinted.append(series)
    if hinted:
        return sorted(set(hinted))[0]

    candidates = [series for series in available if series_matches_device(series, device)]
    if not candidates:
        return None

    def score(series: str) -> tuple[int, int, int]:
        unqualified = int("_" not in series)
        literal = sum(character not in "xX_" for character in series)
        wildcards = -sum(character in "xX" for character in series)
        return unqualified, literal, wildcards

    candidates.sort(key=lambda series: (score(series), series), reverse=True)
    if len(candidates) > 1 and score(candidates[0]) == score(candidates[1]):
        raise ValueError(
            f"型号 {device} 同时匹配多个等价 Firmware 系列：{', '.join(candidates[:2])}"
        )
    return candidates[0]


def _svd_files(resources: list[dict[str, object]]) -> list[dict[str, str]]:
    files: dict[str, dict[str, str]] = {}
    for resource in resources:
        for debug in resource.get("debug", []):
            assert isinstance(debug, dict)
            file = debug.get("file")
            if not isinstance(file, dict):
                continue
            sha256 = str(file["sha256"])
            row = {"path": str(file["path"]), "sha256": sha256}
            if sha256 in files and files[sha256] != row:
                raise ValueError(f"同一 SVD SHA-256 对应不同资源：{sha256}")
            files[sha256] = row
    return [files[key] for key in sorted(files)]


def build_device_row(
    model: dict[str, object],
    resources: list[dict[str, object]],
    builder: dict[str, object],
    firmware: dict[str, object] | None,
    firmware_registers: dict[str, object] | None,
    comparisons_by_svd: dict[str, dict[str, object]],
    pacs_by_svd: dict[str, dict[str, object]],
    variant: dict[str, object] | None = None,
    firmware_pac: dict[str, object] | None = None,
    pins: dict[str, object] | None = None,
    memory: dict[str, object] | None = None,
    rcu: dict[str, object] | None = None,
    dma: dict[str, object] | None = None,
) -> dict[str, object]:
    svds = _svd_files(resources)
    comparisons = []
    pacs = []
    for svd in svds:
        sha256 = svd["sha256"]
        comparison = comparisons_by_svd.get(sha256)
        pac = pacs_by_svd.get(sha256)
        if comparison is None or pac is None:
            raise ValueError(f"SVD {sha256} 缺少交叉校验或 PAC 编译证据")
        comparisons.append(comparison)
        pacs.append(pac)

    has_memory = bool(resources) and all(resource.get("memory") for resource in resources)
    has_algorithm = bool(resources) and all(resource.get("algorithm") for resource in resources)
    builder_evidence = str(builder.get("evidence", "none"))
    conflict = any(
        comparison.get("conflict_status") == "known-blocking"
        for comparison in comparisons
    )
    pac_compiled = bool(svds) and all(
        str(pac.get("status")) in COMPILED_PAC_STATUSES for pac in pacs
    )
    if variant is not None:
        layouts = variant.get("layouts", [])
        instances = variant.get("instances", [])
        assert isinstance(layouts, list) and isinstance(instances, list)
        firmware_register_count = sum(len(layout.get("registers", [])) for layout in layouts)
        firmware_field_count = sum(len(layout.get("fields", [])) for layout in layouts)
        firmware_instance_count = len(instances)
        firmware_register_conflict = any(
            issue.get("conflict_status") == "known-blocking"
            for issue in variant.get("source_issues", [])
        )
    else:
        register_headers = (
            firmware_registers.get("register_headers", [])
            if firmware_registers is not None
            else []
        )
        assert isinstance(register_headers, list)
        firmware_register_count = sum(
            len(header.get("registers", [])) for header in register_headers
        )
        firmware_field_count = sum(
            len(header.get("fields", [])) for header in register_headers
        )
        firmware_instance_count = len(
            {
                (str(name), int(address))
                for header in register_headers
                for name, address in header.get("instances", {}).items()
            }
        )
        firmware_register_conflict = any(
            header.get("unresolved_registers", []) or header.get("invalid_fields", [])
            for header in register_headers
        )
    firmware_pac_compiled = firmware_pac is not None and str(
        firmware_pac.get("compile_status")
    ) in COMPILED_PAC_STATUSES
    unbounded_array_registers = (
        int(firmware_pac.get("unbounded_array_registers", 0))
        if firmware_pac is not None
        else 0
    )
    pin_status = str(pins.get("status")) if pins is not None else "missing"
    if pin_status not in {"normalized", "conflict", "missing"}:
        raise ValueError(f"未知引脚归一状态：{pin_status}")
    memory_status = (
        str(memory.get("memory_status"))
        if memory is not None
        else "indexed"
        if has_memory
        else "missing"
    )
    flash_status = (
        str(memory.get("flash_status"))
        if memory is not None
        else "pack-and-firmware-candidate"
        if has_algorithm and firmware is not None
        else "pack-candidate"
        if has_algorithm
        else "firmware-candidate"
        if firmware is not None
        else "missing"
    )
    if memory_status not in {"normalized", "indexed", "missing"}:
        raise ValueError(f"未知内存归一状态：{memory_status}")
    if flash_status not in {
        "normalized",
        "geometry-only",
        "conflict",
        "pack-and-firmware-candidate",
        "pack-candidate",
        "firmware-candidate",
        "missing",
    }:
        raise ValueError(f"未知 Flash 归一状态：{flash_status}")
    rcu_gate_status = str(rcu.get("gate_status")) if rcu is not None else "missing"
    rcu_binding_status = (
        str(rcu.get("binding_status")) if rcu is not None else "missing"
    )
    if rcu_gate_status not in {"normalized", "conflict", "missing"}:
        raise ValueError(f"未知 RCU 门控状态：{rcu_gate_status}")
    if rcu_binding_status not in {"normalized", "conflict", "missing"}:
        raise ValueError(f"未知 RCU 绑定状态：{rcu_binding_status}")
    rcc_status = (
        "normalized"
        if rcu_gate_status == rcu_binding_status == "normalized"
        else "gate-table-normalized"
        if rcu_gate_status == "normalized"
        else "conflict"
        if rcu_gate_status == "conflict"
        else "firmware-candidate"
        if firmware is not None
        else "missing"
    )
    dma_status = str(dma.get("status")) if dma is not None else "missing"
    if dma_status not in {"normalized", "source-incomplete", "conflict", "missing"}:
        raise ValueError(f"未知 DMA 归一状态：{dma_status}")

    if conflict:
        interrupt_status = "conflict"
    elif svds:
        interrupt_status = "cross-checked"
    elif firmware is not None:
        interrupt_status = "firmware-indexed"
    else:
        interrupt_status = "missing"
    facts = {
        "registers": (
            "conflict"
            if firmware_register_conflict
            else "generated"
            if pac_compiled
            else "firmware-generated"
            if firmware_pac_compiled
            else "indexed"
            if svds
            else "firmware-indexed"
            if firmware_register_count
            else "missing"
        ),
        "interrupts": interrupt_status,
        "memory": memory_status,
        "pins": pin_status,
        "dma": dma_status,
        "rcc": rcc_status,
        "flash": flash_status,
    }

    blockers = []
    if facts["registers"] == "missing":
        blockers.append("register-source-missing")
    elif facts["registers"] == "conflict":
        blockers.append("register-source-conflict")
    if unbounded_array_registers:
        blockers.append("register-array-bounds-not-normalized")
    if facts["interrupts"] == "missing":
        blockers.append("interrupt-source-missing")
    elif facts["interrupts"] == "conflict":
        blockers.append("interrupt-source-conflict")
    if facts["memory"] == "missing":
        blockers.append("memory-source-missing")
    if facts["pins"] == "missing":
        blockers.append("pin-source-missing")
    elif facts["pins"] == "conflict":
        blockers.append("pin-source-conflict")
    if facts["dma"] == "source-incomplete":
        blockers.append("dma-fixed-map-missing")
    elif facts["dma"] == "conflict":
        blockers.append("dma-source-conflict")
    elif facts["dma"] == "missing":
        blockers.append("dma-source-missing")
    if facts["rcc"] == "missing":
        blockers.append("rcc-source-missing")
    elif facts["rcc"] == "conflict":
        blockers.append("rcc-source-conflict")
    elif facts["rcc"] != "normalized":
        blockers.append("rcc-not-normalized")
    if facts["flash"] == "missing":
        blockers.append("flash-source-missing")
    elif facts["flash"] == "conflict":
        blockers.append("flash-source-conflict")
    elif facts["flash"] != "normalized":
        blockers.append("flash-not-normalized")
    if firmware is None and not svds:
        blockers.append("firmware-source-missing")
    if model.get("core") is None or model.get("rust_target") is None:
        blockers.append("target-source-missing")

    source_complete = not blockers
    support_state = "pac_generated" if source_complete and (
        pac_compiled or firmware_pac_compiled
    ) else (
        "source_complete" if source_complete else "catalogued"
    )
    rust_target = model.get("rust_target")
    firmware_series = str(firmware["series"]) if firmware is not None else None
    architecture = (
        "arm"
        if isinstance(rust_target, str) and rust_target.startswith("thumb")
        else "riscv"
        if isinstance(rust_target, str) and rust_target.startswith("riscv")
        else "riscv"
        if firmware_series is not None and firmware_series.startswith(("GD32VF", "GD32VW"))
        else None
    )
    return {
        "id": model["id"],
        "feature": model["feature"],
        "architecture": architecture,
        "core": model.get("core"),
        "rust_target": rust_target,
        "part_numbers": model["part_numbers"],
        "support_state": support_state,
        "facts": facts,
        "blockers": sorted(blockers),
        "sources": {
            "cmsis_devices": model["cmsis_devices"],
            "packs": model["source_packs"],
            "firmware_series": firmware_series,
            "firmware_variant": variant.get("id") if variant is not None else None,
            "firmware_defines": variant.get("defines", []) if variant is not None else [],
            "builder_evidence": builder_evidence,
            "builder_matrices": builder.get("matrix_paths", []),
            "builder_pin_matrices": pins.get("matrix_paths", []) if pins is not None else [],
            "builder_afio": pins.get("afio_paths", []) if pins is not None else [],
            "memory_profiles": memory.get("profiles", []) if memory is not None else [],
            "rcu_variant": rcu.get("variant") if rcu is not None else None,
            "dma_variant": dma.get("variant") if dma is not None else None,
        },
        "artifacts": {
            "svd": [
                {
                    **svd,
                    "comparison": comparisons_by_svd[svd["sha256"]]["conflict_status"],
                }
                for svd in svds
            ],
            "pac": "compiled" if pac_compiled else "missing",
            "firmware_pac": "compiled" if firmware_pac_compiled else "missing",
            "firmware_registers": {
                "instances": firmware_instance_count,
                "registers": firmware_register_count,
                "fields": firmware_field_count,
            },
            "pins": {
                "gpio_pins": int(pins.get("gpio_pins", 0)) if pins is not None else 0,
                "functions": int(pins.get("functions", 0)) if pins is not None else 0,
                "packages": pins.get("packages", []) if pins is not None else [],
                "afio_routes": int(pins.get("afio_routes", 0)) if pins is not None else 0,
            },
            "memory": {
                "regions": int(memory.get("memory_regions", 0)) if memory is not None else 0,
                "flash_regions": int(memory.get("flash_regions", 0)) if memory is not None else 0,
                "algorithms": memory.get("algorithms", []) if memory is not None else [],
            },
            "rcc": {
                "gate_status": rcu_gate_status,
                "binding_status": rcu_binding_status,
            },
            "dma": {
                "kind": dma.get("kind") if dma is not None else None,
                "channels": int(dma.get("channels", 0)) if dma is not None else 0,
                "requests": int(dma.get("requests", 0)) if dma is not None else 0,
                "mdma_channels": (
                    int(dma.get("mdma_channels", 0)) if dma is not None else 0
                ),
                "mdma_requests": (
                    int(dma.get("mdma_requests", 0)) if dma is not None else 0
                ),
            },
        },
    }


def _unique(rows: list[dict[str, object]], key: str, label: str) -> dict[str, dict[str, object]]:
    result = {}
    for row in rows:
        value = str(row[key])
        if value in result:
            raise ValueError(f"{label} 重复主键：{value}")
        result[value] = row
    return result


def firmware_sources_by_kind(
    official_firmware: dict[str, object],
    official_registers: dict[str, object],
    builder_firmware: dict[str, object] | None = None,
    builder_registers: dict[str, object] | None = None,
) -> tuple[
    dict[tuple[str, str], dict[str, object]],
    dict[tuple[str, str], dict[str, object]],
]:
    def index(report: dict[str, object], kind: str, label: str):
        libraries = _unique(report["libraries"], "series", label)
        return {(kind, series): row for series, row in libraries.items()}

    firmware = index(official_firmware, "official", "Firmware 系列")
    registers = index(official_registers, "official", "Firmware 寄存器系列")
    if builder_firmware is not None:
        firmware.update(index(builder_firmware, "builder", "Builder Firmware 系列"))
    if builder_registers is not None:
        registers.update(
            index(builder_registers, "builder", "Builder Firmware 寄存器系列")
        )
    return firmware, registers


def iar_memory_data(device: dict[str, object]) -> dict[str, object]:
    linker = device.get("linker")
    flash = device.get("flash")
    if not isinstance(linker, dict) or not isinstance(flash, dict):
        raise ValueError(f"IAR 型号缺少链接或 Flash 数据：{device.get('id')}")
    memory = linker.get("memory")
    regions = flash.get("regions")
    if not isinstance(memory, list) or not memory or not isinstance(regions, list) or not regions:
        raise ValueError(f"IAR 型号内存或 Flash 区域无效：{device.get('id')}")
    algorithms = sorted(
        {
            str(region["algorithm"]["sha256"])
            for region in regions
            if isinstance(region, dict) and isinstance(region.get("algorithm"), dict)
        }
    )
    if not algorithms or any(
        int(region.get("write_size", 0)) <= 0 or int(region.get("erase_size", 0)) <= 0
        for region in regions
        if isinstance(region, dict)
    ):
        raise ValueError(f"IAR 型号 Flash 几何或算法无效：{device.get('id')}")
    return {
        "id": device["id"],
        "memory_status": "normalized",
        "flash_status": "normalized",
        "memory_regions": len(memory),
        "flash_regions": len(regions),
        "algorithms": algorithms,
        "profiles": [
            f"iar-i79:{device['configuration']}",
            f"iar-icf:{linker['path']}",
            f"iar-board:{flash['path']}",
        ],
    }


def build_report(inputs: dict[str, dict[str, object]]) -> dict[str, object]:
    models = inputs["models"]
    resources = inputs["resources"]
    builders = inputs["builders"]
    pins = inputs["pins"]
    memory = inputs["memory"]
    rcu = inputs["rcu"]
    dma = inputs["dma"]
    firmware = inputs["firmware"]
    firmware_registers = inputs["registers"]
    builder_firmware = inputs.get("builder_firmware")
    builder_registers = inputs.get("builder_registers")
    firmware_variants = inputs["variants"]
    firmware_pacs = inputs["firmware_pacs"]
    comparisons = inputs["comparisons"]
    pacs = inputs["pacs"]
    iar = inputs.get("iar")
    iar_pacs = inputs.get("iar_pacs")
    raw_devices = models["devices"]
    raw_catalog = models["catalog_entries"]
    if not isinstance(raw_devices, list) or not isinstance(raw_catalog, list):
        raise ValueError("型号报告缺少 devices/catalog_entries")

    resources_by_device = _unique(resources["devices"], "device", "Pack 资源")
    builders_by_device = _unique(builders["devices"], "id", "Builder 型号")
    pins_by_device = _unique(pins["devices"], "id", "Builder 引脚")
    memory_by_device = _unique(memory["devices"], "id", "内存与 Flash")
    rcu_by_device = _unique(rcu["devices"], "id", "RCU")
    dma_by_device = _unique(dma["devices"], "id", "DMA")
    firmware_by_series, registers_by_series = firmware_sources_by_kind(
        firmware,
        firmware_registers,
        builder_firmware,
        builder_registers,
    )
    variants_by_device = {}
    for variant in firmware_variants["variants"]:
        for device in variant["devices"]:
            if device in variants_by_device:
                raise ValueError(f"Firmware 设备重复归属变体：{device}")
            variants_by_device[device] = variant
    firmware_pacs_by_variant = _unique(
        firmware_pacs["pacs"], "id", "Firmware PAC"
    )
    comparisons_by_svd = _unique(comparisons["comparisons"], "svd_sha256", "SVD 对照")
    pacs_by_svd = _unique(pacs["pacs"], "svd_sha256", "PAC 编译")
    iar_devices = {}
    iar_svds = {}
    if iar is not None or iar_pacs is not None:
        if iar is None or iar_pacs is None:
            raise ValueError("IAR 设备与 PAC 编译报告必须同时提供")
        iar_devices = _unique(iar["devices"], "id", "IAR 设备")
        iar_svds = {Path(str(row["path"])).name: row for row in iar["svd_files"]}
        if len(iar_svds) != len(iar["svd_files"]):
            raise ValueError("IAR SVD 文件名重复")
        for row in iar_pacs["pacs"]:
            sha256 = str(row["svd_sha256"])
            if sha256 in pacs_by_svd:
                raise ValueError(f"IAR 与 Pack PAC SHA-256 重复：{sha256}")
            pacs_by_svd[sha256] = row
            comparisons_by_svd[sha256] = {
                "svd_sha256": sha256,
                "conflict_status": "none",
            }

    devices = []
    for raw_model in raw_devices:
        assert isinstance(raw_model, dict)
        cmsis_devices = raw_model["cmsis_devices"]
        assert isinstance(cmsis_devices, list)
        device_resources = []
        for cmsis_device in cmsis_devices:
            resource = resources_by_device.get(str(cmsis_device))
            if resource is None:
                raise ValueError(f"CMSIS 型号缺少 Pack 资源：{cmsis_device}")
            device_resources.append(resource)
        if iar_device := iar_devices.get(str(raw_model["id"])):
            svd = iar_svds.get(str(iar_device["svd"]))
            if svd is None:
                raise ValueError(f"IAR 型号引用未知 SVD：{raw_model['id']}")
            device_resources.append({"debug": [{"file": svd}]})
        builder = builders_by_device.get(str(raw_model["id"]))
        if builder is None:
            raise ValueError(f"规范型号缺少 Builder 归一记录：{raw_model['id']}")
        pin_data = pins_by_device.get(str(raw_model["id"]))
        if pin_data is None:
            raise ValueError(f"规范型号缺少 Builder 引脚归一记录：{raw_model['id']}")
        memory_data = memory_by_device.get(str(raw_model["id"]))
        if memory_data is None:
            raise ValueError(f"规范型号缺少内存归一记录：{raw_model['id']}")
        if iar_device := iar_devices.get(str(raw_model["id"])):
            memory_data = iar_memory_data(iar_device)
        rcu_data = rcu_by_device.get(str(raw_model["id"]))
        if rcu_data is None:
            raise ValueError(f"规范型号缺少 RCU 归一记录：{raw_model['id']}")
        dma_data = dma_by_device.get(str(raw_model["id"]))
        if dma_data is None:
            raise ValueError(f"规范型号缺少 DMA 归一记录：{raw_model['id']}")
        variant_ids = {
            str(variant["id"]): variant
            for device in [raw_model["id"], *cmsis_devices]
            if (variant := variants_by_device.get(str(device))) is not None
        }
        if len(variant_ids) > 1:
            raise ValueError(
                f"规范型号 {raw_model['id']} 同时归属多个 Firmware 变体："
                + ", ".join(sorted(variant_ids))
            )
        variant = next(iter(variant_ids.values()), None)
        if rcu_data.get("variant") != (variant.get("id") if variant is not None else None):
            raise ValueError(f"规范型号 RCU 与 Firmware 变体不一致：{raw_model['id']}")
        if dma_data.get("variant") != (variant.get("id") if variant is not None else None):
            raise ValueError(f"规范型号 DMA 与 Firmware 变体不一致：{raw_model['id']}")
        series = str(variant["series"]) if variant is not None else None
        source_kind = str(variant.get("source_kind", "official")) if variant is not None else None
        if variant is not None and (
            (source_kind, series) not in firmware_by_series
            or (source_kind, series) not in registers_by_series
        ):
            raise ValueError(f"Firmware 变体 {variant['id']} 缺少系列来源：{series}")
        firmware_pac = (
            firmware_pacs_by_variant.get(str(variant["id"]))
            if variant is not None
            else None
        )
        if variant is not None and firmware_pac is None:
            raise ValueError(f"Firmware 变体缺少 PAC 编译证据：{variant['id']}")
        devices.append(
            build_device_row(
                raw_model,
                device_resources,
                builder,
                firmware_by_series.get((source_kind, series)) if series is not None else None,
                registers_by_series.get((source_kind, series)) if series is not None else None,
                comparisons_by_svd,
                pacs_by_svd,
                variant=variant,
                firmware_pac=firmware_pac,
                pins=pin_data,
                memory=memory_data,
                rcu=rcu_data,
                dma=dma_data,
            )
        )

    variant_summary = firmware_variants["summary"]
    expected_normalized = int(variant_summary["normalized_devices"])
    expected_with_firmware = int(variant_summary["devices"])
    expected_missing = int(variant_summary["missing_devices"])
    actual_with_firmware = sum(
        device["sources"]["firmware_variant"] is not None for device in devices
    )
    if expected_normalized != len(devices):
        raise ValueError("Firmware 变体与规范型号总数不一致")
    if expected_with_firmware != actual_with_firmware:
        raise ValueError("Firmware 变体设备覆盖数不一致")
    if expected_missing != len(devices) - actual_with_firmware:
        raise ValueError("Firmware 变体缺失设备数不闭合")
    pac_summary = firmware_pacs["summary"]
    if int(pac_summary["variants"]) != len(firmware_pacs_by_variant):
        raise ValueError("Firmware PAC 变体计数不一致")
    if int(pac_summary["devices"]) != actual_with_firmware:
        raise ValueError("Firmware PAC 设备覆盖数不一致")
    if any(
        str(pac.get("compile_status")) not in COMPILED_PAC_STATUSES
        for pac in firmware_pacs_by_variant.values()
    ):
        raise ValueError("Firmware PAC 存在未通过类型检查的变体")
    pin_summary = pins["summary"]
    if int(pin_summary["normalized_devices"]) != len(devices):
        raise ValueError("Builder 引脚与规范型号总数不一致")
    normalized_pin_devices = sum(device["facts"]["pins"] == "normalized" for device in devices)
    conflicted_pin_devices = sum(device["facts"]["pins"] == "conflict" for device in devices)
    if int(pin_summary["devices_with_normalized_pins"]) != normalized_pin_devices:
        raise ValueError("全来源引脚覆盖数不一致")
    if int(pin_summary.get("devices_with_pin_conflict", 0)) != conflicted_pin_devices:
        raise ValueError("全来源引脚冲突数不一致")
    memory_summary = memory["summary"]
    if int(memory_summary["normalized_devices"]) != len(devices):
        raise ValueError("内存与规范型号总数不一致")
    normalized_memory_devices = sum(device["facts"]["memory"] == "normalized" for device in devices)
    normalized_flash_devices = sum(device["facts"]["flash"] == "normalized" for device in devices)
    conflicted_flash_devices = sum(device["facts"]["flash"] == "conflict" for device in devices)
    iar_memory_additions = sum(
        str(memory_by_device[device].get("memory_status")) != "normalized"
        for device in iar_devices
    )
    iar_flash_additions = sum(
        str(memory_by_device[device].get("flash_status")) != "normalized"
        for device in iar_devices
    )
    if (
        int(memory_summary["devices_with_normalized_memory"]) + iar_memory_additions
        != normalized_memory_devices
    ):
        raise ValueError("内存归一覆盖数不一致")
    if (
        int(memory_summary["devices_with_normalized_flash"]) + iar_flash_additions
        != normalized_flash_devices
    ):
        raise ValueError("Flash 归一覆盖数不一致")
    if int(memory_summary["devices_with_flash_source_conflict"]) != conflicted_flash_devices:
        raise ValueError("Flash 来源冲突数不一致")
    rcu_summary = rcu["summary"]
    if int(rcu_summary["normalized_devices"]) != len(devices):
        raise ValueError("RCU 与规范型号总数不一致")
    normalized_rcu_gate_devices = sum(
        device["artifacts"]["rcc"]["gate_status"] == "normalized"
        for device in devices
    )
    conflicted_rcu_gate_devices = sum(
        device["artifacts"]["rcc"]["gate_status"] == "conflict"
        for device in devices
    )
    normalized_rcc_devices = sum(
        device["facts"]["rcc"] == "normalized" for device in devices
    )
    if int(rcu_summary["devices_with_normalized_gate_table"]) != normalized_rcu_gate_devices:
        raise ValueError("RCU 门控表归一覆盖数不一致")
    if int(rcu_summary["devices_with_gate_table_conflict"]) != conflicted_rcu_gate_devices:
        raise ValueError("RCU 门控表冲突数不一致")
    if int(rcu_summary["devices_with_normalized_rcu"]) != normalized_rcc_devices:
        raise ValueError("RCU 实例绑定覆盖数不一致")
    dma_summary = dma["summary"]
    if int(dma_summary["normalized_devices"]) != len(devices):
        raise ValueError("DMA 与规范型号总数不一致")
    normalized_dma_devices = sum(
        device["facts"]["dma"] == "normalized" for device in devices
    )
    conflicted_dma_devices = sum(
        device["facts"]["dma"] == "conflict" for device in devices
    )
    incomplete_dma_devices = sum(
        device["facts"]["dma"] == "source-incomplete" for device in devices
    )
    if int(dma_summary["devices_with_normalized_dma"]) != normalized_dma_devices:
        raise ValueError("DMA 归一覆盖数不一致")
    if int(dma_summary["devices_with_dma_conflict"]) != conflicted_dma_devices:
        raise ValueError("DMA 来源冲突数不一致")
    if (
        int(dma_summary["devices_with_fixed_request_map_missing"])
        != incomplete_dma_devices
    ):
        raise ValueError("固定映射 DMA 缺失数不一致")

    states = Counter(str(device["support_state"]) for device in devices)
    blockers = Counter(
        str(blocker) for device in devices for blocker in device["blockers"]
    )
    return {
        "schema_version": 1,
        "vendor": {"id": "gigadevice", "name": "GigaDevice"},
        "summary": {
            "catalog_entries": len(raw_catalog),
            "normalized_devices": len(devices),
            "part_numbers": sum(len(device["part_numbers"]) for device in devices),
            "catalog_only_entries": sum(
                entry.get("kind") == "catalog_only" for entry in raw_catalog
            ),
            "devices_with_firmware": sum(
                device["sources"]["firmware_series"] is not None for device in devices
            ),
            "devices_with_compiled_pac": sum(
                device["artifacts"]["pac"] == "compiled" for device in devices
            ),
            "devices_with_firmware_registers": sum(
                int(device["artifacts"]["firmware_registers"]["registers"]) > 0
                for device in devices
            ),
            "devices_with_firmware_pac": sum(
                device["artifacts"]["firmware_pac"] == "compiled"
                for device in devices
            ),
            "devices_with_any_compiled_pac": sum(
                device["artifacts"]["pac"] == "compiled"
                or device["artifacts"]["firmware_pac"] == "compiled"
                for device in devices
            ),
            "devices_with_normalized_pins": normalized_pin_devices,
            "devices_with_pin_source_conflict": conflicted_pin_devices,
            "devices_with_normalized_memory": normalized_memory_devices,
            "devices_with_normalized_flash": normalized_flash_devices,
            "devices_with_flash_source_conflict": conflicted_flash_devices,
            "devices_with_normalized_rcu_gate_table": normalized_rcu_gate_devices,
            "devices_with_rcu_gate_table_conflict": conflicted_rcu_gate_devices,
            "devices_with_normalized_rcc": normalized_rcc_devices,
            "devices_with_normalized_dma": normalized_dma_devices,
            "devices_with_dma_source_conflict": conflicted_dma_devices,
            "devices_with_incomplete_fixed_dma": incomplete_dma_devices,
            "devices_with_register_source_conflict": sum(
                "register-source-conflict" in device["blockers"] for device in devices
            ),
            "devices_with_source_conflict": sum(
                "interrupt-source-conflict" in device["blockers"] for device in devices
            ),
            "support_states": dict(sorted(states.items())),
            "blockers": dict(sorted(blockers.items())),
        },
        "catalog_entries": raw_catalog,
        "devices": devices,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    for name, relative in (
        ("models", "reports/gigadevice-models.json"),
        ("resources", "reports/gigadevice-pack-resources.json"),
        ("builders", "reports/gigadevice-builder-models.json"),
        ("pins", "reports/gigadevice-pins.json"),
        ("memory", "reports/gigadevice-memory.json"),
        ("rcu", "reports/gigadevice-rcu.json"),
        ("dma", "reports/gigadevice-dma.json"),
        ("firmware", "reports/gigadevice-firmware-headers.json"),
        ("registers", "reports/gigadevice-firmware-registers.json"),
        ("builder_firmware", "reports/gigadevice-builder-headers.json"),
        ("builder_registers", "reports/gigadevice-builder-registers.json"),
        ("variants", "reports/gigadevice-merged-firmware-variants.json"),
        ("firmware_pacs", "reports/gigadevice-firmware-pac-compile.json"),
        ("comparisons", "reports/gigadevice-svd-header-comparison.json"),
        ("pacs", "reports/gigadevice-pac-compile.json"),
        ("iar", "reports/gigadevice-iar-a7.json"),
        ("iar_pacs", "reports/gigadevice-iar-pac-compile.json"),
    ):
        parser.add_argument(f"--{name}", type=Path, default=repo_root / relative)
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-mcu-data.json"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    paths = {
        name: getattr(args, name)
        for name in (
            "models",
            "resources",
            "builders",
            "pins",
            "memory",
            "rcu",
            "dma",
            "firmware",
            "registers",
            "builder_firmware",
            "builder_registers",
            "variants",
            "firmware_pacs",
            "comparisons",
            "pacs",
            "iar",
            "iar_pacs",
        )
    }
    report = build_report(
        {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name, path in paths.items()
        }
    )
    report["provenance"] = {
        name: {"path": path.name, "sha256": common._sha256(path)}
        for name, path in sorted(paths.items())
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(
        " ".join(
            f"{key}={summary[key]}"
            for key in (
                "catalog_entries",
                "normalized_devices",
                "part_numbers",
                "devices_with_compiled_pac",
                "devices_with_source_conflict",
            )
        )
    )
    print(f"mcu-data 报告：{args.output}")
    if int(summary["catalog_entries"]) < 704 or int(summary["normalized_devices"]) < 657:
        raise ValueError("mcu-data 全型号闭包低于门限")
    states = summary["support_states"]
    assert isinstance(states, dict)
    if sum(int(value) for value in states.values()) != int(summary["normalized_devices"]):
        raise ValueError("mcu-data 支持状态计数不闭合")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
