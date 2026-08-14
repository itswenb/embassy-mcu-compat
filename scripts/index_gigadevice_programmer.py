#!/usr/bin/env python3
"""归一 Programmer 中的完整料号与 Flash 页几何。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import gigadevice_sources as common


A7_PART_RE = re.compile(r"^(GD32A7\d{2}[A-Z]{2})(?:T3T[AB]|J3TA)$")
H77_PART_RE = re.compile(r"^(GD32H77[A-Z0-9]{3})(?:K7|T7|J7)$")


def devices_from_tokens(tokens: list[str]) -> list[dict[str, object]]:
    grouped: dict[str, set[str]] = {}
    for raw in tokens:
        part = raw.upper()
        match = A7_PART_RE.fullmatch(part) or H77_PART_RE.fullmatch(part)
        if match is not None:
            grouped.setdefault(match.group(1), set()).add(part)
    return [
        {"id": device, "part_numbers": sorted(parts)}
        for device, parts in sorted(grouped.items())
    ]


def parse_flash_xml(text: str) -> list[dict[str, object]]:
    root = ET.fromstring(text)
    profiles = []
    for flash in root.findall("Flash"):
        pages = []
        for page in flash.findall("./pageGroup/Page"):
            start_index = int(page.attrib["startIndex"], 0)
            end_index = int(page.attrib["endIndex"], 0)
            start = int(page.attrib["startAddress"], 0)
            end = int(page.attrib["endAddress"], 0)
            page_size = int(page.attrib["pageSize"], 0)
            count = end_index - start_index + 1
            if count <= 0 or page_size <= 0:
                raise ValueError(f"Programmer Flash 页几何无效：{page.attrib}")
            calculated_size = count * page_size
            pages.append(
                {
                    "start_index": start_index,
                    "end_index": end_index,
                    "count": count,
                    "address": start,
                    "end_address": end,
                    "size": calculated_size,
                    "page_size": page_size,
                    "geometry_status": (
                        "consistent"
                        if end - start + 1 == calculated_size
                        else "source-inconsistent"
                    ),
                    "bank": page.attrib.get("bank", ""),
                    "type": page.attrib.get("type", ""),
                }
            )
        declared_pages = int(flash.find("pageGroup").attrib["pageNumber"], 0)
        represented_pages = sum(int(page["count"]) for page in pages)
        indexed_pages = max(int(page["end_index"]) for page in pages) + 1
        profiles.append(
            {
                "series": root.attrib["series"].upper(),
                "pattern": flash.attrib["McuPartNo"].upper(),
                "base_address": int(flash.attrib["baseAddress"], 0),
                "rram_size": int(flash.attrib.get("RRAMSize", "0"), 0) * 1024,
                "flash_size": int(flash.attrib.get("Size", "0"), 0) * 1024,
                "declared_page_number": declared_pages,
                "page_number_status": (
                    "consistent"
                    if declared_pages in {represented_pages, indexed_pages}
                    else "source-inconsistent"
                ),
                "pages": pages,
            }
        )
    if not profiles:
        raise ValueError("Programmer Flash XML 不含 Flash profile")
    return profiles


def build_report(scan: dict[str, object], extracted_root: Path) -> dict[str, object]:
    summary = scan.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("Programmer 扫描报告缺少 summary")
    tokens = [
        str(token)
        for key in ("a7_tokens", "h77_tokens")
        for token in summary.get(key, [])
    ]
    profiles = []
    sources = []
    for path in sorted(extracted_root.rglob("GD32MCUFlashXML/*.xml")):
        rows = parse_flash_xml(path.read_text(encoding="utf-8-sig"))
        relative = path.relative_to(extracted_root).as_posix()
        source = {"path": relative, "sha256": common._sha256(path)}
        profiles.extend({**row, "source": source} for row in rows)
        sources.append(source)
    devices = devices_from_tokens(tokens)
    return {
        "schema_version": 1,
        "summary": {
            "devices": len(devices),
            "a7_devices": sum(str(device["id"]).startswith("GD32A7") for device in devices),
            "h77_devices": sum(str(device["id"]).startswith("GD32H77") for device in devices),
            "part_numbers": sum(len(device["part_numbers"]) for device in devices),
            "flash_profiles": len(profiles),
            "flash_xml_files": len(sources),
        },
        "devices": devices,
        "flash_profiles": sorted(
            profiles, key=lambda row: (str(row["series"]), str(row["pattern"]))
        ),
        "flash_xml_sources": sources,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan",
        type=Path,
        default=repo_root / "reports/gigadevice-programmer.json",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/programmer-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-programmer-data.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    scan = json.loads(args.scan.read_text(encoding="utf-8"))
    archive_sha256 = str(scan["source"]["archive_sha256"])
    extracted_root = args.cache_dir / archive_sha256[:12]
    report = build_report(scan, extracted_root)
    report["provenance"] = {
        "scan": {"path": args.scan.name, "sha256": common._sha256(args.scan)},
        "archive_sha256": archive_sha256,
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in report["summary"].items()))
    print(f"Programmer 结构化数据：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
