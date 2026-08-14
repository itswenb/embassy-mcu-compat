#!/usr/bin/env python3
"""索引 GD32 Firmware 中的 RISC-V ISA 与链接内存事实。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import analyze_gigadevice_coverage as coverage
import gigadevice_sources as common
import index_gigadevice_firmware_headers as firmware_headers


TARGETS = {
    "RV32IMAC": "riscv32imac-unknown-none-elf",
    "RV32IMAFC": "riscv32imafc-unknown-none-elf",
}
COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
MEMORY_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*\([^)]*\)\s*:\s*ORIGIN\s*=\s*"
    r"(0x[0-9A-Fa-f]+)\s*,\s*LENGTH\s*=\s*(\d+)\s*([KM]?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def parse_iar_core(text: str) -> tuple[str, str]:
    root = ET.fromstring(text)
    values = [
        option.findtext("state", "").strip().upper()
        for option in root.iter("option")
        if option.findtext("name", "").strip() == "GCoreDevice"
    ]
    values = sorted(set(filter(None, values)))
    if len(values) != 1 or values[0] not in TARGETS:
        raise ValueError(f"IAR RISC-V 核心选择无效：{values}")
    isa = values[0]
    return isa, TARGETS[isa]


def parse_linker_memory(text: str) -> list[dict[str, object]]:
    scales = {"": 1, "K": 1024, "M": 1024 * 1024}
    rows = [
        {
            "name": name.lower(),
            "kind": "flash" if "flash" in name.lower() else "ram",
            "address": int(address, 16),
            "size": int(size) * scales[unit.upper()],
        }
        for name, address, size, unit in MEMORY_RE.findall(COMMENT_RE.sub("", text))
    ]
    if not rows or not any(row["kind"] == "flash" for row in rows) or not any(
        row["kind"] == "ram" for row in rows
    ):
        raise ValueError("RISC-V 链接脚本缺少 Flash 或 RAM 区域")
    return sorted(rows, key=lambda row: (int(row["address"]), str(row["name"])))


def _core_source(root: Path) -> tuple[str, str, dict[str, object]]:
    rows = []
    for path in sorted(root.rglob("*.ewp")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "GCoreDevice" not in text:
            continue
        isa, target = parse_iar_core(text)
        rows.append((isa, target, path))
    choices = {(isa, target) for isa, target, _ in rows}
    if len(choices) != 1:
        raise ValueError(f"Firmware RISC-V 核心选择不唯一：{sorted(choices)}")
    isa, target = choices.pop()
    path = rows[0][2]
    return isa, target, {
        "path": path.relative_to(root).as_posix(),
        "sha256": common._sha256(path),
        "project_files": len(rows),
    }


def build_report(lock: dict[str, object], root: Path) -> dict[str, object]:
    raw_items = lock.get("firmware")
    if not isinstance(raw_items, list):
        raise ValueError("Firmware 锁文件缺少 firmware 列表")
    libraries = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("Firmware 锁文件包含非法条目")
        filename = str(raw_item["filename"])
        library_root = root / filename.removesuffix(".7z")
        linkers = [
            path
            for path in sorted(library_root.rglob("*.lds"))
            if "Firmware/RISCV/env_Eclipse" in path.as_posix()
        ]
        if not linkers:
            continue
        marker = firmware_headers._source_marker(library_root, raw_item)
        isa, target, core_source = _core_source(library_root)
        profiles = [
            {
                "pattern": path.stem.upper(),
                "memory": parse_linker_memory(path.read_text(encoding="utf-8")),
                "source": {
                    "path": path.relative_to(library_root).as_posix(),
                    "sha256": common._sha256(path),
                },
            }
            for path in linkers
        ]
        libraries.append(
            {
                "series": coverage._series_from_firmware_filename(filename),
                "version": raw_item["version"],
                "document_id": raw_item["document_id"],
                "archive_sha256": raw_item["sha256"],
                "tree_sha256": marker["tree_sha256"],
                "isa": isa,
                "rust_target": target,
                "core_source": core_source,
                "linker_profiles": profiles,
            }
        )
    libraries.sort(key=lambda row: str(row["series"]))
    return {
        "schema_version": 1,
        "summary": {
            "libraries": len(libraries),
            "linker_profiles": sum(len(row["linker_profiles"]) for row in libraries),
        },
        "libraries": libraries,
    }


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock", type=Path, default=root / "sources/gigadevice/firmware.lock.json"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=root / ".cache/research/gigadevice/firmware-sources-v1",
    )
    parser.add_argument(
        "--output", type=Path, default=root / "reports/gigadevice-riscv.json"
    )
    parser.add_argument("--minimum-libraries", type=int, default=2)
    parser.add_argument("--minimum-linker-profiles", type=int, default=6)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_report(json.loads(args.lock.read_text(encoding="utf-8")), args.root)
    summary = report["summary"]
    if (
        int(summary["libraries"]) < args.minimum_libraries
        or int(summary["linker_profiles"]) < args.minimum_linker_profiles
    ):
        raise ValueError("RISC-V Firmware 来源覆盖低于门限")
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"RISC-V 来源报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
