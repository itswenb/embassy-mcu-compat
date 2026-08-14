#!/usr/bin/env python3
"""发现、下载并校验 GigaDevice 官方固件库。"""

from __future__ import annotations

import argparse
import hashlib
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
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import NamedTuple


BASE_URL = "https://www.gd32mcu.com"
SOURCE_PAGE = f"{BASE_URL}/cn/download/7?kw=Firmware"
AGREEMENT_URL = f"{BASE_URL}/download/agree/box_id/12/document_id/{{document_id}}/path_type/1"
DOWNLOAD_URL = f"{BASE_URL}/download/down/document_id/{{document_id}}/path_type/1"
USER_AGENT = "embassy-mcu-compat-source-sync/1"
MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024


class FirmwareSource(NamedTuple):
    name: str
    version: str
    document_id: int
    published: str


class DownloadEntry(NamedTuple):
    name: str
    version: str
    box_id: int
    document_id: int
    published: str


class FirmwareRecord(NamedTuple):
    source: FirmwareSource
    url: str
    filename: str
    size: int
    sha256: str
    status: str = "active"


class _DownloadPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[DownloadEntry] = []
        self.page_count = 1
        self._entry: dict[str, str] | None = None
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
                self._entry = {}
                self._entry_depth = self._open_li_depth

        if tag == "a":
            href = values.get("href", "")
            page = re.search(r"/p/(\d+)(?:\?|$)", href)
            if page:
                self.page_count = max(self.page_count, int(page.group(1)))
            if self._entry is not None and "agree_box" in classes:
                match = re.fullmatch(
                    r"/download/agree/box_id/(\d+)/document_id/(\d+)/path_type/1", href
                )
                if match:
                    self._entry["box_id"] = match.group(1)
                    self._entry["document_id"] = match.group(2)

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

        entry = self._entry
        self._entry = None
        self._open_li_depth = max(0, self._open_li_depth - 1)
        required = {"name", "version", "box_id", "document_id", "published"}
        if required <= entry.keys():
            self.entries.append(
                DownloadEntry(
                    name=" ".join(entry["name"].split()),
                    version=entry["version"],
                    box_id=int(entry["box_id"]),
                    document_id=int(entry["document_id"]),
                    published=entry["published"],
                )
            )


def parse_download_page(html: str) -> tuple[list[DownloadEntry], int]:
    parser = _DownloadPageParser()
    parser.feed(html)
    parser.close()
    return parser.entries, parser.page_count


