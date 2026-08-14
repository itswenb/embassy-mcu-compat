#!/usr/bin/env python3
"""把真实 GD32 Firmware 变体生成 stm32-metapac-gen staging 数据。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import gigadevice_sources as common
from analyze_gigadevice_stm32_register_compat import expand_gd_register


CORE_NAMES = {
    "Cortex-M3": "cm3",
    "Cortex-M4": "cm4",
    "Cortex-M7": "cm7",
    "Cortex-M23": "cm23",
    "Cortex-M33": "cm33",
    "RV32IMAC": "riscv32imac",
    "RV32IMAFC": "riscv32imafc",
}


def _register_identity(layout: dict[str, object]) -> tuple[str, str, str]:
    block = str(layout["block"])
    if re.fullmatch(r"[A-Za-z_]\w*", block) is None:
        raise ValueError(f"布局 block 不是合法标识符：{block}")
    digest = str(layout["id"]).rsplit("-", 1)[-1]
    if re.fullmatch(r"[0-9a-f]{16}", digest) is None:
        raise ValueError(f"布局 ID 缺少 16 位摘要：{layout['id']}")
    kind = "gd" + re.sub(r"[^a-z0-9]", "", block.lower()) + digest[:8]
    return kind, "v1", block


def register_ir(layout: dict[str, object]) -> dict[str, object]:
    registers = layout.get("registers")
    fields = layout.get("fields")
    if not isinstance(registers, list) or not isinstance(fields, list):
        raise ValueError(f"布局缺少 registers/fields：{layout.get('id')}")
    _, _, block_name = _register_identity(layout)
    fields_by_register: dict[str, list[dict[str, object]]] = {}
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError(f"布局位域记录无效：{layout.get('id')}")
        fields_by_register.setdefault(str(field["register"]), []).append(field)
    items = []
    fieldsets = {}
    for register in sorted(registers, key=lambda row: (int(row["offset"]), str(row["name"]))):
        if not isinstance(register, dict):
            raise ValueError(f"布局寄存器记录无效：{layout.get('id')}")
        source_name = str(register["name"])
        width = int(register.get("width", 32))
        selected_fields = sorted(
            fields_by_register.get(source_name, []),
            key=lambda row: (int(row["bit_offset"]), str(row["name"])),
        )
        if selected_fields:
            fieldset = {
                "fields": [
                    {
                        "name": str(field["name"]),
                        "bit_offset": int(field["bit_offset"]),
                        "bit_size": int(field["bit_size"]),
                    }
                    for field in selected_fields
                ]
            }
            if width != 32:
                fieldset["bit_size"] = width
            fieldsets[f"fieldset/{source_name}"] = fieldset
        for expanded in expand_gd_register(register):
            item = {
                "name": expanded["name"],
                "byte_offset": expanded["offset"],
            }
            if width != 32:
                item["bit_size"] = width
            if selected_fields:
                item["fieldset"] = source_name
            items.append(item)
    unknown = set(fields_by_register) - {str(register["name"]) for register in registers}
    if unknown:
        raise ValueError(f"布局位域引用未知寄存器：{layout['id']}:{sorted(unknown)}")
    return {f"block/{block_name}": {"items": items}, **fieldsets}


def _memory_configurations(memory: dict[str, object]) -> dict[str, list[list[dict[str, object]]]]:
    raw_devices = memory.get("devices")
    raw_profiles = memory.get("profiles")
    if not isinstance(raw_devices, list) or not isinstance(raw_profiles, list):
        raise ValueError("内存报告格式无效")
    profiles = {str(profile["device"]): profile for profile in raw_profiles}
    result = {}
    for device in raw_devices:
        device_id = str(device["id"])
        configurations = {}
        for profile_id in map(str, device.get("profiles", [])):
            if profile_id not in profiles:
                raise ValueError(f"内存型号引用未知 profile：{device_id}:{profile_id}")
            regions = []
            for region in profiles[profile_id].get("memory", []):
                kind = str(region["kind"])
                address = int(region["address"])
                size = int(region["size"])
                if kind not in {"flash", "ram", "eeprom"}:
                    raise ValueError(f"内存区域类型无效：{device_id}:{kind}")
                if address < 0 or size <= 0 or address + size > 1 << 32:
                    raise ValueError(f"内存区域范围无效：{device_id}:{region['name']}")
                regions.append(
                    {
                        "name": str(region["name"]),
                        "kind": kind,
                        "address": address,
                        "size": size,
                    }
                )
            configurations.setdefault(
                json.dumps(regions, ensure_ascii=False, sort_keys=True), regions
            )
        result[device_id] = (
            [next(iter(configurations.values()))]
            if device.get("memory_status") == "normalized" and len(configurations) == 1
            else []
        )
    return result


def _chip(
    model: dict[str, object],
    variant: dict[str, object],
    layouts: dict[str, tuple[str, str, str]],
    memory: list[list[dict[str, object]]],
) -> dict[str, object]:
    core = CORE_NAMES.get(str(model.get("core")), "unknown")
    peripherals = []
    for instance in sorted(
        variant["instances"], key=lambda row: (str(row["name"]), int(row["address"]))
    ):
        layout_id = str(instance["layout"])
        if layout_id not in layouts:
            raise ValueError(f"外设实例引用未知布局：{variant['id']}:{layout_id}")
        kind, version, block = layouts[layout_id]
        peripherals.append(
            {
                "name": str(instance["name"]),
                "address": int(instance["address"]),
                "registers": {"kind": kind, "version": version, "block": block},
            }
        )
    return {
        "name": str(model["id"]),
        "family": "GD32",
        "line": str(variant["series"]),
        "die": "GD32",
        "device_id": 0,
        "packages": [],
        "memory": memory,
        "docs": [],
        "cores": [
            {
                "name": core,
                "peripherals": peripherals,
                "interrupts": [
                    {"name": str(row["name"]), "number": int(row["value"])}
                    for row in sorted(
                        variant["interrupts"],
                        key=lambda row: (int(row["value"]), str(row["name"])),
                    )
                ],
                "dma_channels": [],
                "pins": [],
            }
        ],
    }


def build_staging(
    models: dict[str, object], variants: dict[str, object], memory: dict[str, object]
) -> dict[str, dict[str, object]]:
    raw_models = models.get("devices")
    raw_variants = variants.get("variants")
    if not isinstance(raw_models, list) or not isinstance(raw_variants, list):
        raise ValueError("型号或变体报告格式无效")
    models_by_id = {str(model["id"]): model for model in raw_models}
    memory_by_id = _memory_configurations(memory)
    if set(models_by_id) - set(memory_by_id):
        raise ValueError("内存报告未覆盖全部规范化型号")
    chips = {}
    register_files = {}
    layout_identities = {}
    for variant in sorted(raw_variants, key=lambda row: str(row["id"])):
        local_layouts = {}
        for layout in variant["layouts"]:
            identity = _register_identity(layout)
            layout_id = str(layout["id"])
            ir = register_ir(layout)
            key = f"{identity[0]}_{identity[1]}"
            if key in register_files and register_files[key] != ir:
                raise ValueError(f"register IR 名称冲突：{key}")
            if layout_id in layout_identities and layout_identities[layout_id] != identity:
                raise ValueError(f"布局 ID 身份冲突：{layout_id}")
            register_files[key] = ir
            layout_identities[layout_id] = identity
            local_layouts[layout_id] = identity
        for device in sorted(map(str, variant["devices"])):
            model = models_by_id.get(device)
            if model is None:
                raise ValueError(f"变体引用未知型号：{device}")
            if device in chips:
                raise ValueError(f"型号同时属于多个变体：{device}")
            chips[device] = _chip(model, variant, local_layouts, memory_by_id[device])
    return {"chips": chips, "registers": register_files}


def _write_staging(output: Path, staging: dict[str, dict[str, object]], manifest: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".m32-data-", dir=output.parent) as directory:
        temporary = Path(directory)
        chips_dir = temporary / "chips"
        registers_dir = temporary / "registers"
        chips_dir.mkdir()
        registers_dir.mkdir()
        for name, chip in sorted(staging["chips"].items()):
            common._write_text_atomic(
                chips_dir / f"{name}.json",
                json.dumps(chip, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        for name, registers in sorted(staging["registers"].items()):
            common._write_text_atomic(
                registers_dir / f"{name}.json",
                json.dumps(registers, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        common._write_text_atomic(
            temporary / "generation.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        if output.exists():
            marker = output / "generation.json"
            if not marker.is_file():
                raise ValueError(f"拒绝覆盖非本脚本生成目录：{output}")
            shutil.rmtree(output)
        temporary.rename(output)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=root / "reports/gigadevice-models.json")
    parser.add_argument(
        "--variants",
        type=Path,
        default=root / "reports/gigadevice-merged-firmware-variants.json",
    )
    parser.add_argument(
        "--memory",
        type=Path,
        default=root / ".cache/normalized/gigadevice-memory.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / ".cache/generated/gigadevice-stm32-data-v1",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=root / "reports/gigadevice-stm32-data.json",
    )
    parser.add_argument("--minimum-devices", type=int, default=632)
    args = parser.parse_args()
    staging = build_staging(
        json.loads(args.models.read_text(encoding="utf-8")),
        json.loads(args.variants.read_text(encoding="utf-8")),
        json.loads(args.memory.read_text(encoding="utf-8")),
    )
    manifest = {
        "schema_version": 1,
        "models_sha256": common._sha256(args.models),
        "variants_sha256": common._sha256(args.variants),
        "memory_sha256": common._sha256(args.memory),
        "chips": len(staging["chips"]),
        "register_files": len(staging["registers"]),
        "chips_with_memory": sum(bool(chip["memory"]) for chip in staging["chips"].values()),
        "memory_regions": sum(
            len(configuration)
            for chip in staging["chips"].values()
            for configuration in chip["memory"]
        ),
    }
    if manifest["chips"] < args.minimum_devices:
        raise ValueError("生成的 GD32 Chip 数低于门限")
    _write_staging(args.output, staging, manifest)
    report = {
        **manifest,
        "output_tree_sha256": common.tree_sha256(args.output),
        "output": args.output.relative_to(root).as_posix(),
    }
    common._write_text_atomic(
        args.report, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(f"chips={manifest['chips']} register_files={manifest['register_files']}")
    print(f"GD32 stm32-data staging：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
