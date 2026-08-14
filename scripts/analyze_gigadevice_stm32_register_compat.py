#!/usr/bin/env python3
"""按寄存器与位域位置审计 GD32 布局和 STM32 register IR 的结构兼容性。"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

import gigadevice_sources as common


KIND_ALIASES = {
    "ADC": {"adc"},
    "AFIO": {"afio"},
    "BKP": {"bkp"},
    "CAN": {"can", "fdcan"},
    "CEC": {"cec"},
    "CMP": {"comp"},
    "CRC": {"crc"},
    "CTC": {"crs"},
    "DAC": {"dac"},
    "DBG": {"dbgmcu"},
    "DCI": {"dcmi"},
    "DMA": {"dma", "bdma", "gpdma"},
    "DMAMUX": {"dmamux"},
    "DSI": {"dsi"},
    "ENET": {"eth"},
    "EXMC": {"fmc", "fsmc"},
    "EXTI": {"exti"},
    "FMC": {"flash"},
    "FWDGT": {"iwdg"},
    "GPIO": {"gpio"},
    "GPION": {"gpio"},
    "HAU": {"hash"},
    "HRTIMER": {"hrtim"},
    "HWSEM": {"hsem"},
    "I2C": {"i2c"},
    "I2S": {"spi"},
    "ICACHE": {"icache"},
    "LPTIMER": {"lptim"},
    "LPUART": {"usart"},
    "MDMA": {"mdma"},
    "NVMC": {"flash"},
    "OSPI": {"octospi", "xspi"},
    "OSPIM": {"octospim"},
    "PKCAU": {"pka"},
    "PMU": {"pwr"},
    "QSPI": {"quadspi", "octospi", "xspi"},
    "RAMECCMU": {"ramecc"},
    "RCU": {"rcc"},
    "RSPDIF": {"spdifrx"},
    "RTC": {"rtc"},
    "SAI": {"sai"},
    "SDIO": {"sdmmc"},
    "SHRTIMER": {"hrtim"},
    "SLCD": {"lcd"},
    "SPI": {"spi"},
    "SQPI": {"quadspi", "octospi", "xspi"},
    "SYSCFG": {"syscfg"},
    "TIMER": {"timer"},
    "TLI": {"ltdc"},
    "TRNG": {"rng"},
    "TSI": {"tsc"},
    "UART": {"usart"},
    "USART": {"usart"},
    "VREF": {"vrefbuf"},
    "WWDGT": {"wwdg"},
}


def expand_gd_register(register: dict[str, object]) -> list[dict[str, object]]:
    parameters = register.get("array_parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError(f"寄存器数组参数无效：{register.get('name')}")
    offset = int(register["offset"])
    if not parameters:
        return [{"name": str(register["name"]), "offset": offset}]
    dimensions = []
    for bounds in parameters.values():
        if not isinstance(bounds, dict):
            raise ValueError(f"寄存器数组范围无效：{register.get('name')}")
        start = int(bounds["start"])
        stride = int(bounds["stride"])
        indices = (
            list(map(int, bounds["indices"]))
            if "indices" in bounds
            else list(range(start, int(bounds["end"]) + 1))
            if "end" in bounds
            else []
        )
        if start < 0 or stride <= 0 or not indices or indices != sorted(set(indices)):
            raise ValueError(f"寄存器数组未闭合：{register.get('name')}")
        dimensions.append((start, stride, indices))
    return [
        {
            "name": f"{register['name']}_{'_'.join(map(str, indices))}",
            "offset": offset
            + sum(
                (index - start) * stride
                for index, (start, stride, _) in zip(indices, dimensions, strict=True)
            ),
        }
        for indices in itertools.product(*(dimension[2] for dimension in dimensions))
    ]


def _gd_register_offsets(register: dict[str, object]) -> list[int]:
    return [int(row["offset"]) for row in expand_gd_register(register)]


def _field_ranges(
    fields: list[dict[str, object]],
) -> tuple[tuple[tuple[int, int], ...], ...]:
    ranges = []
    for field in fields:
        offset = field.get("bit_offset")
        size = int(field["bit_size"])
        if isinstance(offset, int):
            field_ranges = [((offset, offset + size - 1),)]
        elif isinstance(offset, list):
            field_ranges = [
                tuple((int(item["start"]), int(item["end"])) for item in offset)
            ]
        else:
            raise ValueError(f"位域偏移无效：{field.get('name')}")
        array = field.get("array")
        if isinstance(array, dict):
            if "offsets" in array:
                shifts = list(map(int, array["offsets"]))
            else:
                shifts = [
                    index * int(array["stride"])
                    for index in range(int(array["len"]))
                ]
            field_ranges = [
                tuple((start + shift, end + shift) for start, end in field_ranges[0])
                for shift in shifts
            ]
        ranges.extend(field_ranges)
    return tuple(sorted(ranges))


def gd_layout_signature(
    layout: dict[str, object],
) -> tuple[tuple[int, int, tuple[tuple[tuple[int, int], ...], ...]], ...]:
    registers = layout.get("registers")
    fields = layout.get("fields")
    if not isinstance(registers, list) or not isinstance(fields, list):
        raise ValueError(f"GD32 布局缺少寄存器或位域：{layout.get('id')}")
    fields_by_register: dict[str, list[dict[str, object]]] = defaultdict(list)
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError(f"GD32 位域记录无效：{layout.get('id')}")
        fields_by_register[str(field["register"])].append(field)
    signature = []
    for register in registers:
        if not isinstance(register, dict):
            raise ValueError(f"GD32 寄存器记录无效：{layout.get('id')}")
        name = str(register["name"])
        field_ranges = _field_ranges(fields_by_register.get(name, []))
        signature.extend(
            (offset, int(register.get("width", 32)), field_ranges)
            for offset in _gd_register_offsets(register)
        )
    return tuple(sorted(signature))


def _st_item_offsets(item: dict[str, object]) -> list[int]:
    offset = int(item["byte_offset"])
    array = item.get("array")
    if not isinstance(array, dict):
        return [offset]
    if "offsets" in array:
        return [offset + int(value) for value in array["offsets"]]
    return [offset + index * int(array["stride"]) for index in range(int(array["len"]))]


def st_block_signature(
    registers: dict[str, object], block: str
) -> tuple[tuple[int, int, tuple[tuple[tuple[int, int], ...], ...]], ...]:
    signature = []

    def resolve(
        prefix: str, name: str, collection: str, stack: tuple[str, ...] = ()
    ) -> dict[str, object]:
        key = f"{prefix}/{name}"
        if key in stack:
            raise ValueError(f"STM32 register IR 继承循环：{' -> '.join((*stack, key))}")
        value = registers.get(key)
        if not isinstance(value, dict) or not isinstance(value.get(collection), list):
            raise ValueError(f"STM32 register IR 缺少 {key}")
        parent_name = value.get("extends")
        resolved = (
            resolve(prefix, str(parent_name), collection, (*stack, key))
            if parent_name is not None
            else {}
        )
        inherited = {
            str(item["name"]): item
            for item in resolved.get(collection, [])
            if isinstance(item, dict) and "name" in item
        }
        inherited.update(
            {
                str(item["name"]): item
                for item in value[collection]
                if isinstance(item, dict) and "name" in item
            }
        )
        return {**resolved, **value, collection: list(inherited.values())}

    def visit(name: str, bases: list[int], stack: tuple[str, ...]) -> None:
        if name in stack:
            raise ValueError(f"STM32 register IR 子块循环：{' -> '.join((*stack, name))}")
        value = resolve("block", name, "items")
        for item in value["items"]:
            if not isinstance(item, dict):
                raise ValueError(f"STM32 block/{name} 包含无效条目")
            offsets = [
                base + offset
                for base in bases
                for offset in _st_item_offsets(item)
            ]
            child = item.get("block")
            if child is not None:
                visit(str(child), offsets, (*stack, name))
                continue
            fieldset_name = item.get("fieldset")
            if fieldset_name is None:
                fields = []
                bit_size = int(item.get("bit_size", 32))
            else:
                fieldset = resolve("fieldset", str(fieldset_name), "fields")
                fields = fieldset["fields"]
                bit_size = int(fieldset.get("bit_size", item.get("bit_size", 32)))
            ranges = _field_ranges(fields)
            signature.extend((offset, bit_size, ranges) for offset in offsets)

    visit(block, [0], ())
    return tuple(sorted(signature))


def st_block_is_subset(
    layout: dict[str, object], registers: dict[str, object], block: str
) -> bool:
    gd_rows = gd_layout_signature(layout)
    try:
        st_rows = st_block_signature(registers, block)
    except ValueError:
        return False
    for st_offset, st_width, st_fields in st_rows:
        if not any(
            gd_offset == st_offset
            and gd_width == st_width
            and set(st_fields) <= set(gd_fields)
            for gd_offset, gd_width, gd_fields in gd_rows
        ):
            return False
    return True


def _stm32_index(
    registers_dir: Path,
) -> tuple[
    dict[tuple[object, ...], list[dict[str, str]]],
    dict[str, list[tuple[dict[str, str], dict[str, object]]]],
]:
    index: dict[tuple[object, ...], list[dict[str, str]]] = defaultdict(list)
    by_kind: dict[str, list[tuple[dict[str, str], dict[str, object]]]] = defaultdict(list)
    for path in sorted(registers_dir.glob("*.json")):
        kind, separator, version = path.stem.partition("_")
        if not separator:
            raise ValueError(f"无法拆分 STM32 register 文件名：{path.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in sorted(data):
            if not key.startswith("block/"):
                continue
            block = key.removeprefix("block/")
            try:
                signature = st_block_signature(data, block)
            except ValueError:
                continue
            metadata = {
                "kind": kind,
                "version": version,
                "block": block,
                "path": path.name,
                "registers": len(signature),
                "fields": sum(len(row[2]) for row in signature),
            }
            index[signature].append(metadata)
            by_kind[kind].append((metadata, data))
    return index, by_kind


def _semantic_kinds(block: str) -> set[str]:
    normalized = block.upper()
    if normalized.endswith("_BASE") or normalized.startswith("DMA_CH"):
        return set()
    normalized = normalized.rstrip("0123456789")
    return KIND_ALIASES.get(normalized, {normalized.lower()})


def analyze(variants: dict[str, object], registers_dir: Path) -> dict[str, object]:
    raw_variants = variants.get("variants")
    if not isinstance(raw_variants, list):
        raise ValueError("变体报告缺少 variants")
    stm32, stm32_by_kind = _stm32_index(registers_dir)
    layouts: dict[str, dict[str, object]] = {}
    for variant in raw_variants:
        if not isinstance(variant, dict):
            raise ValueError("变体记录无效")
        for layout in variant.get("layouts", []):
            if not isinstance(layout, dict):
                raise ValueError("布局记录无效")
            layout_id = str(layout["id"])
            previous = layouts.setdefault(layout_id, layout)
            if (
                previous["block"] != layout["block"]
                or gd_layout_signature(previous) != gd_layout_signature(layout)
            ):
                raise ValueError(f"同一布局 ID 内容不一致：{layout_id}")
    rows = []
    for layout_id, layout in sorted(layouts.items()):
        kinds = _semantic_kinds(str(layout["block"]))
        exact_candidates = [
            candidate
            for candidate in stm32.get(gd_layout_signature(layout), [])
            if candidate["kind"] in kinds
        ]
        subset_candidates = []
        seen_candidates = set()
        for kind in sorted(kinds):
            for candidate, data in stm32_by_kind.get(kind, []):
                key = (candidate["path"], candidate["block"])
                if key in seen_candidates:
                    continue
                if st_block_is_subset(layout, data, candidate["block"]):
                    seen_candidates.add(key)
                    subset_candidates.append(candidate)
        status = (
            "exact"
            if exact_candidates
            else "subset"
            if subset_candidates
            else "missing"
        )
        rows.append(
            {
                "id": layout_id,
                "block": layout["block"],
                "registers": len(layout["registers"]),
                "fields": len(layout["fields"]),
                "status": status,
                "exact_candidates": exact_candidates,
                "subset_candidates": subset_candidates,
            }
        )
    return {
        "schema_version": 1,
        "summary": {
            "layouts": len(rows),
            "exact": sum(row["status"] == "exact" for row in rows),
            "subset": sum(row["status"] == "subset" for row in rows),
            "missing": sum(row["status"] == "missing" for row in rows),
            "unique": sum(
                len(row["subset_candidates"]) == 1 for row in rows
            ),
            "ambiguous": sum(
                len(row["subset_candidates"]) > 1 for row in rows
            ),
        },
        "layouts": rows,
    }


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variants",
        type=Path,
        default=root / "reports/gigadevice-merged-firmware-variants.json",
    )
    parser.add_argument(
        "--stm32-registers",
        type=Path,
        default=root / ".cache/research/repos/stm32-data-generated/data/registers",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "reports/gigadevice-stm32-register-compat.json",
    )
    args = parser.parse_args()
    report = analyze(
        json.loads(args.variants.read_text(encoding="utf-8")), args.stm32_registers
    )
    report["provenance"] = {
        "variants": {"path": args.variants.name, "sha256": common._sha256(args.variants)},
        "stm32_registers": {
            "path": args.stm32_registers.relative_to(root).as_posix(),
            "tree_sha256": common.tree_sha256(args.stm32_registers),
        },
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in report["summary"].items()))
    print(f"STM32 寄存器结构兼容报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
