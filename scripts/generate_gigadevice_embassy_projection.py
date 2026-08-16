#!/usr/bin/env python3
"""从 GD32 规范事实生成 Embassy 兼容投影。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
from functools import cache
from pathlib import Path

import gigadevice_sources as common
from normalize_gigadevice_embassy_names import embassy_instance_name
from generate_gigadevice_stm32_data import CORE_NAMES
from analyze_gigadevice_stm32_register_compat import st_block_signature


REQUIRED_SYSTEM_PERIPHERALS = {"EXTI", "FLASH", "RCC"}
ROUTING_PERIPHERALS = {"AFIO", "SYSCFG"}
EMBASSY_DMA_KINDS = {"dma", "bdma", "gpdma", "lpdma", "mdma"}
SYSTEM_COMPAT_PERIPHERALS = {"RCC", "FLASH", "AFIO", "SYSCFG", "PWR", "UID"}
UID_ADDRESSES = {"GD32F30x_DFP": 0x1FFFF7E8}
CORE_PROFILE_COMPATIBILITY = {
    "cm0": {"cm0"},
    "cm0p": {"cm0", "cm0p"},
    "cm3": {"cm3"},
    "cm4": {"cm3", "cm4"},
    "cm7": {"cm3", "cm4", "cm7"},
    "cm23": {"cm0", "cm0p"},
    "cm33": {"cm0p", "cm33"},
    "cm55": {"cm33", "cm55"},
}

DMAMUX_FIELD_NAMES = {
    "MUXID": "DMAREQ_ID",
    "SOIE": "SOIE",
    "EVGEN": "EGE",
    "SYNCEN": "SE",
    "SYNCP": "SPOL",
    "NBR": "NBREQ",
    "SYNCID": "SYNC_ID",
}


def _project_dmamux_registers(
    native: dict[str, object], block: str
) -> tuple[dict[str, str], dict[str, object]]:
    source = native.get(f"block/{block}")
    if not isinstance(source, dict) or not isinstance(source.get("items"), list):
        raise ValueError(f"DMAMUX SVD IR 缺少 block/{block}")
    channels = []
    for item in source["items"]:
        if not isinstance(item, dict):
            continue
        match = re.fullmatch(r"RM_CH(\d+)CFG", str(item.get("name", "")))
        if match is not None:
            channels.append((int(match.group(1)), item))
    channels.sort(key=lambda row: row[0])
    if not channels or [index for index, _ in channels] != list(range(len(channels))):
        raise ValueError("DMAMUX 请求通道不是从 0 开始的连续实例")
    if any(int(item.get("byte_offset", -1)) != index * 4 for index, item in channels):
        raise ValueError("DMAMUX 请求通道寄存器不是 4 字节连续布局")

    projected_fields = None
    source_fieldsets = []
    for _, item in channels:
        fieldset_name = item.get("fieldset")
        fieldset = native.get(f"fieldset/{fieldset_name}")
        if not isinstance(fieldset, dict) or not isinstance(fieldset.get("fields"), list):
            raise ValueError(f"DMAMUX 请求通道 fieldset 无效：{fieldset_name}")
        fields = []
        for field in fieldset["fields"]:
            if not isinstance(field, dict):
                raise ValueError(f"DMAMUX 请求通道字段无效：{fieldset_name}")
            mapped = copy.deepcopy(field)
            mapped["name"] = DMAMUX_FIELD_NAMES.get(
                str(field.get("name", "")), str(field.get("name", ""))
            )
            fields.append(mapped)
        signature = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        if projected_fields is None:
            projected_fields = (signature, fields)
        elif projected_fields[0] != signature:
            raise ValueError("DMAMUX 请求通道寄存器字段布局不一致")
        source_fieldsets.append(f"fieldset/{fieldset_name}")
    assert projected_fields is not None
    required = {"DMAREQ_ID", "EGE", "NBREQ"}
    actual = {str(field["name"]) for field in projected_fields[1]}
    if not required <= actual:
        raise ValueError("DMAMUX 请求通道缺少 Embassy 所需字段")

    registers = copy.deepcopy(native)
    registers.pop(f"block/{block}")
    for name in source_fieldsets:
        registers.pop(name, None)
    projected_block = copy.deepcopy(source)
    projected_block["items"] = [
        {
            "name": "CCR",
            "array": {"len": len(channels), "stride": 4},
            "byte_offset": 0,
            "fieldset": "CCR",
        },
        *[
            copy.deepcopy(item)
            for item in source["items"]
            if not (
                isinstance(item, dict)
                and re.fullmatch(r"RM_CH\d+CFG", str(item.get("name", "")))
            )
        ],
    ]
    registers["block/DMAMUX"] = projected_block
    registers["fieldset/CCR"] = {"fields": projected_fields[1]}
    digest = hashlib.sha256(
        json.dumps(registers, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return {"kind": "dmamux", "version": f"gd{digest}", "block": "DMAMUX"}, registers


def _project_exti_registers(
    native: dict[str, object], block: str
) -> tuple[dict[str, str], dict[str, object]]:
    source = native.get(f"block/{block}")
    if not isinstance(source, dict) or not isinstance(source.get("items"), list):
        raise ValueError(f"EXTI SVD IR 缺少 block/{block}")
    names = {
        "INTEN": ("IMR", 0),
        "EVEN": ("EMR", 4),
        "RTEN": ("RTSR", 8),
        "FTEN": ("FTSR", 12),
        "SWIEV": ("SWIER", 16),
        "PD": ("PR", 20),
    }
    items = []
    line_sets = []
    found = set()
    for item in source["items"]:
        if not isinstance(item, dict):
            raise ValueError("EXTI block 包含无效寄存器")
        mapping = names.get(str(item.get("name", "")))
        if mapping is None:
            items.append(copy.deepcopy(item))
            continue
        target, expected_offset = mapping
        if int(item.get("byte_offset", -1)) != expected_offset:
            raise ValueError(f"EXTI {item.get('name')} 偏移不兼容")
        fieldset = native.get(f"fieldset/{item.get('fieldset')}")
        if not isinstance(fieldset, dict) or not isinstance(fieldset.get("fields"), list):
            raise ValueError(f"EXTI {item.get('name')} 缺少 fieldset")
        bits = {
            int(field["bit_offset"])
            for field in fieldset["fields"]
            if isinstance(field, dict)
            and int(field.get("bit_size", 0)) == 1
            and isinstance(field.get("bit_offset"), int)
        }
        line_sets.append(bits)
        mapped = copy.deepcopy(item)
        mapped.update(
            {
                "name": target,
                "array": {"len": 1, "stride": 32},
                "fieldset": "LINES",
            }
        )
        items.append(mapped)
        found.add(str(item["name"]))
    if found != set(names):
        raise ValueError("EXTI 缺少 Embassy 所需的六个基础寄存器")
    common_lines = set.intersection(*line_sets)
    line_count = 0
    while line_count in common_lines:
        line_count += 1
    if line_count < 16:
        raise ValueError("EXTI GPIO 中断线不足 16 条")

    registers = copy.deepcopy(native)
    projected_block = copy.deepcopy(source)
    projected_block["items"] = items
    registers[f"block/{block}"] = projected_block
    registers["fieldset/LINES"] = {
        "fields": [
            {
                "name": "LINE",
                "description": "EXTI line",
                "bit_offset": 0,
                "bit_size": 1,
                "array": {"len": line_count, "stride": 1},
            }
        ]
    }
    digest = hashlib.sha256(
        json.dumps(registers, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return {"kind": "exti", "version": f"gd{digest}", "block": block}, registers


def _project_usart_v1_registers(
    sources: dict[tuple[str, str, str], dict[str, object]],
    official: dict[str, object],
) -> tuple[dict[str, str], dict[str, object], dict[tuple[str, str, str], str]]:
    expected = {
        "STAT0": 0,
        "DATA": 4,
        "BAUD": 8,
        "CTL0": 12,
        "CTL1": 16,
        "CTL2": 20,
        "GP": 28,
    }
    registers = copy.deepcopy(official)
    uart = registers.get("block/UART")
    usart = registers.get("block/USART")
    if not all(
        isinstance(block, dict) and isinstance(block.get("items"), list)
        for block in (uart, usart)
    ):
        raise ValueError("STM32 USART v1 模板缺少 UART/USART block")
    assert isinstance(uart, dict) and isinstance(usart, dict)
    gtpr = next(
        (
            copy.deepcopy(item)
            for item in usart["items"]
            if isinstance(item, dict) and item.get("name") == "GTPR"
        ),
        None,
    )
    if gtpr is None:
        raise ValueError("STM32 USART v1 模板缺少 GTPR")
    gtpr["byte_offset"] = 28
    uart["items"].append(gtpr)
    usart["items"] = [
        item
        for item in usart["items"]
        if not isinstance(item, dict) or item.get("name") != "GTPR"
    ]

    blocks = {}
    extras = {}
    for key, native in sources.items():
        source = native.get(f"block/{key[2]}")
        if not isinstance(source, dict) or not isinstance(source.get("items"), list):
            raise ValueError(f"USART SVD IR 缺少 block/{key[2]}")
        offsets = {
            str(item.get("name")): int(item.get("byte_offset", -1))
            for item in source["items"]
            if isinstance(item, dict)
        }
        if any(offsets.get(name) != offset for name, offset in expected.items()):
            raise ValueError("USART v1 基础寄存器布局不兼容")
        source_extras = [
            copy.deepcopy(item)
            for item in source["items"]
            if isinstance(item, dict)
            and int(item.get("byte_offset", -1)) not in expected.values()
        ]
        blocks[key] = "USART" if source_extras else "UART"
        for item in source_extras:
            extras[(str(item.get("name")), int(item.get("byte_offset", -1)))] = item
        for name, value in native.items():
            if name not in registers:
                registers[name] = copy.deepcopy(value)
    usart["items"].extend(extras[key] for key in sorted(extras))
    digest = hashlib.sha256(
        json.dumps(registers, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return {"kind": "usart", "version": f"v1_gd{digest}"}, registers, blocks


def _project_i2c_v1_registers(
    native: dict[str, object], block: str, official: dict[str, object]
) -> tuple[dict[str, str], dict[str, object]]:
    names = {
        "CTL0": ("CR1", 0),
        "CTL1": ("CR2", 4),
        "SADDR0": ("OAR1", 8),
        "SADDR1": ("OAR2", 12),
        "DATA": ("DR", 16),
        "STAT0": ("SR1", 20),
        "STAT1": ("SR2", 24),
        "CKCFG": ("CCR", 28),
        "RT": ("TRISE", 32),
    }
    source = native.get(f"block/{block}")
    template = official.get("block/I2C")
    if not all(
        isinstance(value, dict) and isinstance(value.get("items"), list)
        for value in (source, template)
    ):
        raise ValueError("I2C v1 缺少原生或 STM32 block")
    assert isinstance(source, dict) and isinstance(template, dict)
    source_items = {
        str(item.get("name")): item
        for item in source["items"]
        if isinstance(item, dict)
    }
    if any(
        name not in source_items
        or int(source_items[name].get("byte_offset", -1)) != offset
        for name, (_, offset) in names.items()
    ):
        raise ValueError("I2C v1 基础寄存器布局不兼容")

    registers = copy.deepcopy(official)
    projected = copy.deepcopy(template)
    projected["items"] = [
        copy.deepcopy(item)
        for item in template["items"]
        if isinstance(item, dict) and item.get("name") != "FLTR"
    ]
    for item in source["items"]:
        if not isinstance(item, dict) or str(item.get("name")) in names:
            continue
        projected["items"].append(copy.deepcopy(item))
        fieldset = item.get("fieldset")
        if fieldset is not None and f"fieldset/{fieldset}" in native:
            registers[f"fieldset/{fieldset}"] = copy.deepcopy(
                native[f"fieldset/{fieldset}"]
            )
    registers["block/I2C"] = projected

    widths = {
        ("CR2", "FREQ"): ("CTL1", "I2CCLK"),
        ("TRISE", "TRISE"): ("RT", "RISETIME"),
    }
    for (target_set, target_field), (source_set, source_field) in widths.items():
        native_fields = native.get(f"fieldset/{source_set}", {}).get("fields", [])
        width = next(
            (
                int(field["bit_size"])
                for field in native_fields
                if isinstance(field, dict) and field.get("name") == source_field
            ),
            None,
        )
        target_fields = registers.get(f"fieldset/{target_set}", {}).get("fields", [])
        target = next(
            (
                field
                for field in target_fields
                if isinstance(field, dict) and field.get("name") == target_field
            ),
            None,
        )
        if width is None or target is None:
            raise ValueError("I2C v1 时钟字段布局不兼容")
        target["bit_size"] = width

    digest = hashlib.sha256(
        json.dumps(registers, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return {"kind": "i2c", "version": f"v1_gd{digest}", "block": "I2C"}, registers


def _classify_timer_block(registers: dict[str, object], block: str) -> str:
    value = registers.get(f"block/{block}")
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError(f"定时器 SVD IR 缺少 block/{block}")
    items = [item for item in value["items"] if isinstance(item, dict)]
    channel_offsets = {
        int(item["byte_offset"])
        for item in items
        if int(item.get("byte_offset", -1)) in {52, 56, 60, 64}
    }
    channels = len(channel_offsets)
    advanced = any(int(item.get("byte_offset", -1)) == 68 for item in items)
    if channels == 0:
        return "TIM_BASIC"
    if advanced:
        if channels >= 4:
            return "TIM_ADV"
        if channels == 2:
            return "TIM_2CH_CMP"
        if channels == 1:
            return "TIM_1CH_CMP"
    if channels == 2:
        return "TIM_2CH"
    if channels == 1:
        return "TIM_1CH"
    if channels != 4:
        raise ValueError(f"Embassy 不支持 {channels} 通道定时器块：{block}")
    counter = next(
        (item for item in items if int(item.get("byte_offset", -1)) == 36), None
    )
    if counter is None:
        raise ValueError(f"定时器缺少 CNT：{block}")
    width = int(counter.get("bit_size", 32))
    fieldset_name = counter.get("fieldset")
    if fieldset_name is not None:
        fieldset = registers.get(f"fieldset/{fieldset_name}")
        if not isinstance(fieldset, dict) or not isinstance(fieldset.get("fields"), list):
            raise ValueError(f"定时器 CNT fieldset 无效：{fieldset_name}")
        width = max(
            (
                int(field["bit_offset"]) + int(field["bit_size"])
                for field in fieldset["fields"]
                if isinstance(field, dict) and isinstance(field.get("bit_offset"), int)
            ),
            default=width,
        )
    return "TIM_GP32" if width > 16 else "TIM_GP16"


def _register_block_is_subset(
    native: dict[str, object],
    native_block: str,
    embassy: dict[str, object],
    embassy_block: str,
    signatures: dict[tuple[int, str], tuple[object, ...]] | None = None,
) -> bool:
    def signature(registers: dict[str, object], block: str) -> tuple[object, ...]:
        if signatures is None:
            return st_block_signature(registers, block)
        key = (id(registers), block)
        if key not in signatures:
            signatures[key] = st_block_signature(registers, block)
        return signatures[key]

    native_rows = signature(native, native_block)
    for offset, width, fields in signature(embassy, embassy_block):
        if not any(
            native_offset == offset
            and native_width == width
            and set(fields) <= set(native_fields)
            for native_offset, native_width, native_fields in native_rows
        ):
            return False
    return True


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


def _series_affinity(real: str, candidate: str) -> int:
    real_match = re.match(r"gd32([a-z]\d{3})", real.lower())
    candidate_match = re.match(r"stm32([a-z]\d{3})", candidate.lower())
    if real_match is None or candidate_match is None:
        return 2
    if real_match.group(1) == candidate_match.group(1):
        return 0
    return 1 if real_match.group(1)[0] == candidate_match.group(1)[0] else 2


def select_profile(
    model: dict[str, object],
    variant: dict[str, object],
    candidates: list[dict[str, object]],
    compatibility: dict[str, object],
) -> dict[str, object]:
    target = str(model.get("rust_target", ""))
    if target.startswith("riscv"):
        return {"profile": None, "status": "blocked", "reasons": "RISC-V 不适用"}
    expected_core = CORE_NAMES.get(str(model.get("core")))
    if expected_core is None:
        return {"profile": None, "status": "blocked", "reasons": "未知核心架构"}

    real = normalized_instance_names(variant)
    required = REQUIRED_SYSTEM_PERIPHERALS & real
    native_names = sorted(
        (
            str(instance["name"])
            for instance in variant.get("instances", [])
            if isinstance(instance, dict)
        ),
        key=lambda value: (-len(value), value),
    )
    base_interrupt_names = {
        _normalized_interrupt_name(
            str(interrupt["name"]),
            native_names,
            {name: 0 for name in real if name.startswith("DMA")},
        )
        for interrupt in variant.get("interrupts", [])
        if isinstance(interrupt, dict)
    }
    compatibility_by_layout = {
        str(row["id"]): row
        for row in compatibility.get("layouts", [])
        if isinstance(row, dict) and "id" in row
    }
    scored = []
    for candidate in candidates:
        cores = candidate.get("cores")
        if not isinstance(cores, list) or len(cores) != 1:
            continue
        if (
            not isinstance(cores[0], dict)
            or cores[0].get("name")
            not in CORE_PROFILE_COMPATIBILITY.get(expected_core, {expected_core})
        ):
            continue
        names = _candidate_names(candidate)
        if not required <= names:
            continue
        if any((peripheral in real) != (peripheral in names) for peripheral in ROUTING_PERIPHERALS):
            continue
        name = str(candidate.get("name", "")).lower()
        if not name.startswith("stm32"):
            continue
        peripherals = {
            str(peripheral["name"]): peripheral
            for peripheral in cores[0].get("peripherals", [])
            if isinstance(peripheral, dict) and "name" in peripheral
        }
        dma_channel_offsets = _dma_channel_offsets(peripherals)
        interrupt_names = {
            _offset_dma_interrupt_name(name, dma_channel_offsets)
            for name in base_interrupt_names
        }
        missing_interrupt_signals = 0
        for peripheral_name in real & peripherals.keys():
            bindings = [
                row
                for row in peripherals[peripheral_name].get("interrupts", [])
                if isinstance(row, dict)
            ]
            required_signals = {
                str(binding["signal"]).upper() for binding in bindings
            }
            projected_signals = {
                binding["signal"]
                for binding in _project_interrupt_bindings(
                    peripheral_name, bindings, interrupt_names
                )
            }
            missing_interrupt_signals += len(required_signals - projected_signals)
        register_matches = 0
        for instance in variant.get("instances", []):
            if not isinstance(instance, dict):
                continue
            mapped = _map_peripheral(str(instance["name"]))
            template = peripherals.get(mapped or "")
            layout = compatibility_by_layout.get(str(instance.get("layout", "")))
            if template is None or layout is None or not isinstance(template.get("registers"), dict):
                continue
            options = [
                option
                for key in ("exact_candidates", "subset_candidates")
                for option in layout.get(key, [])
                if isinstance(option, dict)
            ]
            if any(
                all(
                    option.get(key) == template["registers"].get(key)
                    for key in ("kind", "version", "block")
                )
                for option in options
            ):
                register_matches += 1
        score = (
            missing_interrupt_signals,
            -register_matches,
            len(real - names),
            _series_affinity(str(model.get("id", "")), name),
            len(names - real),
            name,
        )
        scored.append((score, name))
    if not scored:
        return {
            "profile": None,
            "status": "blocked",
            "reasons": "没有通过核心系统外设门的单核 STM32 profile",
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
    selections = {}
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
            series = re.match(r"gd32([a-z]\d{3})", device.lower())
            selection_key = (
                str(variant["id"]),
                str(model.get("core")),
                str(model.get("rust_target")),
                series.group(1) if series is not None else device,
            )
            selected = selections.get(selection_key)
            if selected is None:
                selected = select_profile(model, variant, candidates, compatibility)
                selections[selection_key] = selected
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


def project_chip(
    profile: dict[str, object],
    model: dict[str, object],
    variant: dict[str, object],
    facts: dict[str, object],
    *,
    native_chip: dict[str, object] | None = None,
) -> tuple[dict[str, object], list[str]]:
    profile_cores = profile.get("cores")
    if not isinstance(profile_cores, list) or len(profile_cores) != 1:
        raise ValueError("Embassy profile 必须是单核芯片")
    profile_peripherals = profile_cores[0].get("peripherals")
    if not isinstance(profile_peripherals, list):
        raise ValueError("Embassy profile 缺少外设")
    by_name = {
        str(peripheral["name"]): peripheral
        for peripheral in profile_peripherals
        if isinstance(peripheral, dict) and "name" in peripheral
    }
    native_core = None
    if native_chip is not None:
        native_cores = native_chip.get("cores")
        if not isinstance(native_cores, list) or len(native_cores) != 1:
            raise ValueError("原生 GD32 chip 必须是单核芯片")
        native_core = native_cores[0]
        if not isinstance(native_core, dict) or not isinstance(
            native_core.get("peripherals"), list
        ):
            raise ValueError("原生 GD32 chip 缺少外设")
        source_peripherals = list(native_core["peripherals"])
    else:
        source_peripherals = list(variant.get("instances", []))
    uid_addresses = {
        UID_ADDRESSES[str(source["name"])]
        for source in model.get("source_packs", [])
        if isinstance(source, dict) and str(source.get("name")) in UID_ADDRESSES
    }
    if len(uid_addresses) > 1:
        raise ValueError("同一芯片匹配到多个 UID 地址")
    if "UID" in by_name and uid_addresses and not any(
        isinstance(peripheral, dict) and str(peripheral.get("name")) == "UID"
        for peripheral in source_peripherals
    ):
        source_peripherals.append({"name": "UID", "address": uid_addresses.pop()})
    expected_names = {
        (embassy_instance_name(str(instance["name"])) or (str(instance["name"]), ""))[0]
        for instance in source_peripherals
        if isinstance(instance, dict)
    }
    missing_adc_common = {
        name
        for name in by_name
        if name.startswith("ADC") and name.endswith("_COMMON")
    } - expected_names

    projected_peripherals = []
    peripheral_pins = facts.get("peripheral_pins", {})
    peripheral_dma = facts.get("peripheral_dma_channels", {})
    peripheral_interrupts = facts.get("peripheral_interrupts", {})
    peripheral_registers = facts.get("peripheral_registers")
    if not all(
        isinstance(value, dict)
        for value in (peripheral_pins, peripheral_dma, peripheral_interrupts)
    ) or (peripheral_registers is not None and not isinstance(peripheral_registers, dict)):
        raise ValueError("GD32 外设 pin/DMA 事实格式无效")
    native_only = []
    embassy_usable = set()
    afio_usable = (
        peripheral_registers is None
        or "AFIO" in peripheral_registers
        or (
            native_chip is not None
            and facts.get("register_evidence") == "native-ir"
            and any(
                isinstance(instance, dict)
                and _map_peripheral(str(instance.get("name", ""))) == "AFIO"
                for instance in source_peripherals
            )
        )
    )
    seen = set()
    for instance in sorted(
        source_peripherals,
        key=lambda row: (str(row["name"]), int(row["address"])),
    ):
        if not isinstance(instance, dict):
            raise ValueError("GD32 变体包含无效外设实例")
        mapped = embassy_instance_name(str(instance["name"]))
        if mapped is None:
            name = str(instance["name"])
            if native_chip is None:
                native_only.append(name)
                continue
        else:
            name = mapped[0]
        if name in seen:
            raise ValueError(f"Embassy 实例名碰撞：{name}")
        seen.add(name)
        template = by_name.get(name)
        template_usable = template is not None
        registers = template.get("registers") if template is not None else None
        compatible_registers = (
            peripheral_registers.get(name)
            if isinstance(peripheral_registers, dict)
            else None
        )
        if (
            template_usable
            and isinstance(registers, dict)
            and peripheral_registers is not None
            and name not in SYSTEM_COMPAT_PERIPHERALS
            and re.fullmatch(r"(?:BDMA|GPDMA|LPDMA|MDMA|DMA)\d*", name) is None
            and compatible_registers is None
        ):
            template_usable = False
        if template_usable and template.get("afio") is not None and not afio_usable:
            template_usable = False
        if template_usable and (
            isinstance(registers, dict)
            and registers.get("kind") == "adc"
            and "VREFINTCAL" in by_name
            and "VREFINTCAL" not in expected_names
        ):
            template_usable = False
        if (
            template_usable
            and isinstance(registers, dict)
            and registers.get("kind") == "adc"
            and missing_adc_common
        ):
            template_usable = False
        interrupts = peripheral_interrupts.get(name, [])
        required_interrupts = {
            str(binding["signal"]).upper()
            for binding in (template or {}).get("interrupts", [])
            if isinstance(binding, dict) and "signal" in binding
        }
        available_interrupts = {
            str(binding["signal"]).upper()
            for binding in interrupts
            if isinstance(binding, dict) and "signal" in binding
        }
        if not required_interrupts.issubset(available_interrupts):
            template_usable = False
        if not template_usable and native_chip is None:
            native_only.append(name)
            continue
        peripheral = copy.deepcopy(template if template_usable else instance)
        if not template_usable:
            native_only.append(name)
        else:
            embassy_usable.add(name)
            if compatible_registers is not None:
                peripheral["registers"] = copy.deepcopy(compatible_registers)
        peripheral["name"] = name
        peripheral["address"] = int(instance["address"])
        peripheral["pins"] = copy.deepcopy(peripheral_pins.get(name, []))
        peripheral["dma_channels"] = copy.deepcopy(peripheral_dma.get(name, []))
        peripheral["interrupts"] = copy.deepcopy(interrupts)
        projected_peripherals.append(peripheral)

    projected_names = {str(peripheral["name"]) for peripheral in projected_peripherals}
    if native_chip is not None and projected_names != expected_names:
        missing = sorted(expected_names - projected_names)
        extra = sorted(projected_names - expected_names)
        raise ValueError(f"原生外设拓扑不一致：缺少={missing} 多出={extra}")
    required_system = {"RCC"}
    if "FLASH" in by_name:
        required_system.add("FLASH")
    missing_system = required_system - embassy_usable
    if missing_system:
        raise ValueError(
            "关键系统外设无法投影：" + ", ".join(sorted(missing_system))
        )
    if "AFIO" not in projected_names:
        for peripheral in projected_peripherals:
            peripheral.pop("afio", None)
    projected_pins = [
        copy.deepcopy(pin) for pin in facts.get("pins", []) if isinstance(pin, dict)
    ]
    missing_pin_ports = {
        f"GPIO{str(pin['name'])[1]}"
        for pin in projected_pins
        if len(str(pin.get("name", ""))) >= 2
        and f"GPIO{str(pin['name'])[1]}" not in projected_names
    }
    if missing_pin_ports:
        raise ValueError(
            "引脚引用不存在的 GPIO 端口：" + ", ".join(sorted(missing_pin_ports))
        )
    projected_pin_names = {str(pin["name"]) for pin in projected_pins}
    for peripheral in projected_peripherals:
        pins = peripheral.get("pins")
        if isinstance(pins, list):
            missing_pins = {
                str(pin.get("pin"))
                for pin in pins
                if isinstance(pin, dict) and str(pin.get("pin")) not in projected_pin_names
            }
            if missing_pins:
                raise ValueError(
                    f"{peripheral['name']} 引用不存在的引脚："
                    + ", ".join(sorted(missing_pins))
                )
    hardware_dma = sorted(
        {
            mapped
            for instance in variant.get("instances", [])
            if isinstance(instance, dict)
            for mapped in [_map_peripheral(str(instance["name"]))]
            if mapped is not None and re.fullmatch(r"DMA\d+", mapped)
        }
    )
    projected_dma = sorted(name for name in projected_names if re.fullmatch(r"DMA\d+", name))
    if hardware_dma and not projected_dma:
        raise ValueError("关键外设无法投影：" + ", ".join(hardware_dma))
    projected_dma_channels = [
        copy.deepcopy(channel)
        for channel in facts.get("dma_channels", [])
        if isinstance(channel, dict)
    ]
    projected_by_name = {
        str(peripheral["name"]): peripheral for peripheral in projected_peripherals
    }
    for channel in projected_dma_channels:
        dma_name = str(channel.get("dma"))
        dma_peripheral = projected_by_name.get(dma_name, {})
        dma_registers = dma_peripheral.get("registers")
        if not isinstance(dma_registers, dict) or dma_registers.get(
            "kind"
        ) not in EMBASSY_DMA_KINDS:
            raise ValueError(f"{dma_name} 无可用 Embassy 寄存器契约")
        signal = str(channel.get("name", "")).removeprefix(dma_name + "_")
        dma_interrupts = peripheral_interrupts.get(dma_name, [])
        if not any(
            isinstance(binding, dict)
            and str(binding.get("signal", "")).upper() == signal.upper()
            for binding in dma_interrupts
        ):
            raise ValueError(
                f"DMA channel {channel.get('name')} 缺少真实中断绑定"
            )
        references = {str(channel.get("dma"))}
        if channel.get("dmamux") is not None:
            references.add(str(channel["dmamux"]))
        if missing := references - projected_names:
            raise ValueError(
                f"DMA channel {channel.get('name')} 引用不存在的外设："
                + ", ".join(sorted(missing))
            )
    dma_channel_names = {
        str(channel["name"])
        for channel in projected_dma_channels
        if "name" in channel
    }
    for peripheral in projected_peripherals:
        bindings = peripheral.get("dma_channels")
        if isinstance(bindings, list):
            for binding in bindings:
                if not isinstance(binding, dict):
                    raise ValueError(f"{peripheral['name']} 包含无效 DMA 绑定")
                channel = binding.get("channel")
                dmamux = binding.get("dmamux")
                if channel is not None and str(channel) not in dma_channel_names:
                    raise ValueError(
                        f"{peripheral['name']} 引用不存在的 DMA channel：{channel}"
                    )
                if dmamux is not None and str(dmamux) not in projected_names:
                    raise ValueError(
                        f"{peripheral['name']} 引用不存在的 DMAMUX：{dmamux}"
                    )

    memory = facts.get("memory")
    if not isinstance(memory, list) or not memory:
        raise ValueError(f"{model.get('id')} 缺少规范化内存")
    projected = copy.deepcopy(profile)
    projected["name"] = str(model["id"])
    projected["family"] = "GD32"
    projected["line"] = str(variant.get("series", "GD32"))
    projected["memory"] = memory
    core = projected["cores"][0]
    core["name"] = CORE_NAMES.get(str(model.get("core")), core["name"])
    core["peripherals"] = sorted(
        projected_peripherals, key=lambda peripheral: str(peripheral["name"])
    )
    core["interrupts"] = copy.deepcopy(facts.get("interrupts", []))
    core["dma_channels"] = projected_dma_channels
    core["pins"] = projected_pins
    return projected, sorted(native_only)


def merge_patch(source: object, target: object) -> object:
    if not isinstance(source, dict) or not isinstance(target, dict):
        return copy.deepcopy(target)
    patch = {}
    for key in sorted(source.keys() - target.keys()):
        patch[key] = None
    for key in sorted(target):
        if key not in source:
            patch[key] = copy.deepcopy(target[key])
            continue
        if source[key] == target[key]:
            continue
        if isinstance(source[key], dict) and isinstance(target[key], dict):
            nested = merge_patch(source[key], target[key])
            if nested:
                patch[key] = nested
        else:
            patch[key] = copy.deepcopy(target[key])
    return patch


def _mapped_signal(peripheral: str, signal: str) -> str:
    if peripheral.startswith("TIMER"):
        match = re.fullmatch(r"CH(\d+)(.*)", signal)
        if match is not None:
            signal = f"CH{int(match.group(1)) + 1}{match.group(2)}"
        if signal.endswith("_ON"):
            return signal.removesuffix("_ON") + "N"
        return {"ETI": "ETR", "BRKIN": "BKIN"}.get(signal, signal)
    return signal


@cache
def _map_peripheral(name: str) -> str | None:
    mapped = embassy_instance_name(name)
    return mapped[0] if mapped is not None else None


def _dma_channel_offsets(
    peripherals: dict[str, dict[str, object]],
) -> dict[str, int]:
    return {
        name: (
            0
            if any(
                str(binding.get("signal", "")).upper() == "CH0"
                for binding in peripheral.get("interrupts", [])
                if isinstance(binding, dict)
            )
            else 1
        )
        for name, peripheral in peripherals.items()
        if name.startswith("DMA")
    }


def _normalized_interrupt_name(
    name: str,
    native_names: list[str],
    dma_channel_offsets: dict[str, int] | None = None,
) -> str:
    dma_channel_offsets = dma_channel_offsets or {}
    mappings = {
        native: mapped
        for native in native_names
        if (mapped := _map_peripheral(native)) is not None
    }
    shared = re.fullmatch(r"([A-Za-z]+)(\d+)_(\d+)", name)
    if shared is not None:
        first = f"{shared.group(1)}{shared.group(2)}"
        second = f"{shared.group(1)}{shared.group(3)}"
        if first in mappings and second in mappings:
            name = f"{first}_{second}"
    pattern = "|".join(
        re.escape(native)
        for native in sorted(mappings, key=lambda value: (-len(value), value))
    )
    normalized = (
        re.sub(
            rf"(?<![A-Za-z0-9])({pattern})(?![A-Za-z0-9])",
            lambda match: mappings[match.group(1)],
            name,
        )
        if pattern
        else name
    )
    normalized = normalized.upper()
    normalized = {
        "EXTI5_9": "EXTI9_5",
        "EXTI10_15": "EXTI15_10",
    }.get(normalized, normalized)
    normalized = re.sub(
        r"\b([A-Z]+)(\d+)_\1(\d+)\b",
        lambda match: f"{match.group(1)}{match.group(2)}_{match.group(3)}",
        normalized,
    )
    normalized = re.sub(r"\b(TIM\d+)_CHANNEL(?=_|$)", r"\1_CC", normalized)
    normalized = re.sub(r"\b(TIM\d+)_TRG_CMT(?=_|$)", r"\1_TRG_COM", normalized)
    normalized = re.sub(r"\b(CAN\d+)_EWMC\b", r"\1_SCE", normalized)

    return _offset_dma_interrupt_name(normalized, dma_channel_offsets)


def _offset_dma_interrupt_name(
    normalized: str, dma_channel_offsets: dict[str, int]
) -> str:
    combined = re.fullmatch(r"(DMA\d+)_CHANNEL(\d+)_CHANNEL(\d+)", normalized)
    if combined is None:
        combined = re.fullmatch(r"(DMA\d+)_CHANNEL(\d+)_(\d+)", normalized)
    if combined is not None:
        offset = dma_channel_offsets.get(combined.group(1), 1)
        return (
            f"{combined.group(1)}_CHANNEL{int(combined.group(2)) + offset}_"
            f"{int(combined.group(3)) + offset}"
        )
    return re.sub(
        r"\b(DMA\d+)_CHANNEL(\d+)\b",
        lambda match: (
            f"{match.group(1)}_CHANNEL"
            f"{int(match.group(2)) + dma_channel_offsets.get(match.group(1), 1)}"
        ),
        normalized,
    )


def _project_interrupt_bindings(
    peripheral: str,
    bindings: list[dict[str, object]],
    actual_names: set[str],
) -> list[dict[str, str]]:
    projected = []
    token = re.compile(rf"(?:^|_){re.escape(peripheral.upper())}(?:_|$)")
    for binding in bindings:
        signal = str(binding["signal"]).upper()
        expected = str(binding["interrupt"]).upper()
        exti_interrupt = None
        if peripheral == "EXTI" and (line_match := re.fullmatch(r"EXTI(\d+)", signal)):
            line = int(line_match.group(1))
            for name in sorted(actual_names):
                range_match = re.fullmatch(r"EXTI(\d+)_(\d+)", name)
                if range_match is not None:
                    first, last = map(int, range_match.groups())
                    if min(first, last) <= line <= max(first, last):
                        exti_interrupt = name
                        break
        exact = (
            f"{peripheral.upper()}_CHANNEL{int(signal.removeprefix('CH'))}"
            if peripheral.startswith("DMA") and re.fullmatch(r"CH\d+", signal)
            else peripheral.upper() if signal == "GLOBAL" else ""
        )
        candidates = sorted(
            name
            for name in actual_names
            if token.search(name)
            and (signal == "GLOBAL" or all(part in name for part in signal.split("_")))
        )
        interrupt = (
            exact
            if exact in actual_names
            else expected
            if expected in actual_names
            else exti_interrupt
            if exti_interrupt is not None
            else candidates[0]
            if len(candidates) == 1
            else None
        )
        if interrupt is not None:
            projected.append({"signal": signal, "interrupt": interrupt})
    return projected


def _dma_channel_interrupt_name(
    channel: str, actual_names: set[str]
) -> str | None:
    match = re.fullmatch(r"(DMA\d+)_CH(\d+)", channel)
    if match is None:
        return None
    dma, number = match.group(1), int(match.group(2))
    exact = f"{dma}_CHANNEL{number}"
    if exact in actual_names:
        return exact
    candidates = []
    for name in actual_names:
        combined = re.fullmatch(rf"{re.escape(dma)}_CHANNEL(\d+(?:_\d+)*)", name)
        if combined is not None and number in {
            int(value) for value in combined.group(1).split("_")
        }:
            candidates.append(name)
    return candidates[0] if len(candidates) == 1 else None


def build_projection_facts(
    profile: dict[str, object],
    variant: dict[str, object],
    memory: dict[str, object],
    pins: dict[str, object],
    dma: dict[str, object],
    compatibility: dict[str, object] | None = None,
    native_chip: dict[str, object] | None = None,
    native_registers: dict[str, dict[str, object]] | None = None,
    official_registers: dict[str, dict[str, object]] | None = None,
    register_signatures: dict[tuple[int, str], tuple[object, ...]] | None = None,
) -> dict[str, object]:
    if pins.get("status") != "normalized" or dma.get("status") != "normalized":
        raise ValueError("GD32 pins 或 DMA 事实未规范化")
    regions = memory.get("memory")
    if not isinstance(regions, list) or not regions:
        raise ValueError("GD32 内存事实未规范化")
    profile_cores = profile.get("cores")
    if not isinstance(profile_cores, list) or len(profile_cores) != 1:
        raise ValueError("Embassy profile 必须是单核芯片")
    flash_regions = sorted(
        (
            region
            for region in regions
            if isinstance(region, dict) and region.get("kind") == "flash"
        ),
        key=lambda region: int(region["address"]),
    )
    flash_groups: dict[str, list[int]] = {}
    for region in flash_regions:
        flash_groups.setdefault(
            str(region.get("bank", region["address"])), []
        ).append(int(region["address"]))
    flash_names = {}
    for bank_index, addresses in enumerate(flash_groups.values(), 1):
        for region_index, address in enumerate(addresses, 1):
            flash_names[address] = (
                f"BANK_{bank_index}"
                if len(addresses) == 1
                else f"BANK_{bank_index}_REGION_{region_index}"
            )
    ram_regions = {
        address: index
        for index, address in enumerate(
            sorted(
                {
                    int(region["address"])
                    for region in regions
                    if isinstance(region, dict) and region.get("kind") == "ram"
                }
            ),
            1,
        )
    }
    memory_rows = []
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("GD32 内存区域无效")
        kind = str(region["kind"])
        address = int(region["address"])
        row = {
            "name": (
                flash_names[address]
                if kind == "flash"
                else "SRAM"
                if kind == "ram" and ram_regions[address] == 1
                else f"SRAM{ram_regions[address]}"
                if kind == "ram"
                else str(region["name"])
            ),
            "kind": kind,
            "address": address,
            "size": int(region["size"]),
        }
        if region.get("settings") is not None:
            row["settings"] = copy.deepcopy(region["settings"])
        memory_rows.append(row)

    native_cores = native_chip.get("cores") if native_chip is not None else None
    native_instances = (
        native_cores[0].get("peripherals", [])
        if isinstance(native_cores, list)
        and len(native_cores) == 1
        and isinstance(native_cores[0], dict)
        else variant.get("instances", [])
    )
    native_names = sorted(
        (
            str(instance["name"])
            for instance in native_instances
            if isinstance(instance, dict)
        ),
        key=lambda name: (-len(name), name),
    )
    profile_peripherals = {
        str(peripheral["name"]): peripheral
        for peripheral in profile_cores[0].get("peripherals", [])
        if isinstance(peripheral, dict) and "name" in peripheral
    }
    peripheral_registers = {}
    generated_registers = {}
    if native_registers is not None and official_registers is not None:
        usart_sources = {}
        usart_official = None
        for instance in native_instances:
            if not isinstance(instance, dict) or not isinstance(
                instance.get("registers"), dict
            ):
                continue
            peripheral = _map_peripheral(str(instance["name"]))
            template = profile_peripherals.get(peripheral or "")
            template_registers = template.get("registers") if isinstance(template, dict) else None
            if not isinstance(template_registers, dict) or (
                template_registers.get("kind"), template_registers.get("version")
            ) != ("usart", "v1"):
                continue
            source = instance["registers"]
            key = (str(source["kind"]), str(source["version"]), str(source["block"]))
            native = native_registers.get(f"{key[0]}_{key[1]}")
            usart_official = official_registers.get("usart_v1")
            if native is not None and usart_official is not None:
                usart_sources[key] = native
        try:
            usart_reference, usart_registers, usart_blocks = (
                _project_usart_v1_registers(usart_sources, usart_official)
                if usart_sources and usart_official is not None
                else (None, None, {})
            )
        except ValueError:
            usart_reference, usart_registers, usart_blocks = None, None, {}

        for instance in native_instances:
            if not isinstance(instance, dict):
                continue
            peripheral = _map_peripheral(str(instance["name"]))
            template = profile_peripherals.get(peripheral or "")
            source = instance.get("registers")
            if template is None or not isinstance(template.get("registers"), dict):
                continue
            if not isinstance(source, dict):
                continue
            template_registers = template["registers"]
            native = native_registers.get(f"{source.get('kind')}_{source.get('version')}")
            official = official_registers.get(
                f"{template_registers.get('kind')}_{template_registers.get('version')}"
            )
            if native is None or official is None:
                continue
            selected = copy.deepcopy(template_registers)
            if template_registers.get("kind") == "timer":
                selected["block"] = _classify_timer_block(native, str(source["block"]))
            elif not _register_block_is_subset(
                native,
                str(source["block"]),
                official,
                str(template_registers["block"]),
                register_signatures,
            ):
                if (
                    template_registers.get("kind") == "usart"
                    and template_registers.get("version") == "v1"
                ):
                    key = (
                        str(source["kind"]),
                        str(source["version"]),
                        str(source["block"]),
                    )
                    if usart_reference is None or usart_registers is None or key not in usart_blocks:
                        continue
                    selected = {**usart_reference, "block": usart_blocks[key]}
                    registers = usart_registers
                elif template_registers.get("kind") == "exti":
                    try:
                        selected, registers = _project_exti_registers(
                            native, str(source["block"])
                        )
                    except ValueError:
                        continue
                elif (
                    template_registers.get("kind") == "i2c"
                    and template_registers.get("version") == "v1"
                ):
                    try:
                        selected, registers = _project_i2c_v1_registers(
                            native, str(source["block"]), official
                        )
                    except ValueError:
                        continue
                elif template_registers.get("kind") == "dmamux":
                    selected, registers = _project_dmamux_registers(
                        native, str(source["block"])
                    )
                else:
                    continue
                generated_registers[
                    f"{selected['kind']}_{selected['version']}.json"
                ] = registers
            peripheral_registers[str(peripheral)] = selected
    else:
        compatible_layouts = {
            str(row["id"]): row
            for row in (compatibility or {}).get("layouts", [])
            if isinstance(row, dict) and "id" in row
        }
        register_records = []
        for instance in variant.get("instances", []):
            if not isinstance(instance, dict):
                continue
            peripheral = _map_peripheral(str(instance["name"]))
            template = profile_peripherals.get(peripheral or "")
            layout = compatible_layouts.get(str(instance.get("layout", "")))
            if template is None or not isinstance(template.get("registers"), dict):
                continue
            template_registers = template["registers"]
            candidates = [
                candidate
                for key in ("exact_candidates", "subset_candidates")
                for candidate in (layout or {}).get(key, [])
                if isinstance(candidate, dict)
                and candidate.get("kind") == template_registers.get("kind")
            ]
            if candidates:
                register_records.append((peripheral, template_registers, candidates))

        versions_by_kind: dict[str, list[set[str]]] = {}
        preferred_versions: dict[str, list[str]] = {}
        for _, template, candidates in register_records:
            kind = str(template["kind"])
            versions_by_kind.setdefault(kind, []).append(
                {str(candidate["version"]) for candidate in candidates}
            )
            preferred_versions.setdefault(kind, []).append(str(template["version"]))
        selected_versions = {}
        for kind, choices in versions_by_kind.items():
            common = set.intersection(*choices)
            if not common:
                raise ValueError(f"{kind} 外设没有芯片内共同兼容版本")
            selected_versions[kind] = min(
                common,
                key=lambda version: (-preferred_versions[kind].count(version), version),
            )

        for peripheral, template, candidates in register_records:
            selected = min(
                (
                    candidate
                    for candidate in candidates
                    if candidate["version"] == selected_versions[str(template["kind"])]
                ),
                key=lambda candidate: (
                    candidate.get("block") != template.get("block"),
                    -int(candidate.get("fields", 0)),
                    -int(candidate.get("registers", 0)),
                    str(candidate.get("block", "")),
                ),
            )
            peripheral_registers[str(peripheral)] = {
                key: selected[key] for key in ("kind", "version", "block")
            }
    dma_channel_offsets = _dma_channel_offsets(profile_peripherals)
    dma_request_instances = {
        match.group(1)
        for peripheral in profile_peripherals.values()
        for binding in peripheral.get("dma_channels", [])
        if isinstance(binding, dict) and binding.get("request") is not None
        for match in [re.match(r"(DMA\d+)_CH\d+$", str(binding.get("channel", "")))]
        if match is not None
    }
    interrupts = [
        {
            "name": _normalized_interrupt_name(
                str(interrupt["name"]), native_names, dma_channel_offsets
            ),
            "number": int(interrupt["value"]),
        }
        for interrupt in variant.get("interrupts", [])
        if isinstance(interrupt, dict)
    ]
    interrupt_names = {str(interrupt["name"]) for interrupt in interrupts}
    if len(interrupt_names) != len(interrupts):
        raise ValueError("GD32 中断名归一后发生碰撞")

    peripheral_interrupts = {
        name: _project_interrupt_bindings(
            name,
            [row for row in peripheral.get("interrupts", []) if isinstance(row, dict)],
            interrupt_names,
        )
        for name, peripheral in profile_peripherals.items()
    }

    projected_pins = set()
    peripheral_pins: dict[str, set[tuple[str, str]]] = {}
    for pin in pins.get("pins", []):
        if not isinstance(pin, dict):
            raise ValueError("GD32 pin 事实无效")
        pin_name = str(pin["name"]).split("-", 1)[0]
        if re.fullmatch(r"P[A-Z]\d+", pin_name) is None:
            continue
        projected_pins.add(pin_name)
        for function in pin.get("functions", []):
            if not isinstance(function, dict):
                continue
            function_name = str(function["name"])
            native = next(
                (
                    name
                    for name in native_names
                    if function_name == name or function_name.startswith(name + "_")
                ),
                None,
            )
            if native is None:
                continue
            peripheral = _map_peripheral(native)
            if peripheral is None:
                continue
            signal = function_name[len(native) :].removeprefix("_") or peripheral
            peripheral_pins.setdefault(peripheral, set()).add(
                (pin_name, _mapped_signal(native, signal))
            )

    dma_channels = []
    channel_names = {}
    variant_dmamuxes = {
        mapped
        for instance in variant.get("instances", [])
        if isinstance(instance, dict)
        for mapped in [_map_peripheral(str(instance["name"]))]
        if mapped is not None and mapped.startswith("DMAMUX")
    }
    for channel in dma.get("dma_channels", []):
        if not isinstance(channel, dict):
            raise ValueError("GD32 DMA channel 事实无效")
        native_dma = str(channel["dma"])
        projected_dma = _map_peripheral(native_dma)
        if projected_dma is None:
            continue
        index = int(channel["channel"])
        name = f"{projected_dma}_CH{index + dma_channel_offsets.get(projected_dma, 1)}"
        channel_names[(native_dma, index)] = name
        projected_channel = {"name": name, "dma": projected_dma, "channel": index}
        if channel.get("dmamux") is not None:
            dmamux = _map_peripheral(str(channel["dmamux"]))
            if dmamux is None:
                raise ValueError(f"无法映射 DMAMUX：{channel['dmamux']}")
            projected_channel["dmamux"] = dmamux
        elif dma.get("kind") == "dmamux" and len(variant_dmamuxes) == 1:
            projected_channel["dmamux"] = next(iter(variant_dmamuxes))
        if "dmamux" in projected_channel:
            if channel.get("dmamux_channel") is None:
                raise ValueError(f"{channel['name']} 缺少真实 DMAMUX 通道索引")
            projected_channel["dmamux_channel"] = int(channel["dmamux_channel"])
        dma_channels.append(projected_channel)

    for channel in dma_channels:
        dma_name = str(channel["dma"])
        signal = str(channel["name"]).removeprefix(dma_name + "_")
        bindings = peripheral_interrupts.setdefault(dma_name, [])
        if any(str(binding.get("signal")) == signal for binding in bindings):
            continue
        interrupt = _dma_channel_interrupt_name(str(channel["name"]), interrupt_names)
        if interrupt is not None:
            bindings.append({"signal": signal, "interrupt": interrupt})

    peripheral_dma: dict[str, set[tuple[object, ...]]] = {}
    for request in dma.get("dma_requests", []):
        if not isinstance(request, dict) or not isinstance(request.get("binding"), dict):
            continue
        binding = request["binding"]
        if binding.get("kind") != "peripheral":
            continue
        native = str(binding["peripheral"])
        peripheral = _map_peripheral(native)
        if peripheral is None:
            continue
        signal = _mapped_signal(native, str(binding["signal"]))
        if dma.get("kind") == "dmamux" and request.get("request") is not None:
            dmamuxes = {
                str(channel["dmamux"])
                for channel in dma_channels
                if channel.get("dmamux") is not None
            }
            if len(dmamuxes) != 1:
                raise ValueError("DMAMUX 请求无法确定唯一 DMAMUX 实例")
            peripheral_dma.setdefault(peripheral, set()).add(
                ("request", signal, dmamuxes.pop(), int(request["request"]))
            )
            continue
        channel = channel_names.get((str(request["dma"]), int(request["channel"])))
        if channel is not None:
            requires_request = projected_dma in dma_request_instances
            request_number = request.get("request")
            if requires_request and request_number is None:
                continue
            peripheral_dma.setdefault(peripheral, set()).add(
                (
                    "channel",
                    signal,
                    channel,
                    int(request_number) if requires_request else -1,
                )
            )

    return {
        "memory": [memory_rows],
        "interrupts": interrupts,
        "pins": [{"name": name} for name in sorted(projected_pins)],
        "peripheral_pins": {
            peripheral: [
                {"pin": pin, "signal": signal}
                for pin, signal in sorted(entries)
            ]
            for peripheral, entries in sorted(peripheral_pins.items())
        },
        "dma_channels": sorted(dma_channels, key=lambda row: str(row["name"])),
        "peripheral_dma_channels": {
            peripheral: [
                (
                    {
                        "signal": entry[1],
                        "channel": entry[2],
                        **({"request": entry[3]} if entry[3] >= 0 else {}),
                    }
                    if entry[0] == "channel"
                    else {
                        "signal": entry[1],
                        "dmamux": entry[2],
                        "request": entry[3],
                    }
                )
                for entry in sorted(entries)
            ]
            for peripheral, entries in sorted(peripheral_dma.items())
        },
        "peripheral_interrupts": peripheral_interrupts,
        "peripheral_registers": peripheral_registers,
        "generated_registers": generated_registers,
        "register_evidence": (
            "native-ir"
            if native_registers is not None and official_registers is not None
            else "firmware-layout"
        ),
    }


def build_projection_manifest(
    profile_report: dict[str, object], inputs: dict[str, object]
) -> dict[str, object]:
    rows = profile_report.get("profiles")
    if not isinstance(rows, list):
        raise ValueError("Embassy profile 报告格式无效")
    source_hashes = inputs.get("source_hashes", {})
    if not isinstance(source_hashes, dict):
        raise ValueError("投影来源哈希格式无效")
    projections = []
    generated_registers = {}
    for row in sorted(rows, key=lambda item: str(item["chip"])):
        if not isinstance(row, dict):
            raise ValueError("Embassy profile 记录无效")
        chip = str(row["chip"])
        entry = {
            "chip": chip,
            "profile": row.get("profile"),
            "rust_target": row.get("rust_target"),
            "status": row.get("status"),
            "source_hashes": copy.deepcopy(source_hashes),
        }
        if row.get("status") != "projected":
            entry["reasons"] = str(row.get("reasons", "profile 未投影"))
            projections.append(entry)
            continue
        try:
            model = inputs["models"][chip]
            variant = inputs["variants"][str(row["variant"])]
            profile = inputs["official_profiles"][str(row["profile"])]
            native_chip = inputs["native_chips"][chip]
            rcu = inputs["rcu"][chip]
            if (
                rcu.get("status") != "normalized"
                or rcu.get("binding_status") != "normalized"
                or rcu.get("gate_status") != "normalized"
            ):
                raise ValueError("GD32 RCU 事实未规范化")
            facts = build_projection_facts(
                profile,
                variant,
                inputs["memory"][chip],
                inputs["pins"][chip],
                inputs["dma"][chip],
                inputs.get("compatibility"),
                native_chip,
                inputs.get("native_registers"),
                inputs.get("official_registers"),
                inputs.get("register_signatures"),
            )
            for filename, registers in facts.get("generated_registers", {}).items():
                previous = generated_registers.get(filename)
                if previous is not None and previous != registers:
                    raise ValueError(f"兼容寄存器文件名碰撞：{filename}")
                generated_registers[filename] = registers
            projected, native_only = project_chip(
                profile, model, variant, facts, native_chip=native_chip
            )
            entry["patch"] = merge_patch(profile, projected)
            entry["native_pac_only_peripherals"] = native_only
        except (KeyError, TypeError, ValueError) as error:
            entry["status"] = "blocked"
            entry["reasons"] = str(error)
        projections.append(entry)
    return {
        "schema_version": 1,
        "summary": {
            "devices": len(projections),
            "projected": sum(row["status"] == "projected" for row in projections),
            "blocked": sum(row["status"] == "blocked" for row in projections),
        },
        "registers": generated_registers,
        "projections": projections,
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


def _load_registers(directory: Path) -> dict[str, dict[str, object]]:
    registers = {}
    for path in sorted(directory.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"register IR 不是对象：{path}")
        registers[path.stem] = value
    if not registers:
        raise ValueError(f"没有 register IR：{directory}")
    return registers


def _index_rows(rows: object, key: str) -> dict[str, dict[str, object]]:
    if not isinstance(rows, list):
        raise ValueError(f"投影输入缺少 {key} 列表")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or key not in row:
            raise ValueError(f"投影输入包含无效 {key} 记录")
        value = str(row[key]).lower()
        if value in indexed:
            raise ValueError(f"投影输入包含重复 {key}：{value}")
        indexed[value] = row
    return indexed


def _write_projection_registers(
    output: Path, registers: dict[str, object]
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".embassy-registers-", dir=output.parent) as name:
        temporary = Path(name) / "data"
        register_dir = temporary / "registers"
        register_dir.mkdir(parents=True)
        for filename, value in sorted(registers.items()):
            if re.fullmatch(r"[a-z0-9_-]+\.json", filename) is None:
                raise ValueError(f"兼容寄存器文件名无效：{filename}")
            (register_dir / filename).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        common._write_text_atomic(
            temporary / ".m32-embassy-registers.json",
            json.dumps(
                {"schema_version": 1, "registers": len(registers)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        if output.exists():
            marker = output / ".m32-embassy-registers.json"
            if not marker.is_file():
                raise ValueError(f"兼容寄存器输出缺少生成标记：{output}")
            shutil.rmtree(output)
        temporary.rename(output)


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
        "--native-chips",
        type=Path,
        default=root / ".cache/generated/gigadevice-stm32-data-v1/chips",
    )
    parser.add_argument(
        "--native-registers",
        type=Path,
        default=root / ".cache/generated/gigadevice-stm32-data-v1/registers",
    )
    parser.add_argument(
        "--official-registers",
        type=Path,
        default=root / ".cache/research/repos/stm32-data-generated/data/registers",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "reports/gigadevice-embassy-profiles.json",
    )
    parser.add_argument(
        "--memory",
        type=Path,
        default=root / ".cache/normalized/gigadevice-memory.json",
    )
    parser.add_argument(
        "--pins",
        type=Path,
        default=root / ".cache/normalized/gigadevice-pins.json",
    )
    parser.add_argument(
        "--dma",
        type=Path,
        default=root / ".cache/normalized/gigadevice-dma.json",
    )
    parser.add_argument(
        "--rcu",
        type=Path,
        default=root / ".cache/normalized/gigadevice-rcu.json",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--registers-output",
        type=Path,
        default=root / ".cache/generated/gigadevice-embassy-registers-v1",
    )
    args = parser.parse_args()
    models = json.loads(args.models.read_text(encoding="utf-8"))
    variants = json.loads(args.variants.read_text(encoding="utf-8"))
    compatibility = json.loads(args.register_compat.read_text(encoding="utf-8"))
    candidates = _load_candidates(args.official_chips)
    native_chips = _load_candidates(args.native_chips)
    native_registers = _load_registers(args.native_registers)
    official_registers = _load_registers(args.official_registers)
    report = build_profile_report(
        models,
        variants,
        candidates,
        compatibility,
    )
    report["provenance"] = {
        "models_sha256": common._sha256(args.models),
        "variants_sha256": common._sha256(args.variants),
        "register_compat_sha256": common._sha256(args.register_compat),
        "official_chips_tree_sha256": common.tree_sha256(args.official_chips),
        "native_chips_tree_sha256": common.tree_sha256(args.native_chips),
        "native_registers_tree_sha256": common.tree_sha256(args.native_registers),
        "official_registers_tree_sha256": common.tree_sha256(args.official_registers),
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    if args.manifest is not None:
        memory = json.loads(args.memory.read_text(encoding="utf-8"))
        pins = json.loads(args.pins.read_text(encoding="utf-8"))
        dma = json.loads(args.dma.read_text(encoding="utf-8"))
        rcu = json.loads(args.rcu.read_text(encoding="utf-8"))
        source_hashes = {
            "models_sha256": common._sha256(args.models),
            "variants_sha256": common._sha256(args.variants),
            "memory_sha256": common._sha256(args.memory),
            "pins_sha256": common._sha256(args.pins),
            "dma_sha256": common._sha256(args.dma),
            "rcu_sha256": common._sha256(args.rcu),
            "register_compat_sha256": common._sha256(args.register_compat),
            "official_chips_tree_sha256": common.tree_sha256(args.official_chips),
            "native_chips_tree_sha256": common.tree_sha256(args.native_chips),
            "native_registers_tree_sha256": common.tree_sha256(args.native_registers),
            "official_registers_tree_sha256": common.tree_sha256(args.official_registers),
        }
        manifest = build_projection_manifest(
            report,
            {
                "models": _index_rows(models.get("devices"), "id"),
                "variants": _index_rows(variants.get("variants"), "id"),
                "official_profiles": _index_rows(candidates, "name"),
                "native_chips": _index_rows(native_chips, "name"),
                "native_registers": native_registers,
                "official_registers": official_registers,
                "register_signatures": {},
                "memory": _index_rows(memory.get("profiles"), "device"),
                "pins": _index_rows(pins.get("devices"), "id"),
                "dma": _index_rows(dma.get("devices"), "id"),
                "rcu": _index_rows(rcu.get("devices"), "id"),
                "compatibility": compatibility,
                "source_hashes": source_hashes,
            },
        )
        common._write_text_atomic(
            args.manifest,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        _write_projection_registers(args.registers_output, manifest["registers"])
        print(
            "manifest "
            + " ".join(f"{key}={value}" for key, value in manifest["summary"].items())
        )
    print(" ".join(f"{key}={value}" for key, value in report["summary"].items()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
