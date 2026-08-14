#!/usr/bin/env python3
"""从 GD32 规范事实生成 Embassy 兼容投影。"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

import gigadevice_sources as common
from normalize_gigadevice_embassy_names import embassy_instance_name
from generate_gigadevice_stm32_data import CORE_NAMES


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
    compatibility_by_layout = {
        str(row["id"]): row
        for row in compatibility.get("layouts", [])
        if isinstance(row, dict) and "id" in row
    }
    scored = []
    matching_core = False
    for candidate in candidates:
        cores = candidate.get("cores")
        if (
            not isinstance(cores, list)
            or len(cores) != 1
            or cores[0].get("name") != expected_core
        ):
            continue
        matching_core = True
        names = _candidate_names(candidate)
        if not required <= names:
            continue
        name = str(candidate.get("name", "")).lower()
        if not name.startswith("stm32"):
            continue
        peripherals = {
            str(peripheral["name"]): peripheral
            for peripheral in cores[0].get("peripherals", [])
            if isinstance(peripheral, dict) and "name" in peripheral
        }
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
            len(real - names),
            _series_affinity(str(model.get("id", "")), name),
            -register_matches,
            len(names - real),
            name,
        )
        scored.append((score, name))
    if not scored:
        return {
            "profile": None,
            "status": "blocked",
            "reasons": (
                "没有相同核心架构的 STM32 profile"
                if not matching_core
                else "没有通过核心系统外设门的 STM32 profile"
            ),
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

    projected_peripherals = []
    peripheral_pins = facts.get("peripheral_pins", {})
    peripheral_dma = facts.get("peripheral_dma_channels", {})
    peripheral_registers = facts.get("peripheral_registers", {})
    if not all(
        isinstance(value, dict)
        for value in (peripheral_pins, peripheral_dma, peripheral_registers)
    ):
        raise ValueError("GD32 外设 pin/DMA 事实格式无效")
    unsupported = []
    seen = set()
    for instance in sorted(
        variant.get("instances", []),
        key=lambda row: (str(row["name"]), int(row["address"])),
    ):
        if not isinstance(instance, dict):
            raise ValueError("GD32 变体包含无效外设实例")
        mapped = embassy_instance_name(str(instance["name"]))
        if mapped is None:
            unsupported.append(str(instance["name"]))
            continue
        name = mapped[0]
        if name in seen:
            raise ValueError(f"Embassy 实例名碰撞：{name}")
        seen.add(name)
        template = by_name.get(name)
        if template is None:
            unsupported.append(name)
            continue
        peripheral = copy.deepcopy(template)
        peripheral["name"] = name
        peripheral["address"] = int(instance["address"])
        peripheral["pins"] = copy.deepcopy(peripheral_pins.get(name, []))
        peripheral["dma_channels"] = copy.deepcopy(peripheral_dma.get(name, []))
        if name in peripheral_registers:
            peripheral["registers"] = copy.deepcopy(peripheral_registers[name])
        projected_peripherals.append(peripheral)

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
    core["dma_channels"] = copy.deepcopy(facts.get("dma_channels", []))
    core["pins"] = copy.deepcopy(facts.get("pins", []))
    return projected, sorted(unsupported)


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
            return f"CH{int(match.group(1)) + 1}{match.group(2)}"
    return signal


def _map_peripheral(name: str) -> str | None:
    mapped = embassy_instance_name(name)
    return mapped[0] if mapped is not None else None


def build_projection_facts(
    profile: dict[str, object],
    variant: dict[str, object],
    memory: dict[str, object],
    pins: dict[str, object],
    dma: dict[str, object],
    compatibility: dict[str, object] | None = None,
) -> dict[str, object]:
    if pins.get("status") != "normalized" or dma.get("status") != "normalized":
        raise ValueError("GD32 pins 或 DMA 事实未规范化")
    regions = memory.get("memory")
    if not isinstance(regions, list) or not regions:
        raise ValueError("GD32 内存事实未规范化")
    profile_cores = profile.get("cores")
    if not isinstance(profile_cores, list) or len(profile_cores) != 1:
        raise ValueError("Embassy profile 必须是单核芯片")
    profile_memory = profile.get("memory")
    template_regions = (
        profile_memory[0]
        if isinstance(profile_memory, list)
        and profile_memory
        and isinstance(profile_memory[0], list)
        else []
    )
    settings_by_kind = {
        str(region["kind"]): copy.deepcopy(region.get("settings"))
        for region in template_regions
        if isinstance(region, dict) and region.get("settings") is not None
    }
    memory_rows = []
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("GD32 内存区域无效")
        kind = str(region["kind"])
        row = {
            "name": "BANK_1" if kind == "flash" else "SRAM" if kind == "ram" else str(region["name"]),
            "kind": kind,
            "address": int(region["address"]),
            "size": int(region["size"]),
        }
        if kind in settings_by_kind:
            row["settings"] = settings_by_kind[kind]
        memory_rows.append(row)

    profile_interrupts = {
        int(interrupt["number"]): str(interrupt["name"])
        for interrupt in profile_cores[0].get("interrupts", [])
        if isinstance(interrupt, dict)
    }
    interrupts = [
        {
            "name": profile_interrupts.get(int(interrupt["value"]), str(interrupt["name"])),
            "number": int(interrupt["value"]),
        }
        for interrupt in variant.get("interrupts", [])
        if isinstance(interrupt, dict)
    ]

    profile_peripherals = {
        str(peripheral["name"]): peripheral
        for peripheral in profile_cores[0].get("peripherals", [])
        if isinstance(peripheral, dict) and "name" in peripheral
    }
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
        if not candidates:
            candidates = [template_registers]
        register_records.append(
            (peripheral, template_registers, candidates)
        )

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

    peripheral_registers = {}
    for peripheral, template, candidates in register_records:
        selected = min(
            (
                candidate
                for candidate in candidates
                if candidate["version"] == selected_versions[str(template["kind"])]
            ),
            key=lambda candidate: (
                candidate.get("block") != template.get("block"),
                str(candidate.get("block", "")),
            ),
        )
        peripheral_registers[peripheral] = {
            key: selected[key] for key in ("kind", "version", "block")
        }

    projected_pins = set()
    peripheral_pins: dict[str, set[tuple[str, str]]] = {}
    native_names = sorted(
        (str(instance["name"]) for instance in variant.get("instances", [])),
        key=lambda name: (-len(name), name),
    )
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
        name = f"{projected_dma}_CH{index + 1}"
        channel_names[(native_dma, index)] = name
        projected_channel = {"name": name, "dma": projected_dma, "channel": index}
        if channel.get("dmamux") is not None:
            dmamux = _map_peripheral(str(channel["dmamux"]))
            if dmamux is None:
                raise ValueError(f"无法映射 DMAMUX：{channel['dmamux']}")
            projected_channel["dmamux"] = dmamux
        elif dma.get("kind") == "dmamux" and len(variant_dmamuxes) == 1:
            projected_channel["dmamux"] = next(iter(variant_dmamuxes))
        dma_channels.append(projected_channel)

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
            peripheral_dma.setdefault(peripheral, set()).add(
                ("channel", signal, channel)
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
                    {"signal": entry[1], "channel": entry[2]}
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
        "peripheral_registers": peripheral_registers,
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
            )
            projected, unsupported = project_chip(profile, model, variant, facts)
            entry["patch"] = merge_patch(profile, projected)
            entry["unsupported_peripherals"] = unsupported
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
    args = parser.parse_args()
    models = json.loads(args.models.read_text(encoding="utf-8"))
    variants = json.loads(args.variants.read_text(encoding="utf-8"))
    compatibility = json.loads(args.register_compat.read_text(encoding="utf-8"))
    candidates = _load_candidates(args.official_chips)
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
        }
        manifest = build_projection_manifest(
            report,
            {
                "models": _index_rows(models.get("devices"), "id"),
                "variants": _index_rows(variants.get("variants"), "id"),
                "official_profiles": _index_rows(candidates, "name"),
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
