#!/usr/bin/env python3
"""索引 GD32 Firmware 中许可明确的器件头文件、中断号和寄存器基址。"""

from __future__ import annotations

import argparse
import ast
import json
import operator
import re
import sys
from pathlib import Path

import analyze_gigadevice_coverage as coverage
import gigadevice_sources as common


PERMISSIVE_LICENSES = {"Apache-2.0", "BSD-3-Clause", "MIT"}
DEFINE_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+(.+?)[ \t]*$",
    re.MULTILINE,
)
BASE_NAME_RE = re.compile(r"^[A-Za-z_]\w*_BASE(?:_[A-Z0-9]+)?$")
IRQ_ENUM_RE = re.compile(
    r"typedef\s+enum(?:\s+[A-Za-z_]\w*)?\s*\{(?P<body>.*?)\}\s*IRQn_Type\s*;",
    re.DOTALL,
)
CAST_RE = re.compile(
    r"\(\s*(?:(?:u?int(?:8|16|32|64)_t)|uintptr_t|size_t|unsigned\s+(?:char|short|int|long)|unsigned|long|int)\s*\)"
)
INTEGER_SUFFIX_RE = re.compile(r"\b(0[xX][0-9A-Fa-f]+|\d+)[uUlL]+\b")
COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
}


def _evaluate(expression: str, values: dict[str, int]) -> int | None:
    expression = INTEGER_SUFFIX_RE.sub(r"\1", CAST_RE.sub("", expression)).strip()
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None

    def visit(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert)):
            value = visit(node.operand)
            if isinstance(node.op, ast.UAdd):
                return value
            if isinstance(node.op, ast.USub):
                return -value
            return ~value
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            return OPERATORS[type(node.op)](visit(node.left), visit(node.right))
        raise ValueError("不支持的 C 整数表达式")

    try:
        return visit(tree)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _base_addresses(text: str) -> dict[str, int]:
    source = COMMENT_RE.sub("", text.replace("\\\n", ""))
    expressions = {
        name: expression.strip()
        for name, expression in DEFINE_RE.findall(source)
        if BASE_NAME_RE.fullmatch(name) is not None
    }
    values: dict[str, int] = {}
    while expressions:
        resolved = {
            name: value
            for name, expression in expressions.items()
            if (value := _evaluate(expression, values)) is not None
        }
        if not resolved:
            break
        values.update(resolved)
        for name in resolved:
            del expressions[name]
    return dict(sorted(values.items()))


def _interrupts(text: str) -> list[dict[str, int | str]]:
    values: dict[str, int] = {}
    interrupts: dict[str, int] = {}
    for match in IRQ_ENUM_RE.finditer(text):
        body = COMMENT_RE.sub("", match.group("body"))
        previous: int | None = None
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            item = re.fullmatch(
                r"([A-Za-z_]\w*(?:_IRQn|_IRQChannel))\s*(?:=\s*([^,]+))?\s*,?",
                line,
            )
            if item is None:
                continue
            identifier = item.group(1)
            expression = item.group(2)
            if expression is not None:
                value = _evaluate(expression, values)
                if value is None:
                    continue
            elif previous is not None:
                value = previous + 1
            else:
                continue
            previous = value
            values[identifier] = value
            name = re.sub(r"(?:_IRQn|_IRQChannel)$", "", identifier)
            if name in interrupts and interrupts[name] != value:
                raise ValueError(f"中断 {name} 在同一头文件中存在冲突值")
            interrupts[name] = value
    return [{"name": name, "value": value} for name, value in interrupts.items()]


def parse_header_facts(text: str) -> dict[str, object]:
    return {"base_addresses": _base_addresses(text), "interrupts": _interrupts(text)}


def is_device_header(text: str) -> bool:
    return IRQ_ENUM_RE.search(text) is not None and bool(_base_addresses(text))


def _source_marker(library_root: Path, item: dict[str, object]) -> dict[str, object]:
    marker_path = library_root / ".source.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    expected_archive = str(item["filename"])
    expected_sha256 = str(item["sha256"])
    if (
        marker.get("schema_version") != 1
        or marker.get("archive") != expected_archive
        or marker.get("archive_sha256") != expected_sha256
    ):
        raise ValueError(f"固件解包标记与锁文件不一致：{marker_path}")
    return marker


def _device_headers(library_root: Path) -> list[dict[str, object]]:
    by_hash: dict[str, dict[str, object]] = {}
    for path in sorted(library_root.rglob("*.h")):
        if not any(part.casefold() == "firmware" for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not is_device_header(text):
            continue
        relative = path.relative_to(library_root).as_posix()
        sha256 = common._sha256(path)
        if sha256 in by_hash:
            duplicates = by_hash[sha256]["duplicate_paths"]
            assert isinstance(duplicates, list)
            duplicates.append(relative)
            continue
        license_name = coverage.source_license(text[:16384])
        row: dict[str, object] = {
            "path": relative,
            "duplicate_paths": [],
            "sha256": sha256,
            "size": path.stat().st_size,
            "license": license_name,
            "publishable": license_name in PERMISSIVE_LICENSES,
        }
        if license_name in PERMISSIVE_LICENSES:
            row.update(parse_header_facts(text))
        by_hash[sha256] = row
    return sorted(by_hash.values(), key=lambda row: str(row["path"]))


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
        marker = _source_marker(library_root, raw_item)
        headers = _device_headers(library_root)
        libraries.append(
            {
                "series": coverage._series_from_firmware_filename(filename),
                "version": raw_item["version"],
                "document_id": raw_item["document_id"],
                "archive_sha256": raw_item["sha256"],
                "tree_sha256": marker["tree_sha256"],
                "device_headers": headers,
            }
        )
    libraries.sort(key=lambda row: str(row["series"]).casefold())
    headers = [header for library in libraries for header in library["device_headers"]]
    return {
        "schema_version": 1,
        "summary": {
            "firmware_libraries": len(libraries),
            "unique_device_headers": len(headers),
            "permissive_device_headers": sum(bool(header["publishable"]) for header in headers),
            "libraries_without_device_header": sum(
                not library["device_headers"] for library in libraries
            ),
        },
        "libraries": libraries,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock", type=Path, default=repo_root / "sources/gigadevice/firmware.lock.json"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/firmware-sources-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-headers.json",
    )
    parser.add_argument("--minimum-firmware-libraries", type=int, default=33)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_report(
        json.loads(args.lock.read_text(encoding="utf-8")),
        args.root,
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    if int(summary["firmware_libraries"]) < args.minimum_firmware_libraries:
        raise ValueError("Firmware 库数量低于覆盖门限")
    if int(summary["libraries_without_device_header"]) != 0:
        raise ValueError("仍有 Firmware 库缺少可识别的器件头文件")
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Firmware 头文件报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