def parse_firmware_page(html: str) -> tuple[list[FirmwareSource], int]:
    entries, page_count = parse_download_page(html)
    firmware = []
    for entry in entries:
        normalized_name = " ".join(entry.name.replace("_", " ").split())
        if entry.box_id != 12 or "Firmware Library" not in normalized_name:
            continue
        if not re.fullmatch(r"\d+\.\d+\.\d+", entry.version):
            raise ValueError(f"固件库版本格式异常：{entry.version!r}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.published):
            raise ValueError(f"固件库发布日期格式异常：{entry.published!r}")
        firmware.append(
            FirmwareSource(
                name=normalized_name,
                version=entry.version,
                document_id=entry.document_id,
                published=entry.published,
            )
        )
    return firmware, page_count


def merge_entries(*pages: list[FirmwareSource]) -> list[FirmwareSource]:
    by_document: dict[int, FirmwareSource] = {}
    by_name: dict[str, FirmwareSource] = {}
    for entry in (item for page in pages for item in page):
        if entry.document_id in by_document:
            raise ValueError(f"重复的 document_id：{entry.document_id}")
        if entry.name in by_name:
            raise ValueError(f"重复的固件库名称：{entry.name}")
        by_document[entry.document_id] = entry
        by_name[entry.name] = entry
    return sorted(by_document.values(), key=lambda item: item.name.casefold())


def _request(url: str, *, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method=method)


def _read_text(url: str) -> str:
    with urllib.request.urlopen(_request(url), timeout=60) as response:
        return response.read().decode("utf-8")


def discover_firmware(keyword: str = "Firmware") -> list[FirmwareSource]:
    encoded = urllib.parse.quote(keyword)
    first_url = f"{BASE_URL}/cn/download/7?kw={encoded}"
    first_entries, page_count = parse_firmware_page(_read_text(first_url))
    pages = [first_entries]
    for page in range(2, page_count + 1):
        url = f"{BASE_URL}/cn/download/7/p/{page}?kw={encoded}"
        entries, discovered_count = parse_firmware_page(_read_text(url))
        if discovered_count != page_count:
            raise ValueError("固件列表翻页信息在页面之间不一致")
        pages.append(entries)
    return merge_entries(*pages)


def _source_index(
    records: list[dict[str, object]], *, label: str
) -> dict[str, dict[str, object]]:
    indexed = {}
    for record in records:
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{label}包含无效来源逻辑标识")
        if name in indexed:
            raise ValueError(f"重复的来源逻辑标识：{name}")
        indexed[name] = record
    return indexed


def plan_source_updates(
    current: list[dict[str, object]],
    discovered: list[dict[str, object]],
    *,
    compare_fields: tuple[str, ...] = ("version", "document_id", "published"),
) -> dict[str, list[str]]:
    """只比较官网轻量元数据，不访问每个归档下载地址。"""
    old = _source_index(current, label="当前锁")
    new = _source_index(discovered, label="官网发现结果")
    plan = {key: [] for key in ("unchanged", "added", "updated", "withdrawn")}
    for name, record in new.items():
        previous = old.get(name)
        if previous is None:
            plan["added"].append(name)
        elif previous.get("status", "active") != "active" or any(
            previous.get(field) != record.get(field) for field in compare_fields
        ):
            plan["updated"].append(name)
        else:
            plan["unchanged"].append(name)
    plan["withdrawn"] = [name for name in old if name not in new]
    return {key: sorted(names, key=str.casefold) for key, names in plan.items()}


def merge_source_updates(
    current: list[dict[str, object]],
    materialized: list[dict[str, object]],
    plan: dict[str, list[str]],
    history: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """合并已物化变化；下架只标记，旧版本只追加到历史。"""
    old = _source_index(current, label="当前锁")
    fresh = _source_index(materialized, label="物化结果")
    changed = set(plan["added"]) | set(plan["updated"])
    if missing := changed - fresh.keys():
        raise ValueError(f"缺少已物化来源：{', '.join(sorted(missing))}")

    merged = []
    for name in set(old) | changed:
        record = dict(fresh[name] if name in changed else old[name])
        record["status"] = "withdrawn" if name in plan["withdrawn"] else "active"
        merged.append(record)

    combined_history = [dict(record) for record in history]
    known = {json.dumps(record, ensure_ascii=False, sort_keys=True) for record in combined_history}
    for name in plan["updated"]:
        if name not in old:
            continue
        record = dict(old[name])
        record["status"] = "superseded"
        key = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if key not in known:
            combined_history.append(record)
            known.add(key)

    return (
        sorted(merged, key=lambda record: str(record["name"]).casefold()),
        sorted(
            combined_history,
            key=lambda record: (
                str(record["name"]).casefold(),
                str(record.get("version", "")),
                int(record.get("document_id", 0)),
            ),
        ),
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def validate_download_url(location: str) -> str:
    url = urllib.parse.urljoin(BASE_URL, location)
    parsed = urllib.parse.urlsplit(url)
    decoded_path = urllib.parse.unquote(parsed.path)
    parts = PurePosixPath(decoded_path).parts
    filename = PurePosixPath(decoded_path).name
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.gd32mcu.com"
        or parsed.query
        or parsed.fragment
        or not decoded_path.startswith("/data/documents/toolSoftware/")
        or ".." in parts
        or not re.fullmatch(r"[A-Za-z0-9._()-]+\.7z", filename)
    ):
        raise ValueError(f"不安全的固件下载地址：{location!r}")
    return urllib.parse.urlunsplit(("https", parsed.netloc, decoded_path, "", ""))


def resolve_download_url(source: FirmwareSource) -> str:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(_request(DOWNLOAD_URL.format(document_id=source.document_id)), timeout=60)
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        if not location:
            raise ValueError(f"文档 {source.document_id} 的下载响应缺少 Location") from error
        return validate_download_url(location)
    raise ValueError(f"文档 {source.document_id} 未返回预期重定向")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == ".source.json" or path.is_dir():
            continue
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"目录树包含不支持的文件：{path}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _remote_size(url: str) -> int | None:
    with urllib.request.urlopen(_request(url, method="HEAD"), timeout=60) as response:
        value = response.headers.get("Content-Length")
    return int(value) if value is not None else None


def _download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
        temporary = Path(file.name)
        try:
            with urllib.request.urlopen(_request(url), timeout=120) as response:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_SIZE:
                        raise ValueError(f"固件归档超过 {MAX_ARCHIVE_SIZE} 字节上限")
                    file.write(chunk)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def validate_archive_members(members: list[str]) -> None:
    if not members:
        raise ValueError("归档为空")
    for member in members:
        stripped = member.rstrip("/")
        path = PurePosixPath(stripped)
        if (
            not stripped
            or "\\" in member
            or "\x00" in member
            or path.is_absolute()
            or ".." in path.parts
            or re.match(r"^[A-Za-z]:", stripped)
            or str(path) != stripped
        ):
            raise ValueError(f"归档包含不安全路径：{member!r}")


def declared_sha256(documents: list[bytes]) -> str:
    digests = {
        match.group().decode("ascii").lower()
        for document in documents
        for match in re.finditer(rb"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])", document)
    }
    if len(digests) != 1:
        raise ValueError(f"供应商校验文本必须包含唯一 SHA-256，实际为 {len(digests)} 个")
    return digests.pop()


def _archive_members(path: Path) -> list[str]:
    result = subprocess.run(
        ["bsdtar", "-tf", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    members = result.stdout.splitlines()
    validate_archive_members(members)
    return members


def parse_7zip_members(listing: str) -> list[str]:
    members: list[str] = []
    for block in re.split(r"\n\s*\n", listing):
        fields = {
            key.strip(): value.strip()
            for line in block.splitlines()
            if " = " in line
            for key, _, value in [line.partition(" = ")]
        }
        if "Folder" not in fields:
            continue
        if "Symbolic Link" in fields or "Hard Link" in fields:
            raise ValueError(f"归档包含链接：{fields.get('Path', '<未知>')!r}")
        member = fields.get("Path")
        if not member:
            raise ValueError("7zip 清单成员缺少路径")
        members.append(member)
    validate_archive_members(members)
    return members


def _sevenzip() -> str | None:
    return shutil.which("7z")


def _verify_archive(path: Path) -> str:
    sevenzip = _sevenzip() if path.suffix.lower() == ".7z" else None
    if sevenzip is not None:
        listing = subprocess.run(
            [sevenzip, "l", "-slt", "--", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        parse_7zip_members(listing.stdout)
        subprocess.run(
            [sevenzip, "t", "--", str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        return sevenzip

    _archive_members(path)
    listing = subprocess.run(
        ["bsdtar", "-tvf", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    unsupported = [line for line in listing.stdout.splitlines() if line and line[0] not in {"-", "d"}]
    if unsupported:
        raise ValueError(f"归档包含链接或特殊文件：{unsupported[0]!r}")
    return "bsdtar"


def _extract_archive(archive: Path, output: Path) -> None:
    tool = _verify_archive(archive)
    output.mkdir(parents=True)
    command = (
        [tool, "x", "-y", f"-o{output}", "--", str(archive)]
        if tool != "bsdtar"
        else [tool, "-xf", str(archive), "-C", str(output), "--no-same-owner"]
    )
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    for path in output.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"解包结果包含链接或特殊文件：{archive.name}")


def select_payload_archive(outer: Path, inner_archives: list[Path]) -> Path:
    if not inner_archives:
        return outer
    if len(inner_archives) == 1:
        return inner_archives[0]
    raise ValueError(f"{outer.name} 包含多个内层 7z：{len(inner_archives)}")


def extract_verified_inner(archive: Path, workspace: Path) -> Path:
    outer = workspace / "outer"
    _extract_archive(archive, outer)
    inner_archives = [path for path in outer.rglob("*.7z") if path.is_file()]
    payload_archive = select_payload_archive(archive, inner_archives)
    if payload_archive == archive:
        return archive
    checksum_files = [path.read_bytes() for path in outer.rglob("*.txt") if path.is_file()]
    expected_inner_sha256 = declared_sha256(checksum_files)
    actual_inner_sha256 = _sha256(payload_archive)
    if actual_inner_sha256 != expected_inner_sha256:
        raise ValueError(
            f"{archive.name} 内层归档 SHA-256 不匹配："
            f"期望 {expected_inner_sha256}，实际 {actual_inner_sha256}"
        )
    return payload_archive


def extract_firmware(record: FirmwareRecord, archive: Path, extract_root: Path) -> Path:
    output = extract_root / PurePosixPath(record.filename).stem
    marker = output / ".source.json"
    expected_marker = {
        "archive": record.filename,
        "archive_sha256": record.sha256,
        "schema_version": 1,
    }
    if marker.is_file():
        actual = json.loads(marker.read_text(encoding="utf-8"))
        if all(actual.get(key) == value for key, value in expected_marker.items()):
            actual_tree_sha256 = tree_sha256(output)
            recorded_tree_sha256 = actual.get("tree_sha256")
            if recorded_tree_sha256 is None:
                actual["tree_sha256"] = actual_tree_sha256
                _write_text_atomic(
                    marker, json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                )
                return output
            if recorded_tree_sha256 == actual_tree_sha256:
                return output
        raise ValueError(f"固件解包缓存校验失败，请检查后删除：{output}")
    if output.exists():
        raise ValueError(f"固件解包目录存在但缺少来源标记，请检查后删除：{output}")

    extract_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=extract_root, prefix=".firmware-extract.") as directory:
        temporary = Path(directory)
        inner_archive = extract_verified_inner(archive, temporary)
        payload = temporary / "payload"
        _extract_archive(inner_archive, payload)
        expected_marker["tree_sha256"] = tree_sha256(payload)
        marker_text = json.dumps(expected_marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _write_text_atomic(payload / ".source.json", marker_text)
        payload.replace(output)
    return output


def materialize(source: FirmwareSource, cache_dir: Path) -> FirmwareRecord:
    url = resolve_download_url(source)
    filename = PurePosixPath(urllib.parse.urlsplit(url).path).name
    path = cache_dir / filename
    expected_size = _remote_size(url)
    if not path.is_file() or (expected_size is not None and path.stat().st_size != expected_size):
        _download(url, path)
    size = path.stat().st_size
    if expected_size is not None and size != expected_size:
        raise ValueError(f"{filename} 长度不匹配：期望 {expected_size}，实际 {size}")
    _verify_archive(path)
    return FirmwareRecord(source, url, filename, size, _sha256(path))


def _firmware_record_data(record: FirmwareRecord) -> dict[str, object]:
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


def _firmware_record_from_data(record: dict[str, object]) -> FirmwareRecord:
    return FirmwareRecord(
        FirmwareSource(
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


def incremental_firmware_records(
    sources: list[FirmwareSource], manifest_path: Path, cache_dir: Path
) -> tuple[list[FirmwareRecord], list[dict[str, object]], dict[str, list[str]]]:
    locked: dict[str, object] = {}
    if manifest_path.is_file():
        locked = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = locked.get("firmware", [])
    history = locked.get("history", [])
    if not isinstance(current, list) or not isinstance(history, list):
        raise ValueError("固件锁文件格式无效")
    discovered = [source._asdict() for source in sources]
    plan = plan_source_updates(current, discovered)
    changed = set(plan["added"]) | set(plan["updated"])
    materialized = [
        _firmware_record_data(materialize(source, cache_dir))
        for source in sources
        if source.name in changed
    ]
    merged, merged_history = merge_source_updates(current, materialized, plan, history)
    return [_firmware_record_from_data(record) for record in merged], merged_history, plan


def write_manifest(
    path: Path,
    records: list[FirmwareRecord],
    history: list[dict[str, object]] | None = None,
) -> None:
    firmware = []
    for record in sorted(records, key=lambda item: item.source.name.casefold()):
        firmware.append(_firmware_record_data(record))
    manifest = {
        "schema_version": 1,
        "source_pages": [
            SOURCE_PAGE,
            f"{BASE_URL}/cn/download/7/p/2?kw=Firmware",
        ],
        "license": {
            "agreement": "SLA-GD0001-version1.1",
            "redistribution": "原始归档仅作来源缓存；公开派生数据必须逐文件确认许可。",
        },
        "firmware": firmware,
    }
    if history:
        manifest["history"] = history
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_text_atomic(path, text)


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    default_cache = repo_root / ".cache/research/gigadevice/official-firmware"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=default_cache)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/firmware-manifest.json",
    )
    parser.add_argument("--minimum-count", type=int, default=33)
    parser.add_argument("--keyword", default="Firmware")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--extract-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.download and args.keyword != "Firmware":
        raise ValueError("局部关键词查询不能与 --download 同时使用")
    sources = discover_firmware(args.keyword)
    if len(sources) < args.minimum_count:
        raise ValueError(f"官网仅发现 {len(sources)} 个固件库，少于下限 {args.minimum_count}")
    if not args.download:
        for source in sources:
            print(f"{source.document_id}\t{source.version}\t{source.name}")
        print(f"共发现 {len(sources)} 个固件库；添加 --download 后下载并生成锁定清单。")
        return 0
    if os.environ.get("GIGADEVICE_ACCEPT_SLA_GD0001") != "1":
        raise ValueError("下载前必须设置 GIGADEVICE_ACCEPT_SLA_GD0001=1 明确接受 SLA-GD0001")
    records, history, _ = incremental_firmware_records(sources, args.manifest, args.cache_dir)
    if args.extract_dir is not None:
        for record in records:
            extract_firmware(record, args.cache_dir / record.filename, args.extract_dir)
    write_manifest(args.manifest, records, history)
    print(f"已校验 {len(records)} 个固件库：{args.manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, urllib.error.URLError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
