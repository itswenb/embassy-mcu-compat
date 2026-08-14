#!/usr/bin/env python3
"""精确发现、下载并锁定一个 GigaDevice 官方工具。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import NamedTuple

import gigadevice_builder as builder
import gigadevice_sources as common


DEFAULT_NAME = "GD32 All-In-One Programmer"


class ToolSource(NamedTuple):
    name: str
    version: str
    box_id: int
    document_id: int
    published: str


def parse_tool_page(html: str, name: str) -> ToolSource:
    entries, _ = common.parse_download_page(html)
    matches = [entry for entry in entries if entry.name == name]
    if len(matches) != 1:
        raise ValueError(f"官网必须提供唯一精确匹配的工具 {name!r}，实际为 {len(matches)} 个")
    entry = matches[0]
    if not entry.version or re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.published) is None:
        raise ValueError(f"官方工具版本或发布日期格式异常：{entry}")
    return ToolSource(*entry)


def discover_tool(name: str, keyword: str) -> ToolSource:
    encoded = urllib.parse.quote(keyword)
    first_url = f"{common.BASE_URL}/cn/download/7?kw={encoded}"
    first_html = common._read_text(first_url)
    _, page_count = common.parse_download_page(first_html)
    matches = []
    for page in range(1, page_count + 1):
        html = (
            first_html
            if page == 1
            else common._read_text(
                f"{common.BASE_URL}/cn/download/7/p/{page}?kw={encoded}"
            )
        )
        entries, discovered_pages = common.parse_download_page(html)
        if discovered_pages != page_count:
            raise ValueError("官方工具列表翻页信息不一致")
        matches.extend(entry for entry in entries if entry.name == name)
    if len(matches) != 1:
        raise ValueError(f"官网必须提供唯一精确匹配的工具 {name!r}，实际为 {len(matches)} 个")
    entry = matches[0]
    return ToolSource(entry.name, entry.version, entry.box_id, entry.document_id, entry.published)


def _tool_record_data(
    source: ToolSource, record: builder.BuilderRecord
) -> dict[str, object]:
    return {
        "name": source.name,
        "version": source.version,
        "published": source.published,
        "box_id": source.box_id,
        "document_id": source.document_id,
        "agreement_url": (
            f"{common.BASE_URL}/download/agree/box_id/{source.box_id}"
            f"/document_id/{source.document_id}/path_type/1"
        ),
        "url": record.url,
        "filename": record.filename,
        "size": record.size,
        "sha256": record.sha256,
        "status": record.status,
    }


def _tool_record_from_data(record: dict[str, object]) -> builder.BuilderRecord:
    source = ToolSource(
        str(record["name"]),
        str(record["version"]),
        int(record["box_id"]),
        int(record["document_id"]),
        str(record["published"]),
    )
    return builder.BuilderRecord(
        source,
        str(record["url"]),
        str(record["filename"]),
        int(record["size"]),
        str(record["sha256"]),
        str(record.get("status", "active")),
    )


def incremental_tool_record(
    source: ToolSource,
    manifest_path: Path,
    cache_dir: Path,
    adopt: Path | None,
) -> tuple[builder.BuilderRecord, list[dict[str, object]], dict[str, list[str]]]:
    locked: dict[str, object] = {}
    if manifest_path.is_file():
        locked = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_record = locked.get("tool")
    current = [current_record] if isinstance(current_record, dict) else []
    if current_record is not None and not isinstance(current_record, dict):
        raise ValueError("官方工具锁文件格式无效")
    history = locked.get("history", [])
    if not isinstance(history, list):
        raise ValueError("官方工具历史记录格式无效")
    discovered = [source._asdict()]
    plan = common.plan_source_updates(
        current,
        discovered,
        compare_fields=("version", "document_id", "published", "box_id"),
    )
    changed = plan["added"] or plan["updated"]
    materialized = []
    if changed:
        record = builder.materialize(source, cache_dir, adopt)
        materialized.append(_tool_record_data(source, record))
    merged, merged_history = common.merge_source_updates(
        current, materialized, plan, history
    )
    return _tool_record_from_data(merged[0]), merged_history, plan


def write_manifest(
    path: Path,
    source: ToolSource,
    record: builder.BuilderRecord,
    history: list[dict[str, object]] | None = None,
) -> None:
    data = {
        "schema_version": 1,
        "source_page": f"{common.BASE_URL}/cn/download/7?kw={urllib.parse.quote(source.name)}",
        "license": {
            "agreement": "SLA-GD0003-version1.1",
            "redistribution": "官方工具原始归档仅作本地来源验证，不随生成仓库发布。",
        },
        "tool": _tool_record_data(source, record),
    }
    if history:
        data["history"] = history
    common._write_text_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--keyword")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--adopt", type=Path)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/tools",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo_root / "sources/gigadevice/programmer.lock.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source = discover_tool(args.name, args.keyword or args.name)
    if not args.download:
        print(
            f"{source.document_id}\t{source.version}\t{source.published}\t"
            f"box={source.box_id}\t{source.name}"
        )
        print("添加 --download 后下载并生成锁定清单。")
        return 0
    if os.environ.get("GIGADEVICE_ACCEPT_SLA_GD0003") != "1":
        raise ValueError("下载前必须设置 GIGADEVICE_ACCEPT_SLA_GD0003=1 明确接受 SLA-GD0003")
    record, history, _ = incremental_tool_record(
        source, args.manifest, args.cache_dir, args.adopt
    )
    write_manifest(args.manifest, source, record, history)
    print(f"已锁定官方工具 {source.name} {source.version}：{args.manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
