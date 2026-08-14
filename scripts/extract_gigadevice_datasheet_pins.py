#!/usr/bin/env python3
"""盘点已锁定 GD32 数据手册中的管脚定义与复用功能表。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import gigadevice_sources as common


TABLE_NUMBER_PATTERN = r"(?P<number>\d+[-–. ]\d+)"
PIN_TITLE_RE = re.compile(
    rf"^Table\s+{TABLE_NUMBER_PATTERN}\.\s*"
    r"(?P<device>GD32[A-Z0-9x-]+)\s+"
    r"(?P<package>(?:LQFP|TQFP|QFN|BGA|CSP|WLCSP|LGA|SOP|SSOP|DFN|KQFP)[A-Z0-9-]*)\s+"
    r"pin\s+definitions(?:\s*\((?:continued|\d+)\))?\s*$",
    re.IGNORECASE,
)
GENERIC_PIN_TITLE_RE = re.compile(
    rf"^Table\s+{TABLE_NUMBER_PATTERN}\.\s*pin\s+definitions\s*$", re.IGNORECASE
)
AF_TITLE_RE = re.compile(
    rf"^Table\s+{TABLE_NUMBER_PATTERN}\.\s*.*(?:alternate\s+function|"
    r"GPIO\s+function\s+mapping).*$",
    re.IGNORECASE,
)
AF_COLUMN_RE = re.compile(r"\bAF(?:1[0-5]|[0-9])\b", re.IGNORECASE)
ANY_TABLE_RE = re.compile(r"^Table\s+(\d+[-–. ]\d+)\.", re.IGNORECASE)
SECTION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)*\.\s+\S")
PIN_MENTION_RE = re.compile(
    r"(?:pin.*(?:definition|assignment|description|mapping|connector)|"
    r"(?:definition|assignment|description|mapping|connector).*pin)",
    re.IGNORECASE,
)
GPIO_RE = r"P[A-Z][0-9]+(?:_C)?"
NAME_FIRST_ROW_RE = re.compile(
    rf"^\s*(?P<name>{GPIO_RE})\s+(?P<position>[A-Z]*\d+)\s+"
    r"(?P<pin_type>I/O|I|O|P|-)\b(?P<rest>.*)$"
)
POSITION_FIRST_ROW_RE = re.compile(
    rf"^\s*(?P<position>[A-Z]*\d+)\s+(?P<name>{GPIO_RE})\s+"
    r"(?P<pin_type>I/O|I|O|P|-)\b(?P<rest>.*)$"
)
FUNCTION_RE = re.compile(
    r"\b(?P<source>Default|Alternate|Remap|Additional):\s*"
    r"(?P<value>.*?)(?=\b(?:Default|Alternate|Remap|Additional):|$)",
    re.IGNORECASE,
)


def _normalized(line: str) -> str:
    return " ".join(line.split())


def _table_number(value: str) -> str:
    return re.sub(r"[-–. ]", "-", value)


def find_candidates(text: str) -> list[dict[str, object]]:
    """从确定性定宽文本中定位实际管脚表，排除目录与修订记录。"""
    candidates = []
    for page, raw_page in enumerate(text.split("\f"), 1):
        lines = raw_page.splitlines()
        for line_number, raw_line in enumerate(lines, 1):
            line = _normalized(raw_line)
            if not line or "..." in line:
                continue
            if match := PIN_TITLE_RE.fullmatch(line):
                if "continued" in line.casefold():
                    continue
                candidates.append(
                    {
                        "kind": "pin-definitions",
                        "page": page,
                        "line": line_number,
                        "table": _table_number(match.group("number")),
                        "title": line,
                        "device_pattern": match.group("device"),
                        "package": match.group("package").upper(),
                    }
                )
                continue
            if match := GENERIC_PIN_TITLE_RE.fullmatch(line):
                candidates.append(
                    {
                        "kind": "pin-definitions",
                        "page": page,
                        "line": line_number,
                        "table": _table_number(match.group("number")),
                        "title": line,
                        "device_pattern": None,
                        "package": None,
                    }
                )
                continue
            if match := AF_TITLE_RE.fullmatch(line):
                if any(
                    len(AF_COLUMN_RE.findall(_normalized(candidate))) >= 2
                    for candidate in lines[line_number : line_number + 6]
                ):
                    candidates.append(
                        {
                            "kind": "alternate-functions",
                            "page": page,
                            "line": line_number,
                            "table": _table_number(match.group("number")),
                            "title": line,
                        }
                    )
                continue
            if len(AF_COLUMN_RE.findall(line)) < 2:
                continue
            previous = [_normalized(item) for item in lines[max(0, line_number - 6) : line_number - 1]]
            if any(AF_TITLE_RE.fullmatch(item) for item in previous):
                continue
            candidates.append(
                {
                    "kind": "alternate-functions",
                    "page": page,
                    "line": line_number,
                    "table": None,
                    "title": line,
                }
            )
    return candidates


def find_pin_mentions(text: str) -> list[dict[str, object]]:
    return [
        {"page": page, "line": line, "text": normalized}
        for page, raw_page in enumerate(text.split("\f"), 1)
        for line, raw_line in enumerate(raw_page.splitlines(), 1)
        if (normalized := _normalized(raw_line)) and PIN_MENTION_RE.search(normalized)
    ]


def _functions(text: str) -> list[dict[str, object]]:
    functions = {}
    for match in FUNCTION_RE.finditer(" ".join(text.split())):
        source = match.group("source").casefold()
        for raw in match.group("value").split(","):
            value = raw.strip().strip(".;:")
            footnote_match = re.search(r"\((\d+)\)$", value)
            footnote = int(footnote_match.group(1)) if footnote_match else None
            if footnote_match:
                value = value[: footnote_match.start()].strip()
            if re.fullmatch(r"[A-Z][A-Z0-9_/]*", value) is None:
                continue
            row: dict[str, object] = {"source": source, "name": value}
            if footnote is not None:
                row["footnote"] = footnote
            functions[(source, value, footnote)] = row
    return [functions[key] for key in sorted(functions)]


def parse_pin_rows(text: str, column_order: str) -> list[dict[str, object]]:
    """解析普通芯片或模块管脚表中的 GPIO 行。"""
    if column_order not in {"name-first", "position-first"}:
        raise ValueError(f"未知管脚表列序：{column_order}")
    expression = NAME_FIRST_ROW_RE if column_order == "name-first" else POSITION_FIRST_ROW_RE
    lines = text.splitlines()
    anchors = [(index, match) for index, line in enumerate(lines) if (match := expression.match(line))]
    starts = []
    for index, match in anchors:
        start = index
        if index and re.search(r"\bDefault:\s*\S+", lines[index - 1]):
            start -= 1
        starts.append((start, index, match))
    pins = []
    for offset, (start, anchor, match) in enumerate(starts):
        end = starts[offset + 1][0] if offset + 1 < len(starts) else len(lines)
        rest = match.group("rest")
        category = re.search(r"\b(?:Default|Alternate|Remap|Additional):", rest)
        rest = rest[category.start() :] if category else ""
        chunk = "\n".join(
            [*lines[start:anchor], rest, *lines[anchor + 1 : end]]
        )
        pins.append(
            {
                "name": match.group("name"),
                "position": match.group("position"),
                "type": match.group("pin_type"),
                "functions": _functions(chunk),
            }
        )
    return pins


def parse_pin_tables(
    text: str, candidates: list[dict[str, object]]
) -> list[dict[str, object]]:
    """按下一张已识别表作为边界，解析数据手册中的全部封装管脚表。"""
    flat = [
        {"page": page, "line": line, "text": raw_line}
        for page, raw_page in enumerate(text.split("\f"), 1)
        for line, raw_line in enumerate(raw_page.splitlines(), 1)
    ]
    positions = {(int(row["page"]), int(row["line"])): index for index, row in enumerate(flat)}
    ordered = sorted(candidates, key=lambda row: (int(row["page"]), int(row["line"])))
    tables = []
    for index, candidate in enumerate(ordered):
        if candidate["kind"] != "pin-definitions":
            continue
        start = positions[(int(candidate["page"]), int(candidate["line"]))]
        end = len(flat)
        if index + 1 < len(ordered):
            following = ordered[index + 1]
            end = positions[(int(following["page"]), int(following["line"]))]
        table_number = str(candidate["table"])
        for cursor in range(start + 1, end):
            line = _normalized(str(flat[cursor]["text"]))
            if line.casefold().startswith("notes:") or SECTION_RE.match(line):
                end = cursor
                break
            if match := ANY_TABLE_RE.match(line):
                number = _table_number(match.group(1))
                if number != table_number or "continued" not in line.casefold():
                    end = cursor
                    break
        segment = "\n".join(str(row["text"]) for row in flat[start:end])
        by_order = {
            order: parse_pin_rows(segment, order)
            for order in ("name-first", "position-first")
        }
        preferred = "position-first" if candidate.get("package") is None else "name-first"
        column_order = max(
            by_order,
            key=lambda order: (len(by_order[order]), order == preferred),
        )
        tables.append(
            {
                **candidate,
                "page_end": int(flat[end - 1]["page"]) if end > start else int(candidate["page"]),
                "column_order": column_order,
                "pins": by_order[column_order],
            }
        )
    return tables


def build_inventory(report: dict[str, object], text_dir: Path) -> dict[str, object]:
    datasheets = report.get("datasheets")
    if report.get("schema_version") != 1 or not isinstance(datasheets, list):
        raise ValueError("数据手册文本报告格式无效")
    rows = []
    for datasheet in datasheets:
        if not isinstance(datasheet, dict):
            raise ValueError("数据手册文本报告包含非法条目")
        cache_name = str(datasheet["text_cache"])
        if Path(cache_name).name != cache_name:
            raise ValueError(f"数据手册文本缓存名非法：{cache_name}")
        source = text_dir / cache_name
        if not source.is_file() or common._sha256(source) != str(datasheet["text_sha256"]):
            raise ValueError(f"数据手册文本缺失或哈希不匹配：{source}")
        text = source.read_text(encoding="utf-8")
        candidates = find_candidates(text)
        pin_tables = parse_pin_tables(text, candidates)
        rows.append(
            {
                "name": datasheet["name"],
                "document_id": datasheet["document_id"],
                "pdf": datasheet["pdf"],
                "text_cache": cache_name,
                "text_sha256": datasheet["text_sha256"],
                "pin_tables": pin_tables,
                "alternate_function_tables": [
                    row for row in candidates if row["kind"] == "alternate-functions"
                ],
                "pin_mentions": find_pin_mentions(text),
            }
        )
    return {
        "schema_version": 1,
        "summary": {
            "datasheets": len(rows),
            "datasheets_with_pin_tables": sum(bool(row["pin_tables"]) for row in rows),
            "datasheets_without_pin_tables": sum(not row["pin_tables"] for row in rows),
            "datasheets_with_alternate_function_tables": sum(
                bool(row["alternate_function_tables"]) for row in rows
            ),
            "pin_tables": sum(len(row["pin_tables"]) for row in rows),
            "pin_tables_without_gpio_rows": sum(
                not table["pins"] for row in rows for table in row["pin_tables"]
            ),
            "gpio_rows": sum(
                len(table["pins"]) for row in rows for table in row["pin_tables"]
            ),
            "pin_functions": sum(
                len(pin["functions"])
                for row in rows
                for table in row["pin_tables"]
                for pin in table["pins"]
            ),
            "alternate_function_tables": sum(
                len(row["alternate_function_tables"]) for row in rows
            ),
        },
        "datasheets": rows,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datasheet-report",
        type=Path,
        default=repo_root / "reports/gigadevice-datasheets.json",
    )
    parser.add_argument(
        "--text-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/datasheet-text-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-datasheet-pins.json",
    )
    parser.add_argument("--show-datasheet")
    parser.add_argument("--show-page", type=int)
    parser.add_argument("--minimum-datasheets", type=int, default=60)
    parser.add_argument("--maximum-missing-pin-tables", type=int, default=0)
    parser.add_argument("--maximum-empty-pin-tables", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    inventory = build_inventory(
        json.loads(args.datasheet_report.read_text(encoding="utf-8")), args.text_dir
    )
    if args.show_page is not None and args.show_datasheet is None:
        raise ValueError("--show-page 必须和 --show-datasheet 同时使用")
    if args.show_datasheet is not None:
        row = next(
            (item for item in inventory["datasheets"] if item["name"] == args.show_datasheet),
            None,
        )
        if row is None:
            raise ValueError(f"未找到数据手册：{args.show_datasheet}")
        if args.show_page is None:
            print(json.dumps(row["pin_mentions"], ensure_ascii=False, indent=2))
            return 0
        pages = (args.text_dir / row["text_cache"]).read_text(encoding="utf-8").split("\f")
        if args.show_page < 1 or args.show_page > len(pages):
            raise ValueError(f"数据手册页码越界：{args.show_page}")
        print(pages[args.show_page - 1])
        return 0
    inventory["provenance"] = {
        "datasheet_report": {
            "path": args.datasheet_report.name,
            "sha256": common._sha256(args.datasheet_report),
        }
    }
    common._write_text_atomic(
        args.output,
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = inventory["summary"]
    if int(summary["datasheets"]) < args.minimum_datasheets:
        raise ValueError("数据手册数量低于覆盖门限")
    if int(summary["datasheets_without_pin_tables"]) > args.maximum_missing_pin_tables:
        missing = ", ".join(
            str(row["name"]) for row in inventory["datasheets"] if not row["pin_tables"]
        )
        raise ValueError(f"仍有数据手册未定位到实际管脚定义表：{missing}")
    if int(summary["pin_tables_without_gpio_rows"]) > args.maximum_empty_pin_tables:
        empty = ", ".join(
            f"{row['name']}:{table['table']}"
            for row in inventory["datasheets"]
            for table in row["pin_tables"]
            if not table["pins"]
        )
        raise ValueError(f"仍有管脚定义表未解析出 GPIO 行：{empty}")
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"数据手册管脚表盘点：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
