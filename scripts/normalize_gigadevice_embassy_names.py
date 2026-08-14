#!/usr/bin/env python3
"""审计 GD32 原生外设实例名到 Embassy/ST 语义名称的转换。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import gigadevice_sources as common


INDEXED_PREFIXES = {
    "ADC": "ADC",
    "DMA": "DMA",
    "I2C": "I2C",
    "LPTIMER": "LPTIM",
    "LPUART": "LPUART",
    "SAI": "SAI",
    "SPI": "SPI",
    "TIMER": "TIM",
    "UART": "UART",
    "USART": "USART",
}

SEMANTIC_NAMES = {
    "CTC": "CRS",
    "DBG": "DBGMCU",
    "DCI": "DCMI",
    "DMAMUX": "DMAMUX1",
    "ENET": "ETH",
    "EXMC": "FMC",
    "FMC": "FLASH",
    "FWDGT": "IWDG",
    "PMU": "PWR",
    "QSPI": "QUADSPI",
    "RCU": "RCC",
    "TLI": "LTDC",
    "TRNG": "RNG",
    "WWDGT": "WWDG",
}

IDENTITY_NAMES = {
    "AFIO",
    "BKP",
    "CEC",
    "CMP",
    "CRC",
    "EXTI",
    "FLASH",
    "RCC",
    "RTC",
    "SYSCFG",
}


def embassy_instance_name(native: str) -> tuple[str, str] | None:
    if native in SEMANTIC_NAMES:
        return SEMANTIC_NAMES[native], "semantic"
    for prefix, embassy_prefix in INDEXED_PREFIXES.items():
        match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", native)
        if match is not None:
            return f"{embassy_prefix}{int(match.group(1)) + 1}", "indexed"
    if native in IDENTITY_NAMES or re.fullmatch(r"GPIO[A-Z]", native):
        return native, "identity"
    return None


def build_report(variants: dict[str, object]) -> dict[str, object]:
    raw_variants = variants.get("variants")
    if not isinstance(raw_variants, list):
        raise ValueError("Firmware 变体报告格式无效")
    mappings: dict[tuple[str, str, str], dict[str, object]] = {}
    unsupported: dict[str, dict[str, object]] = {}
    occurrences = 0
    mapped_occurrences = 0
    devices = set()
    for variant in raw_variants:
        variant_id = str(variant["id"])
        variant_devices = set(map(str, variant["devices"]))
        devices.update(variant_devices)
        targets = {}
        for instance in variant["instances"]:
            occurrences += 1
            native = str(instance["name"])
            address = int(instance["address"])
            converted = embassy_instance_name(native)
            if converted is None:
                row = unsupported.setdefault(
                    native, {"native": native, "variants": set(), "devices": set()}
                )
                row["variants"].add(variant_id)
                row["devices"].update(variant_devices)
                continue
            embassy, rule = converted
            mapped_occurrences += 1
            previous = targets.get(embassy)
            if previous is not None and previous != (native, address):
                raise ValueError(
                    f"Embassy 外设名称冲突：{variant_id}:{embassy}:"
                    f"{previous[0]}@{previous[1]:#x}/{native}@{address:#x}"
                )
            targets[embassy] = (native, address)
            key = native, embassy, rule
            row = mappings.setdefault(
                key,
                {
                    "native": native,
                    "embassy": embassy,
                    "rule": rule,
                    "variants": set(),
                    "devices": set(),
                },
            )
            row["variants"].add(variant_id)
            row["devices"].update(variant_devices)

    def public(row: dict[str, object]) -> dict[str, object]:
        return {
            **row,
            "variants": sorted(row["variants"]),
            "devices": len(row["devices"]),
        }

    return {
        "schema_version": 1,
        "summary": {
            "variants": len(raw_variants),
            "devices": len(devices),
            "instance_occurrences": occurrences,
            "mapped_occurrences": mapped_occurrences,
            "unsupported_occurrences": occurrences - mapped_occurrences,
            "unique_mappings": len(mappings),
            "unique_unsupported": len(unsupported),
            "collisions": 0,
        },
        "index_policy": {
            "native": "保留 GD32 官方从 0 开始的实例编号",
            "embassy": "仅在兼容层转换为 ST/Embassy 从 1 开始的实例编号",
        },
        "mappings": [public(row) for _, row in sorted(mappings.items())],
        "unsupported": [public(row) for _, row in sorted(unsupported.items())],
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
        "--output",
        type=Path,
        default=root / "reports/gigadevice-embassy-names.json",
    )
    args = parser.parse_args()
    report = build_report(json.loads(args.variants.read_text(encoding="utf-8")))
    report["provenance"] = {
        "path": args.variants.name,
        "sha256": common._sha256(args.variants),
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(" ".join(f"{key}={value}" for key, value in report["summary"].items()))
    print(f"Embassy 名称审计报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
