#!/usr/bin/env python3
"""锁定 GigaDevice 选型手册并派生公开的型号事实索引。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import gigadevice_sources as common


SOURCE_PAGE = f"{common.BASE_URL}/cn/download/9?kw=Selection"
DOWNLOAD_URL = f"{common.BASE_URL}/download/down/document_id/{{document_id}}/path_type/{{path_type}}"


class CatalogSource(NamedTuple):
    version: str
    document_id: int
    path_types: tuple[int, ...]
    published: str


class CatalogDocument(NamedTuple):
    language: str
    url: str
    filename: str
    size: int
    sha256: str


class _CatalogParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict[str, object]] = []
        self._entry: dict[str, object] | None = None
        self._li_depth = 0
        self._entry_depth = 0
        self._capture: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "li":
            self._li_depth += 1
            if self._entry is None and "cl" in classes:
                self._entry = {"downloads": []}
                self._entry_depth = self._li_depth
        if tag == "a" and self._entry is not None:
            match = re.fullmatch(
                r"/download/down/document_id/(\d+)/path_type/(\d+)", values.get("href", "")
            )
            if match:
                downloads = self._entry["downloads"]
                assert isinstance(downloads, list)
                downloads.append((int(match.group(1)), int(match.group(2))))
        fields = {"data-name": "name", "data-version": "version", "data-time": "published"}
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
        if self._entry is not None and self._li_depth == self._entry_depth:
            self.entries.append(self._entry)
            self._entry = None
        self._li_depth = max(0, self._li_depth - 1)


def parse_catalog_page(html: str) -> CatalogSource:
    parser = _CatalogParser()
    parser.feed(html)
    parser.close()
    entries = [entry for entry in parser.entries if entry.get("name") == "GD32 MCU Selection Guide"]
    if len(entries) != 1:
        raise ValueError(f"官网必须提供唯一 GD32 MCU Selection Guide，实际为 {len(entries)} 个")
    entry = entries[0]
    downloads = entry["downloads"]
    assert isinstance(downloads, list)
    document_ids = {document_id for document_id, _ in downloads}
    path_types = tuple(sorted({path_type for _, path_type in downloads}))
    if len(document_ids) != 1 or path_types != (1, 2):
        raise ValueError("选型手册必须提供同一文档号的中英文版本")
    version = str(entry.get("version", ""))
    published = str(entry.get("published", ""))
    if not re.fullmatch(r"\d{4}", version) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
        raise ValueError("选型手册版本或发布日期格式异常")
    return CatalogSource(version, document_ids.pop(), path_types, published)


def discover_catalog() -> CatalogSource:
    return parse_catalog_page(common._read_text(SOURCE_PAGE))


def validate_pdf_url(location: str) -> str:
    url = urllib.parse.urljoin(common.BASE_URL, location)
    parsed = urllib.parse.urlsplit(url)
    decoded_path = urllib.parse.unquote(parsed.path)
    filename = PurePosixPath(decoded_path).name
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.gd32mcu.com"
        or parsed.query
        or parsed.fragment
        or not decoded_path.startswith("/data/documents/otherDocument/")
        or ".." in PurePosixPath(decoded_path).parts
        or not re.fullmatch(r"[A-Za-z0-9+._() -]+\.pdf", filename)
    ):
        raise ValueError(f"不安全的选型手册地址：{location!r}")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def resolve_document_url(source: CatalogSource, path_type: int) -> str:
    opener = urllib.request.build_opener(common._NoRedirect)
    url = DOWNLOAD_URL.format(document_id=source.document_id, path_type=path_type)
    try:
        opener.open(common._request(url), timeout=60)
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        if not location:
            raise ValueError("选型手册下载响应缺少 Location") from error
        return validate_pdf_url(location)
    raise ValueError("选型手册下载接口未返回预期重定向")


def materialize(source: CatalogSource, path_type: int, cache_dir: Path) -> CatalogDocument:
    language = {1: "en", 2: "zh"}[path_type]
    url = resolve_document_url(source, path_type)
    remote_name = PurePosixPath(urllib.parse.urlsplit(url).path).name
    filename = urllib.parse.unquote(remote_name).replace("+", "_").replace(" ", "_")
    path = cache_dir / filename
    expected_size = common._remote_size(url)
    if not path.is_file() or (expected_size is not None and path.stat().st_size != expected_size):
        common._download(url, path)
    size = path.stat().st_size
    if expected_size is not None and size != expected_size:
        raise ValueError(f"{filename} 长度不匹配：期望 {expected_size}，实际 {size}")
    if not path.read_bytes()[:5] == b"%PDF-":
        raise ValueError(f"{filename} 不是 PDF")
    return CatalogDocument(language, url, filename, size, common._sha256(path))


def _pdf_text(path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", "-nopgbrk", str(path), "-"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def extract_model_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"(?<![A-Za-z0-9])GD32[A-Za-z0-9]{3,}(?![A-Za-z0-9])", text))
    return {token for token in tokens if token.upper() not in {"GD32MCU", "GD32MCUS"}}


def _catalog_record_data(
    source: CatalogSource,
    documents: list[CatalogDocument],
    status: str = "active",
) -> dict[str, object]:
    return {
        "name": "GD32 MCU Selection Guide",
        "version": source.version,
        "published": source.published,
        "document_id": source.document_id,
        "available_path_types": list(source.path_types),
        "documents": {
            document.language: {
                "url": document.url,
                "filename": document.filename,
                "size": document.size,
                "sha256": document.sha256,
            }
            for document in sorted(documents)
        },
        "status": status,
    }


def _catalog_from_data(
    record: dict[str, object],
) -> tuple[CatalogSource, list[CatalogDocument]]:
    path_types = record["available_path_types"]
    documents = record["documents"]
    if not isinstance(path_types, list) or not isinstance(documents, dict):
        raise ValueError("选型手册锁文件格式无效")
    source = CatalogSource(
        str(record["version"]),
        int(record["document_id"]),
        tuple(int(value) for value in path_types),
        str(record["published"]),
    )
    result = []
    for language, value in sorted(documents.items()):
        if not isinstance(value, dict):
            raise ValueError("选型手册文档锁定记录格式无效")
        result.append(
            CatalogDocument(
                str(language),
                str(value["url"]),
                str(value["filename"]),
                int(value["size"]),
                str(value["sha256"]),
            )
        )
    return source, result


def incremental_catalog_documents(
    source: CatalogSource, manifest_path: Path, cache_dir: Path
) -> tuple[
    CatalogSource,
    list[CatalogDocument],
    list[dict[str, object]],
    dict[str, list[str]],
]:
    locked: dict[str, object] = {}
    if manifest_path.is_file():
        locked = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_record = locked.get("catalog")
    current = []
    if isinstance(current_record, dict):
        path_types = current_record.get("available_path_types")
        if path_types is None:
            documents = current_record.get("documents", {})
            if not isinstance(documents, dict):
                raise ValueError("选型手册锁文件格式无效")
            path_types = [value for key, value in (("en", 1), ("zh", 2)) if key in documents]
        current = [{
            "name": "GD32 MCU Selection Guide",
            "available_path_types": path_types,
            **current_record,
        }]
    elif current_record is not None:
        raise ValueError("选型手册锁文件格式无效")
    history = locked.get("history", [])
    if not isinstance(history, list):
        raise ValueError("选型手册历史记录格式无效")
    discovered = [{
        "name": "GD32 MCU Selection Guide",
        "version": source.version,
        "document_id": source.document_id,
        "published": source.published,
        "available_path_types": list(source.path_types),
    }]
    plan = common.plan_source_updates(
        current,
        discovered,
        compare_fields=("version", "document_id", "published", "available_path_types"),
    )
    changed = plan["added"] or plan["updated"]
    materialized = []
    if changed:
        documents = [materialize(source, path_type, cache_dir) for path_type in source.path_types]
        materialized.append(_catalog_record_data(source, documents))
    merged, merged_history = common.merge_source_updates(
        current, materialized, plan, history
    )
    merged_source, merged_documents = _catalog_from_data(merged[0])
    return merged_source, merged_documents, merged_history, plan


def write_manifest(
    path: Path,
    source: CatalogSource,
    documents: list[CatalogDocument],
    history: list[dict[str, object]] | None = None,
) -> None:
    data = {
        "schema_version": 1,
        "source_page": SOURCE_PAGE,
        "catalog": _catalog_record_data(source, documents),
        "redistribution": "原始 PDF 仅作来源缓存；仓库只保存哈希与事实型型号索引。",
    }
    if history:
        data["history"] = history
    common._write_text_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def write_catalog(path: Path, en: set[str], zh: set[str]) -> None:
    data = {
        "schema_version": 1,
        "en": sorted(en),
        "zh": sorted(zh),
        "all": sorted(en | zh),
        "only_en": sorted(en - zh),
        "only_zh": sorted(zh - en),
    }
    common._write_text_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/catalog",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo_root / "sources/gigadevice/catalog.lock.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-catalog.json",
    )
    parser.add_argument("--download", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source = discover_catalog()
    if not args.download:
        print(f"{source.document_id}\t{source.version}\t{source.published}\tGD32 MCU Selection Guide")
        print("添加 --download 后下载中英文版本并生成型号索引。")
        return 0
    source, documents, history, _ = incremental_catalog_documents(
        source, args.manifest, args.cache_dir
    )
    write_manifest(args.manifest, source, documents, history)
    tokens = {
        document.language: extract_model_tokens(_pdf_text(args.cache_dir / document.filename))
        for document in documents
    }
    write_catalog(args.output, tokens["en"], tokens["zh"])
    print(f"已锁定选型手册并提取 {len(tokens['en'] | tokens['zh'])} 个型号标记：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, urllib.error.URLError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
