#!/usr/bin/env python3
"""发现、下载并提取 GigaDevice 官方 AddOn 中的 CMSIS Pack。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import gigadevice_sources as common


SOURCE_PAGE = f"{common.BASE_URL}/cn/download/7?kw=Addon"
AGREEMENT_URL = f"{common.BASE_URL}/download/agree/box_id/13/document_id/{{document_id}}/path_type/1"
DOWNLOAD_URL = f"{common.BASE_URL}/download/down/document_id/{{document_id}}/path_type/1"


class AddonSource(NamedTuple):
    name: str
    version: str
    document_id: int
    published: str


class AddonRecord(NamedTuple):
    source: AddonSource
    url: str
    filename: str
    size: int
    sha256: str
    status: str = "active"


def parse_addon_page(html: str) -> tuple[list[AddonSource], int]:
    entries, page_count = common.parse_download_page(html)
    addons = []
    for entry in entries:
        name = " ".join(entry.name.replace("_", " ").split())
        if entry.box_id != 13 or not re.fullmatch(r"GD32.+ Add[Oo]n", name):
            continue
        if not re.fullmatch(r"\d+\.\d+\.\d+", entry.version):
            raise ValueError(f"AddOn 版本格式异常：{entry.version!r}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.published):
            raise ValueError(f"AddOn 发布日期格式异常：{entry.published!r}")
        addons.append(AddonSource(name, entry.version, entry.document_id, entry.published))
    return addons, page_count


def discover_addons() -> list[AddonSource]:
    first, page_count = parse_addon_page(common._read_text(SOURCE_PAGE))
    pages = [first]
    for page in range(2, page_count + 1):
        url = f"{common.BASE_URL}/cn/download/7/p/{page}?kw=Addon"
        entries, discovered_count = parse_addon_page(common._read_text(url))
        if discovered_count != page_count:
            raise ValueError("AddOn 翻页信息在页面之间不一致")
        pages.append(entries)
    by_document: dict[int, AddonSource] = {}
    by_name: dict[str, AddonSource] = {}
    for source in (entry for page in pages for entry in page):
        if source.document_id in by_document:
            raise ValueError(f"重复的 AddOn document_id：{source.document_id}")
        if source.name in by_name:
            raise ValueError(f"重复的 AddOn 名称：{source.name}")
        by_document[source.document_id] = source
        by_name[source.name] = source
    return sorted(by_document.values(), key=lambda item: item.name.casefold())


def resolve_download_url(source: AddonSource) -> str:
    opener = urllib.request.build_opener(common._NoRedirect)
    try:
        opener.open(common._request(DOWNLOAD_URL.format(document_id=source.document_id)), timeout=60)
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        if not location:
            raise ValueError(f"AddOn {source.document_id} 下载响应缺少 Location") from error
        return common.validate_download_url(location)
    raise ValueError(f"AddOn {source.document_id} 未返回预期重定向")


def _adopt_by_size(target: Path, expected_size: int | None, adopt_dir: Path | None) -> None:
    if target.exists() or expected_size is None or adopt_dir is None or not adopt_dir.is_dir():
        return
    candidates = [
        path
        for path in adopt_dir.glob("*.7z")
        if path.is_file() and path.stat().st_size == expected_size and path.resolve() != target.resolve()
    ]
    if len(candidates) != 1:
        return
    common._verify_archive(candidates[0])
    temporary = target.parent / f".{target.name}.adopt"
    if temporary.exists():
        raise ValueError(f"接管临时路径已存在：{temporary}")
    os.link(candidates[0], temporary)
    temporary.replace(target)


def materialize(source: AddonSource, cache_dir: Path, adopt_dir: Path | None) -> AddonRecord:
    url = resolve_download_url(source)
    filename = PurePosixPath(urllib.parse.urlsplit(url).path).name
    path = cache_dir / filename
    expected_size = common._remote_size(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _adopt_by_size(path, expected_size, adopt_dir)
    if not path.is_file() or (expected_size is not None and path.stat().st_size != expected_size):
        common._download(url, path)
    size = path.stat().st_size
    if expected_size is not None and size != expected_size:
        raise ValueError(f"{filename} 长度不匹配：期望 {expected_size}，实际 {size}")
    common._verify_archive(path)
    return AddonRecord(source, url, filename, size, common._sha256(path))


def extract_addon(record: AddonRecord, archive: Path, extract_root: Path) -> Path:
    output = extract_root / PurePosixPath(record.filename).stem
    marker = output / ".source.json"
    marker_base = {
        "archive": record.filename,
        "archive_sha256": record.sha256,
        "schema_version": 1,
    }
    if marker.is_file():
        actual = json.loads(marker.read_text(encoding="utf-8"))
        if (
            all(actual.get(key) == value for key, value in marker_base.items())
            and actual.get("tree_sha256") == common.tree_sha256(output)
        ):
            return output
        raise ValueError(f"AddOn 解包缓存校验失败，请检查后删除：{output}")
    if output.exists():
        raise ValueError(f"AddOn 解包目录存在但缺少来源标记，请检查后删除：{output}")

    extract_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=extract_root, prefix=".addon-extract.") as directory:
        temporary = Path(directory)
        inner_archive = common.extract_verified_inner(archive, temporary)
        payload = temporary / "payload"
        common._extract_archive(inner_archive, payload)
        pack_sources = sorted(
            (path for path in payload.rglob("*") if path.is_file() and path.suffix.lower() == ".pack"),
            key=lambda path: path.name.casefold(),
        )

        result = temporary / "result"
        packs_dir = result / "packs"
        unpacked_dir = result / "unpacked"
        packs_dir.mkdir(parents=True)
        unpacked_dir.mkdir(parents=True)
        inventory = []
        for source_pack in pack_sources:
            target_pack = packs_dir / source_pack.name
            if target_pack.exists():
                raise ValueError(f"AddOn 包含重复 Pack 文件名：{source_pack.name}")
            shutil.copy2(source_pack, target_pack)
            common._verify_archive(target_pack)
            target_tree = unpacked_dir / source_pack.stem
            common._extract_archive(target_pack, target_tree)
            pdsc = sorted(path.relative_to(target_tree).as_posix() for path in target_tree.rglob("*.pdsc"))
            inventory.append(
                {
                    "filename": source_pack.name,
                    "sha256": common._sha256(target_pack),
                    "pdsc": pdsc,
                    "tree_sha256": common.tree_sha256(target_tree),
                }
            )

        common._write_text_atomic(
            result / "inventory.json",
            json.dumps(
                {"schema_version": 1, "cmsis_pack_count": len(inventory), "packs": inventory},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        marker_base["tree_sha256"] = common.tree_sha256(result)
        common._write_text_atomic(
            result / ".source.json",
            json.dumps(marker_base, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        result.replace(output)
    return output


def _addon_record_data(record: AddonRecord) -> dict[str, object]:
    return {
        "name": record.source.name,
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


def _addon_record_from_data(record: dict[str, object]) -> AddonRecord:
    return AddonRecord(
        AddonSource(
            str(record["name"]),
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


def incremental_addon_records(
    sources: list[AddonSource],
    manifest_path: Path,
    cache_dir: Path,
    adopt_dir: Path | None,
) -> tuple[list[AddonRecord], list[dict[str, object]], dict[str, list[str]]]:
    locked: dict[str, object] = {}
    if manifest_path.is_file():
        locked = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = locked.get("addons", [])
    history = locked.get("history", [])
    if not isinstance(current, list) or not isinstance(history, list):
        raise ValueError("AddOn 锁文件格式无效")
    plan = common.plan_source_updates(current, [source._asdict() for source in sources])
    changed = set(plan["added"]) | set(plan["updated"])
    missing = common.missing_cached_names(
        cache_dir, current, [source.name for source in sources]
    )
    materialized = [
        _addon_record_data(materialize(source, cache_dir, adopt_dir))
        for source in sources
        if source.name in changed | missing
    ]
    merged, merged_history = common.merge_source_updates(
        current, materialized, plan, history
    )
    return [_addon_record_from_data(record) for record in merged], merged_history, plan


def write_manifest(
    path: Path,
    records: list[AddonRecord],
    history: list[dict[str, object]] | None = None,
) -> None:
    data = {
        "schema_version": 1,
        "source_pages": [
            SOURCE_PAGE,
            f"{common.BASE_URL}/cn/download/7/p/2?kw=Addon",
        ],
        "license": {
            "agreement": "SLA-GD0006-version1.1",
            "redistribution": "AddOn 与 Pack 原始文件仅作本地来源和交叉验证，不随生成仓库发布。",
        },
        "addons": [
            _addon_record_data(record)
            for record in sorted(records, key=lambda item: item.source.name.casefold())
        ],
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
        default=repo_root / ".cache/research/gigadevice/addons",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo_root / "sources/gigadevice/addons.lock.json",
    )
    parser.add_argument("--minimum-count", type=int, default=30)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--adopt-dir", type=Path)
    parser.add_argument("--extract-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    sources = discover_addons()
    if len(sources) < args.minimum_count:
        raise ValueError(f"官网仅发现 {len(sources)} 个当前 AddOn，少于下限 {args.minimum_count}")
    if not args.download:
        for source in sources:
            print(f"{source.document_id}\t{source.version}\t{source.name}")
        print(f"共发现 {len(sources)} 个 AddOn；添加 --download 后下载并生成锁定清单。")
        return 0
    if os.environ.get("GIGADEVICE_ACCEPT_SLA_GD0006") != "1":
        raise ValueError("下载前必须设置 GIGADEVICE_ACCEPT_SLA_GD0006=1 明确接受 SLA-GD0006")
    records, history, _ = incremental_addon_records(
        sources, args.manifest, args.cache_dir, args.adopt_dir
    )
    if args.extract_dir is not None:
        for record in records:
            extract_addon(record, args.cache_dir / record.filename, args.extract_dir)
    write_manifest(args.manifest, records, history)
    print(f"已校验 {len(records)} 个 AddOn：{args.manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, urllib.error.URLError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
