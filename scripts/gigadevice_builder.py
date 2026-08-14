#!/usr/bin/env python3
"""发现、下载并提取 GigaDevice Embedded Builder 结构化数据。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import gigadevice_sources as common


SOURCE_PAGE = f"{common.BASE_URL}/cn/download/7?kw=Embedded%20Builder"
AGREEMENT_URL = f"{common.BASE_URL}/download/agree/box_id/15/document_id/{{document_id}}/path_type/1"
DOWNLOAD_URL = f"{common.BASE_URL}/download/down/document_id/{{document_id}}/path_type/1"


class BuilderSource(NamedTuple):
    version: str
    document_id: int
    published: str


class BuilderRecord(NamedTuple):
    source: BuilderSource
    url: str
    filename: str
    size: int
    sha256: str
    status: str = "active"


def find_extracted_root(cache: Path, archive_sha256: str) -> Path:
    matches = [
        marker.parent
        for marker in cache.glob("*/.complete")
        if marker.read_text(encoding="utf-8").strip() == archive_sha256
    ]
    if len(matches) != 1:
        raise ValueError(f"Builder 解包缓存必须唯一匹配来源哈希，实际为 {len(matches)} 个")
    return matches[0]


def parse_builder_page(html: str) -> BuilderSource:
    entries, _ = common.parse_download_page(html)
    matches = [
        entry
        for entry in entries
        if entry.name == "GD32 Embedded Builder" and entry.box_id == 15
    ]
    if len(matches) != 1:
        raise ValueError(f"官网必须提供唯一 GD32 Embedded Builder，实际为 {len(matches)} 个")
    entry = matches[0]
    if not re.fullmatch(r"\d+\.\d+\.\d+_Rel_r\d+", entry.version):
        raise ValueError(f"Builder 版本格式异常：{entry.version!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.published):
        raise ValueError(f"Builder 发布日期格式异常：{entry.published!r}")
    return BuilderSource(entry.version, entry.document_id, entry.published)


def discover_builder() -> BuilderSource:
    return parse_builder_page(common._read_text(SOURCE_PAGE))


def resolve_download_url(source: BuilderSource) -> str:
    opener = urllib.request.build_opener(common._NoRedirect)
    try:
        opener.open(common._request(DOWNLOAD_URL.format(document_id=source.document_id)), timeout=60)
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        if not location:
            raise ValueError("Builder 下载响应缺少 Location") from error
        return common.validate_download_url(location)
    raise ValueError("Builder 下载接口未返回预期重定向")


def materialize(source: BuilderSource, cache_dir: Path, adopt: Path | None) -> BuilderRecord:
    url = resolve_download_url(source)
    filename = PurePosixPath(urllib.parse.urlsplit(url).path).name
    path = cache_dir / filename
    expected_size = common._remote_size(url)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not path.is_file() and adopt is not None:
        if not adopt.is_file():
            raise ValueError(f"待接管的 Builder 归档不存在：{adopt}")
        common._verify_archive(adopt)
        if expected_size is not None and adopt.stat().st_size != expected_size:
            raise ValueError(
                f"待接管归档长度不匹配：期望 {expected_size}，实际 {adopt.stat().st_size}"
            )
        temporary = cache_dir / f".{filename}.adopt"
        if temporary.exists():
            raise ValueError(f"接管临时路径已存在：{temporary}")
        os.link(adopt, temporary)
        temporary.replace(path)

    if not path.is_file() or (expected_size is not None and path.stat().st_size != expected_size):
        common._download(url, path)
    size = path.stat().st_size
    if expected_size is not None and size != expected_size:
        raise ValueError(f"Builder 长度不匹配：期望 {expected_size}，实际 {size}")
    common._verify_archive(path)
    return BuilderRecord(source, url, filename, size, common._sha256(path))


def _builder_record_data(record: BuilderRecord) -> dict[str, object]:
    return {
        "name": "GD32 Embedded Builder",
        "version": record.source.version,
        "published": record.source.published,
        "document_id": record.source.document_id,
        "agreement_url": AGREEMENT_URL.format(document_id=record.source.document_id),
        "url": record.url,
        "filename": record.filename,
        "size": record.size,
        "sha256": record.sha256,
        "status": record.status,
    }


def _builder_record_from_data(record: dict[str, object]) -> BuilderRecord:
    return BuilderRecord(
        BuilderSource(
            str(record["version"]),
            int(record["document_id"]),
            str(record["published"]),
        ),
        str(record["url"]),
        str(record["filename"]),
        int(record["size"]),
        str(record["sha256"]),
        str(record.get("status", "active")),
    )


def incremental_builder_record(
    source: BuilderSource,
    manifest_path: Path,
    cache_dir: Path,
    adopt: Path | None,
) -> tuple[BuilderRecord, list[dict[str, object]], dict[str, list[str]]]:
    locked: dict[str, object] = {}
    if manifest_path.is_file():
        locked = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_record = locked.get("builder")
    current = []
    if isinstance(current_record, dict):
        current = [{"name": "GD32 Embedded Builder", **current_record}]
    elif current_record is not None:
        raise ValueError("Builder 锁文件格式无效")
    history = locked.get("history", [])
    if not isinstance(history, list):
        raise ValueError("Builder 历史记录格式无效")
    discovered = [{"name": "GD32 Embedded Builder", **source._asdict()}]
    plan = common.plan_source_updates(current, discovered)
    changed = plan["added"] or plan["updated"]
    materialized = [_builder_record_data(materialize(source, cache_dir, adopt))] if changed else []
    merged, merged_history = common.merge_source_updates(
        current, materialized, plan, history
    )
    return _builder_record_from_data(merged[0]), merged_history, plan


def write_manifest(
    path: Path,
    record: BuilderRecord,
    history: list[dict[str, object]] | None = None,
) -> None:
    data = {
        "schema_version": 1,
        "source_page": SOURCE_PAGE,
        "license": {
            "agreement": "SLA-GD0003-version1.1",
            "redistribution": "Builder 原始归档与 XML 仅作本地来源和交叉验证，不随生成仓库发布。",
        },
        "builder": _builder_record_data(record),
    }
    if history:
        data["history"] = history
    common._write_text_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/tools",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo_root / "sources/gigadevice/builder.lock.json",
    )
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--adopt", type=Path)
    parser.add_argument("--extract", action="store_true")
    parser.add_argument(
        "--extract-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/builder-resources",
    )
    parser.add_argument(
        "--firmware-extract-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/builder-firmware-v1",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source = discover_builder()
    if not args.download:
        print(f"{source.document_id}\t{source.version}\t{source.published}\tGD32 Embedded Builder")
        print("添加 --download 后下载并生成锁定清单。")
        return 0
    if os.environ.get("GIGADEVICE_ACCEPT_SLA_GD0003") != "1":
        raise ValueError("下载前必须设置 GIGADEVICE_ACCEPT_SLA_GD0003=1 明确接受 SLA-GD0003")
    record, history, _ = incremental_builder_record(
        source, args.manifest, args.cache_dir, args.adopt
    )
    write_manifest(args.manifest, record, history)
    if args.extract:
        script = Path(__file__).with_name("research-gigadevice-builder.sh")
        subprocess.run([script, args.cache_dir / record.filename, args.extract_cache], check=True)
        firmware_script = Path(__file__).with_name(
            "research-gigadevice-builder-firmware.sh"
        )
        subprocess.run(
            [firmware_script, args.cache_dir / record.filename, args.firmware_extract_cache],
            check=True,
        )
    print(f"已校验 Builder {record.source.version}：{args.manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, urllib.error.URLError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
