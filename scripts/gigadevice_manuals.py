#!/usr/bin/env python3
"""发现、下载并锁定 GigaDevice 官方 GD32 技术文档。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import gigadevice_sources as common


BASE_URL = "https://www.gd32mcu.com"
DOWNLOAD_URL = f"{BASE_URL}/download/down/document_id/{{document_id}}/path_type/{{path_type}}"
MAX_PDF_SIZE = 512 * 1024 * 1024


class DocumentKind(NamedTuple):
    name: str
    list_path: str
    title_markers: tuple[str, ...]
    download_path: str
    manifest_key: str


MANUAL_KIND = DocumentKind(
    "用户手册", "/cn/download/6", ("user manual", "用户手册"), "userManual", "manuals"
)
DATASHEET_KIND = DocumentKind(
    "数据手册", "/cn/download/5", ("datasheet", "数据手册"), "datasheet", "datasheets"
)
SOURCE_PAGE = f"{BASE_URL}{MANUAL_KIND.list_path}"


class ManualSource(NamedTuple):
    name: str
    version: str
    document_id: int
    published: str
    path_types: tuple[int, ...]


class ManualRecord(NamedTuple):
    source: ManualSource
    path_type: int
    url: str
    filename: str
    size: int
    sha256: str
    status: str = "active"


class _ManualPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict[str, object]] = []
        self.page_count = 1
        self._entry: dict[str, object] | None = None
        self._entry_depth = 0
        self._open_li_depth = 0
        self._capture: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "li":
            self._open_li_depth += 1
            if self._entry is None and "cl" in classes:
                self._entry = {"downloads": []}
                self._entry_depth = self._open_li_depth
        if tag == "a":
            href = values.get("href", "")
            page = re.search(r"/p/(\d+)(?:\?|$)", href)
            if page:
                self.page_count = max(self.page_count, int(page.group(1)))
            if self._entry is not None:
                match = re.fullmatch(
                    r"/download/(?:down|agree/box_id/12)/document_id/(\d+)/path_type/([12])",
                    href,
                )
                if match:
                    downloads = self._entry["downloads"]
                    assert isinstance(downloads, list)
                    downloads.append((int(match.group(1)), int(match.group(2))))
        fields = {
            "data-name": "name",
            "data-version": "version",
            "data-time": "published",
        }
        if tag == "dd" and self._entry is not None:
            for class_name, field in fields.items():
                if class_name in classes:
                    self._capture = field
                    self._text = []
                    break

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "dd" and self._capture is not None and self._entry is not None:
            self._entry[self._capture] = " ".join("".join(self._text).split())
            self._capture = None
            self._text = []
        if tag != "li":
            return
        if self._entry is None or self._open_li_depth != self._entry_depth:
            self._open_li_depth = max(0, self._open_li_depth - 1)
            return
        self.entries.append(self._entry)
        self._entry = None
        self._open_li_depth = max(0, self._open_li_depth - 1)


def parse_manual_page(
    html: str, kind: DocumentKind = MANUAL_KIND
) -> tuple[list[ManualSource], int]:
    parser = _ManualPageParser()
    parser.feed(html)
    parser.close()
    result = []
    for entry in parser.entries:
        name = " ".join(str(entry.get("name", "")).replace("_", " ").split())
        if not name.upper().startswith("GD32") or not any(
            marker in name.casefold() for marker in kind.title_markers
        ):
            continue
        downloads = entry.get("downloads")
        if not isinstance(downloads, list) or not downloads:
            raise ValueError(f"{kind.name}缺少下载入口：{name}")
        document_ids = {int(document_id) for document_id, _ in downloads}
        if len(document_ids) != 1:
            raise ValueError(f"{kind.name}下载入口文档编号冲突：{name}")
        version = str(entry.get("version", "")).strip()
        published = str(entry.get("published", "")).strip()
        if not version or re.fullmatch(r"\d{4}-\d{2}-\d{2}", published) is None:
            raise ValueError(f"{kind.name}版本或发布日期无效：{name}")
        result.append(
            ManualSource(
                name,
                version,
                document_ids.pop(),
                published,
                tuple(sorted({int(path_type) for _, path_type in downloads})),
            )
        )
    return sorted(result, key=lambda row: row.name.casefold()), parser.page_count


def merge_sources(
    *pages: list[ManualSource], kind: DocumentKind = MANUAL_KIND
) -> list[ManualSource]:
    by_document: dict[int, ManualSource] = {}
    by_name: dict[str, ManualSource] = {}
    for source in (item for page in pages for item in page):
        if source.document_id in by_document or source.name in by_name:
            raise ValueError(f"{kind.name}重复：{source.name}")
        by_document[source.document_id] = source
        by_name[source.name] = source
    return sorted(by_document.values(), key=lambda row: row.name.casefold())


def discover_documents(
    kind: DocumentKind, keyword: str | None = None
) -> list[ManualSource]:
    source_page = f"{BASE_URL}{kind.list_path}"
    if keyword:
        source_page += "?" + urllib.parse.urlencode({"kw": keyword})
    first, page_count = parse_manual_page(common._read_text(source_page), kind)
    pages = [first]
    for page in range(2, page_count + 1):
        url = f"{BASE_URL}{kind.list_path}/p/{page}"
        if keyword:
            url += "?" + urllib.parse.urlencode({"kw": keyword})
        entries, discovered = parse_manual_page(common._read_text(url), kind)
        if discovered != page_count:
            raise ValueError(f"{kind.name}列表翻页信息在页面之间不一致")
        pages.append(entries)
    return merge_sources(*pages, kind=kind)


def discover_manuals() -> list[ManualSource]:
    return discover_documents(MANUAL_KIND)


def validate_download_url(
    location: str, kind: DocumentKind = MANUAL_KIND
) -> str:
    url = urllib.parse.urljoin(BASE_URL, location)
    parsed = urllib.parse.urlsplit(url)
    decoded_path = urllib.parse.unquote(parsed.path)
    path = PurePosixPath(decoded_path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.gd32mcu.com"
        or parsed.query
        or parsed.fragment
        or not decoded_path.startswith(f"/data/documents/{kind.download_path}/")
        or ".." in path.parts
        or not path.name.casefold().endswith(".pdf")
        or any(character in path.name for character in ("/", "\\", "\x00"))
    ):
        raise ValueError(f"不安全的{kind.name}下载地址：{location!r}")
    encoded_path = urllib.parse.quote(decoded_path, safe="/-._~")
    return urllib.parse.urlunsplit(("https", parsed.netloc, encoded_path, "", ""))


def resolve_download_url(
    source: ManualSource, path_type: int, kind: DocumentKind = MANUAL_KIND
) -> str:
    opener = urllib.request.build_opener(common._NoRedirect)
    try:
        opener.open(
            common._request(
                DOWNLOAD_URL.format(
                    document_id=source.document_id, path_type=path_type
                )
            ),
            timeout=60,
        )
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        if not location:
            raise ValueError(f"{kind.name} {source.document_id} 缺少重定向地址") from error
        return validate_download_url(location, kind)
    raise ValueError(f"{kind.name} {source.document_id} 未返回预期重定向")


def _download_pdf(url: str, path: Path, kind: DocumentKind = MANUAL_KIND) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            with urllib.request.urlopen(common._request(url), timeout=120) as response:
                total = 0
                prefix = b""
                while chunk := response.read(1024 * 1024):
                    if not prefix:
                        prefix = chunk[:5]
                    total += len(chunk)
                    if total > MAX_PDF_SIZE:
                        raise ValueError(f"{kind.name}超过 {MAX_PDF_SIZE} 字节上限")
                    file.write(chunk)
            if prefix != b"%PDF-":
                raise ValueError(f"{kind.name}不是 PDF：{url}")
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def materialize(
    source: ManualSource, cache_dir: Path, kind: DocumentKind = MANUAL_KIND
) -> ManualRecord:
    path_type = 1 if 1 in source.path_types else 2
    url = resolve_download_url(source, path_type, kind)
    remote_name = PurePosixPath(urllib.parse.urlsplit(url).path).name
    filename = f"{source.document_id}-{path_type}-{remote_name}"
    path = cache_dir / filename
    expected_size = common._remote_size(url)
    if not path.is_file() or (expected_size is not None and path.stat().st_size != expected_size):
        _download_pdf(url, path, kind)
    size = path.stat().st_size
    if expected_size is not None and size != expected_size:
        raise ValueError(f"{filename} 长度不匹配：期望 {expected_size}，实际 {size}")
    if not path.read_bytes()[:5] == b"%PDF-":
        raise ValueError(f"{kind.name}缓存不是 PDF：{path}")
    return ManualRecord(source, path_type, url, filename, size, common._sha256(path))


def _manual_source_data(source: ManualSource) -> dict[str, object]:
    return {
        "name": source.name,
        "version": source.version,
        "published": source.published,
        "document_id": source.document_id,
        "available_path_types": list(source.path_types),
    }


def _manual_record_data(record: ManualRecord) -> dict[str, object]:
    return {
        **_manual_source_data(record.source),
        "selected_path_type": record.path_type,
        "url": record.url,
        "filename": record.filename,
        "size": record.size,
        "sha256": record.sha256,
        "status": record.status,
    }


def _manual_record_from_data(record: dict[str, object]) -> ManualRecord:
    path_types = record["available_path_types"]
    if not isinstance(path_types, list):
        raise ValueError("技术文档语言入口格式无效")
    return ManualRecord(
        ManualSource(
            str(record["name"]),
            str(record["version"]),
            int(record["document_id"]),
            str(record["published"]),
            tuple(int(value) for value in path_types),
        ),
        int(record["selected_path_type"]),
        str(record["url"]),
        str(record["filename"]),
        int(record["size"]),
        str(record["sha256"]),
        str(record.get("status", "active")),
    )


def incremental_manual_records(
    sources: list[ManualSource],
    manifest_path: Path,
    cache_dir: Path,
    kind: DocumentKind = MANUAL_KIND,
) -> tuple[list[ManualRecord], list[dict[str, object]], dict[str, list[str]]]:
    locked: dict[str, object] = {}
    if manifest_path.is_file():
        locked = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = locked.get(kind.manifest_key, [])
    history = locked.get("history", [])
    if not isinstance(current, list) or not isinstance(history, list):
        raise ValueError(f"{kind.name}锁文件格式无效")
    plan = common.plan_source_updates(
        current,
        [_manual_source_data(source) for source in sources],
        compare_fields=("version", "document_id", "published", "available_path_types"),
    )
    changed = set(plan["added"]) | set(plan["updated"])
    missing = common.missing_cached_names(
        cache_dir, current, [source.name for source in sources]
    )
    materialized = [
        _manual_record_data(materialize(source, cache_dir, kind))
        for source in sources
        if source.name in changed | missing
    ]
    merged, merged_history = common.merge_source_updates(
        current, materialized, plan, history
    )
    return [_manual_record_from_data(record) for record in merged], merged_history, plan


def write_manifest(
    path: Path,
    records: list[ManualRecord],
    kind: DocumentKind = MANUAL_KIND,
    history: list[dict[str, object]] | None = None,
) -> None:
    manifest = {
        "schema_version": 1,
        "document_kind": kind.name,
        "source_pages": [f"{BASE_URL}{kind.list_path}"],
        "redistribution": "原始 PDF 仅作来源缓存；公开仓库只提交哈希、定位信息和事实型派生数据。",
        kind.manifest_key: [
            _manual_record_data(row)
            for row in sorted(records, key=lambda item: item.source.name.casefold())
        ],
    }
    if history:
        manifest["history"] = history
    common._write_text_atomic(
        path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("manual", "datasheet"), default="manual")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--minimum-count", type=int)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--keyword")
    args = parser.parse_args()
    kind = MANUAL_KIND if args.kind == "manual" else DATASHEET_KIND
    slug = kind.manifest_key
    args.document_kind = kind
    args.cache_dir = args.cache_dir or repo_root / f".cache/research/gigadevice/{slug}"
    args.manifest = args.manifest or repo_root / f"sources/gigadevice/{slug}.lock.json"
    if args.minimum_count is None:
        args.minimum_count = 25 if kind is MANUAL_KIND else 60
    return args


def main() -> int:
    args = _parse_args()
    kind = args.document_kind
    if args.keyword and args.download:
        raise ValueError("关键词查询只用于审计，不能生成不完整锁文件")
    sources = discover_documents(kind, args.keyword)
    if len(sources) < args.minimum_count:
        raise ValueError(
            f"官网仅发现 {len(sources)} 个 GD32 {kind.name}，少于下限 {args.minimum_count}"
        )
    if not args.download:
        for source in sources:
            print(f"{source.document_id}\t{source.version}\t{source.name}")
        print(f"共发现 {len(sources)} 个 GD32 {kind.name}；添加 --download 后锁定。")
        return 0
    if os.environ.get("GIGADEVICE_ACCEPT_SLA_GD0001") != "1":
        raise ValueError("下载前必须设置 GIGADEVICE_ACCEPT_SLA_GD0001=1 明确接受 SLA-GD0001")
    records, history, _ = incremental_manual_records(
        sources, args.manifest, args.cache_dir, kind
    )
    write_manifest(args.manifest, records, kind, history)
    print(f"已校验 {len(records)} 个 GD32 {kind.name}：{args.manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
