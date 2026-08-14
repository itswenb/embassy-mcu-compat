#!/usr/bin/env python3
"""增量锁定并分析 IAR 官方 Arm 设备支持包。"""

from __future__ import annotations

import argparse
import configparser
import json
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

import gigadevice_sources as common
import audit_gigadevice_svds as svd_audit


RELEASE_FEED = "https://github.com/iarsystems/arm/releases.atom"
ASSET_RE = re.compile(
    r"cxarm-device-support-additional-(\d+\.\d+\.\d+)\.\d+\.tar\.bz2"
)
TEXT_SUFFIXES = {
    ".board",
    ".c",
    ".ddf",
    ".h",
    ".html",
    ".i79",
    ".inc",
    ".json",
    ".mac",
    ".menu",
    ".sfr",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


class IarSource(NamedTuple):
    name: str
    version: str
    published: str
    release_url: str
    url: str
    size: int
    sha256: str


def svd_name_for_device(device: str) -> str:
    if re.fullmatch(r"GD32A71[12][A-C][A-Z]", device):
        return "GD32A71x.svd"
    if re.fullmatch(r"GD32A714[A-C][A-Z]", device):
        return "GD32A714x.svd"
    if re.fullmatch(r"GD32A7(?:2[24]|4[124])[A-C][A-Z]", device):
        return "GD32A72_A74x.svd"
    raise ValueError(f"不支持的 IAR A7 型号：{device}")


def parse_release(data: dict[str, object]) -> IarSource:
    tag = str(data.get("tag_name", ""))
    if re.fullmatch(r"\d+\.\d+\.\d+", tag) is None:
        raise ValueError(f"IAR 发布标签格式异常：{tag!r}")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ValueError("IAR 发布缺少资产列表")
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and (match := ASSET_RE.fullmatch(str(asset.get("name", ""))))
        and match.group(1) == tag
    ]
    if len(matches) != 1:
        raise ValueError(f"IAR 发布必须有唯一 additional 设备支持包，实际为 {len(matches)} 个")

    asset = matches[0]
    name = str(asset["name"])
    url = str(asset.get("browser_download_url", ""))
    parsed = urllib.parse.urlsplit(url)
    expected_path = f"/iarsystems/arm/releases/download/{tag}/{name}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"不安全的 IAR 下载地址：{url!r}")
    size = int(asset.get("size", 0))
    if not 0 < size <= common.MAX_ARCHIVE_SIZE:
        raise ValueError(f"IAR 资产大小异常：{size}")
    digest = str(asset.get("digest", ""))
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("IAR 资产缺少有效 SHA-256")
    release_url = str(data.get("html_url", ""))
    if release_url != f"https://github.com/iarsystems/arm/releases/tag/{tag}":
        raise ValueError(f"不安全的 IAR 发布地址：{release_url!r}")
    published = str(data.get("published_at", ""))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", published) is None:
        raise ValueError(f"IAR 发布时间格式异常：{published!r}")
    return IarSource(name, tag, published, release_url, url, size, digest[7:])


def parse_public_release(atom: str, assets_html: str, size: int) -> IarSource:
    root = ET.fromstring(atom)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", namespace)
    if entry is None:
        raise ValueError("IAR 发布订阅缺少条目")
    link = entry.find("atom:link[@rel='alternate']", namespace)
    published_node = entry.find("atom:updated", namespace)
    release_url = "" if link is None else str(link.get("href", ""))
    match = re.fullmatch(
        r"https://github\.com/iarsystems/arm/releases/tag/(\d+\.\d+\.\d+)",
        release_url,
    )
    if match is None or published_node is None or published_node.text is None:
        raise ValueError("IAR 最新发布链接或时间格式异常")
    tag = match.group(1)

    candidates: list[tuple[str, str]] = []
    for item in re.findall(r"<li\b.*?</li>", assets_html, flags=re.DOTALL):
        asset = re.search(
            rf'href="(/iarsystems/arm/releases/download/{re.escape(tag)}/'
            rf'(cxarm-device-support-additional-{re.escape(tag)}\.\d+\.tar\.bz2))"',
            item,
        )
        digest = re.search(r"sha256:([0-9a-f]{64})", item)
        if asset is not None and digest is not None:
            candidates.append((asset.group(1), digest.group(1)))
    if len(candidates) != 1:
        raise ValueError(f"IAR 公开资产页必须有唯一 additional 包，实际为 {len(candidates)} 个")
    path, digest = candidates[0]
    name = path.rsplit("/", 1)[1]
    return parse_release(
        {
            "tag_name": tag,
            "published_at": published_node.text,
            "html_url": release_url,
            "assets": [
                {
                    "name": name,
                    "browser_download_url": f"https://github.com{path}",
                    "size": size,
                    "digest": f"sha256:{digest}",
                }
            ],
        }
    )


def discover() -> IarSource:
    atom = common._read_text(RELEASE_FEED)
    root = ET.fromstring(atom)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    link = root.find("atom:entry/atom:link[@rel='alternate']", namespace)
    release_url = "" if link is None else str(link.get("href", ""))
    match = re.fullmatch(
        r"https://github\.com/iarsystems/arm/releases/tag/(\d+\.\d+\.\d+)",
        release_url,
    )
    if match is None:
        raise ValueError("IAR 最新发布链接格式异常")
    assets = common._read_text(
        f"https://github.com/iarsystems/arm/releases/expanded_assets/{match.group(1)}"
    )
    partial = parse_public_release(atom, assets, 1)
    size = common._remote_size(partial.url)
    if size is None:
        raise ValueError("IAR 资产响应缺少 Content-Length")
    return parse_public_release(atom, assets, size)


def _record_data(source: IarSource) -> dict[str, object]:
    return {**source._asdict(), "status": "active"}


def _record_from_data(data: dict[str, object]) -> IarSource:
    return IarSource(*(data[field] for field in IarSource._fields))


def write_manifest(path: Path, source: IarSource) -> None:
    history: list[dict[str, object]] = []
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        raw_history = previous.get("history", [])
        if not isinstance(raw_history, list) or not all(
            isinstance(row, dict) for row in raw_history
        ):
            raise ValueError("IAR 锁文件历史格式无效")
        history = [dict(row) for row in raw_history]
        old = previous.get("iar")
        if isinstance(old, dict) and _record_from_data(old) != source:
            history.append({**old, "status": "superseded"})
        history = list(
            {
                json.dumps(row, ensure_ascii=False, sort_keys=True): row
                for row in history
            }.values()
        )
    data = {
        "schema_version": 1,
        "source_page": source.release_url,
        "license": {
            "redistribution": "IAR 原始设备支持包仅在本地分析，不随生成仓库发布。"
        },
        "iar": _record_data(source),
    }
    if history:
        data["history"] = history
    common._write_text_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def incremental_record(
    source: IarSource, manifest_path: Path, cache_dir: Path
) -> tuple[IarSource, bool]:
    if manifest_path.is_file():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        locked = data.get("iar")
        if isinstance(locked, dict):
            current = _record_from_data(locked)
            if current == source:
                return current, False
    materialize(source, cache_dir)
    return source, True


def materialize(source: IarSource, cache_dir: Path) -> Path:
    archive = cache_dir / source.name
    if not archive.is_file() or archive.stat().st_size != source.size:
        common._download(source.url, archive)
    if archive.stat().st_size != source.size:
        raise ValueError(f"IAR 资产大小不匹配：{archive}")
    actual = common._sha256(archive)
    if actual != source.sha256:
        raise ValueError(f"IAR 资产 SHA-256 不匹配：期望 {source.sha256}，实际 {actual}")
    common._verify_archive(archive)
    return archive


def extract(source: IarSource, archive: Path, extract_root: Path) -> Path:
    output = extract_root / source.sha256[:12]
    marker = output / ".source.json"
    expected = {"archive": source.name, "archive_sha256": source.sha256, "schema_version": 1}
    if marker.is_file():
        actual = json.loads(marker.read_text(encoding="utf-8"))
        if all(actual.get(key) == value for key, value in expected.items()):
            return output
        raise ValueError(f"IAR 解包缓存校验失败，请检查后删除：{output}")
    if output.exists():
        raise ValueError(f"IAR 解包目录存在但缺少来源标记，请检查后删除：{output}")
    extract_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=extract_root, prefix=".iar-extract.") as directory:
        payload = Path(directory) / "payload"
        common._extract_archive(archive, payload)
        common._write_text_atomic(
            payload / ".source.json",
            json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        payload.replace(output)
    return output


def _unique_path(root: Path, name: str) -> Path:
    matches = [path for path in root.rglob(name) if path.is_file()]
    if len(matches) != 1:
        raise ValueError(f"IAR 设备文件 {name} 必须唯一，实际为 {len(matches)} 个")
    return matches[0]


def _memory_ranges(ddf: Path) -> list[dict[str, object]]:
    ranges = []
    for name, start, end, access in re.findall(
        r"^Memory\s*=\s*(\S+)\s+Memory\s+(0x[0-9A-Fa-f]+)\s+"
        r"(0x[0-9A-Fa-f]+)\s+([RW]+)",
        ddf.read_text(encoding="utf-8", errors="strict"),
        flags=re.MULTILINE,
    ):
        ranges.append(
            {
                "name": name,
                "start": int(start, 16),
                "size": int(end, 16) - int(start, 16) + 1,
                "access": access,
            }
        )
    if not ranges:
        raise ValueError(f"IAR DDF 缺少内存范围：{ddf}")
    return ranges


def _relative_source_path(root: Path, relative: Path, label: str) -> Path:
    if ".." in relative.parts:
        raise ValueError(f"IAR {label}路径越出根目录：{relative}")
    matches = [path.resolve() for path in root.glob(f"**/{relative.as_posix()}") if path.is_file()]
    if len(matches) != 1:
        raise ValueError(f"IAR {label}文件必须唯一，实际为 {len(matches)} 个：{relative}")
    return matches[0]


def _toolkit_path(root: Path, value: str) -> Path:
    prefix = "$TOOLKIT_DIR$/"
    if not value.startswith(prefix):
        raise ValueError(f"IAR 工具链路径缺少 {prefix} 前缀：{value}")
    return _relative_source_path(root, Path("arm") / value.removeprefix(prefix), "工具链")


def _linker_memory(path: Path) -> list[dict[str, object]]:
    values: dict[str, dict[str, int]] = {}
    for name, bound, value in re.findall(
        r"^define symbol __ICFEDIT_region_(I(?:ROM|RAM)\d+)_(start|end)__\s*=\s*"
        r"(0x[0-9A-Fa-f]+)\s*;",
        path.read_text(encoding="utf-8", errors="strict"),
        flags=re.MULTILINE,
    ):
        values.setdefault(name, {})[bound] = int(value, 16)
    memory = []
    for name, bounds in values.items():
        if set(bounds) != {"start", "end"} or bounds["end"] < bounds["start"]:
            raise ValueError(f"IAR ICF 内存范围无效：{path}:{name}")
        if bounds["start"] == bounds["end"] == 0:
            continue
        memory.append(
            {
                "name": name,
                "kind": "flash" if name.startswith("IROM") else "ram",
                "start": bounds["start"],
                "size": bounds["end"] - bounds["start"] + 1,
            }
        )
    if not memory:
        raise ValueError(f"IAR ICF 缺少有效内存范围：{path}")
    return sorted(memory, key=lambda row: (int(row["start"]), str(row["name"])))


def _flash_board(root: Path, board: Path) -> dict[str, object]:
    regions = []
    for index, item in enumerate(ET.parse(board).getroot().findall("pass")):
        range_text = item.findtext("range", "").split()
        if len(range_text) != 3 or range_text[0] != "CODE":
            raise ValueError(f"IAR board Flash 范围无效：{board}")
        start, end = (int(value, 0) for value in range_text[1:])
        descriptor = _toolkit_path(root, item.findtext("loader", ""))
        flash = ET.parse(descriptor).getroot()
        block = flash.findtext("block", "").split()
        if len(block) != 2:
            raise ValueError(f"IAR Flash block 无效：{descriptor}")
        blocks, erase_size = (int(value, 0) for value in block)
        write_size = int(flash.findtext("page", "0"), 0)
        base = int(flash.findtext("flash_base", "0"), 0)
        size = end - start + 1
        if start != base or size <= 0 or blocks * erase_size != size or write_size <= 0:
            raise ValueError(f"IAR board 与 Flash 几何不一致：{descriptor}")
        algorithm = _toolkit_path(root, flash.findtext("exe", ""))
        regions.append(
            {
                "name": f"FLASH{index}",
                "start": start,
                "size": size,
                "write_size": write_size,
                "erase_size": erase_size,
                "blocks": blocks,
                "descriptor": {
                    "path": descriptor.relative_to(root.resolve()).as_posix(),
                    "sha256": common._sha256(descriptor),
                },
                "algorithm": {
                    "path": algorithm.relative_to(root.resolve()).as_posix(),
                    "sha256": common._sha256(algorithm),
                    "size": algorithm.stat().st_size,
                },
            }
        )
    if not regions:
        raise ValueError(f"IAR board 缺少 Flash 范围：{board}")
    return {
        "path": board.relative_to(root.resolve()).as_posix(),
        "sha256": common._sha256(board),
        "regions": regions,
    }


def _device_configuration(root: Path, device: str) -> dict[str, object]:
    path = _unique_path(root, f"{device}.i79")
    parser = configparser.ConfigParser(interpolation=None, comment_prefixes=("#", ";", "//"))
    parser.read_string(path.read_text(encoding="utf-8", errors="strict"))
    ddf_name = parser["DDF FILE"]["name"]
    ddf_relative = Path(ddf_name)
    ddf = _relative_source_path(root, Path("arm/config/debugger") / ddf_relative, "DDF")
    linker = _toolkit_path(root, parser["LINKER FILE"]["name"])
    memory = _linker_memory(linker)
    flash = _flash_board(root, _toolkit_path(root, parser["FLASH LOADER"]["little"]))
    if [
        (int(row["start"]), int(row["size"]))
        for row in memory
        if row["kind"] == "flash"
    ] != [(int(row["start"]), int(row["size"])) for row in flash["regions"]]:
        raise ValueError(f"IAR linker 与 Flash board 范围不一致：{device}")
    return {
        "configuration": path.relative_to(root).as_posix(),
        "ddf": ddf.relative_to(root.resolve()).as_posix(),
        "linker": {
            "path": linker.relative_to(root.resolve()).as_posix(),
            "sha256": common._sha256(linker),
            "memory": memory,
        },
        "flash": flash,
    }


def _a7_resources(root: Path, models: dict[str, object]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = models.get("devices")
    if not isinstance(rows, list):
        raise ValueError("规范型号报告缺少 devices")
    devices = sorted(str(row["id"]) for row in rows if str(row["id"]).startswith("GD32A7"))
    if len(devices) != 48:
        raise ValueError(f"IAR A7 规范型号必须为 48 个，实际为 {len(devices)} 个")

    svd_paths = {name: _unique_path(root, name) for name in {svd_name_for_device(device) for device in devices}}
    mapped: dict[str, list[str]] = {name: [] for name in svd_paths}
    output = []
    for device in devices:
        svd_name = svd_name_for_device(device)
        mapped[svd_name].append(device)
        ddf_device = device
        try:
            ddf = _unique_path(root, f"{ddf_device}.ddf")
            inherited_from = None
        except ValueError:
            ddf_device = f"{device[:-2]}A{device[-1]}"
            ddf = _unique_path(root, f"{ddf_device}.ddf")
            inherited_from = ddf_device
        ddf_text = ddf.read_text(encoding="utf-8", errors="strict")
        include = re.search(r"^File\s*=\s*(GD32A7\S+\.svd)\s*$", ddf_text, re.MULTILINE)
        if include is None or include.group(1) != svd_name:
            raise ValueError(f"IAR DDF 与 A7 SVD 映射不一致：{device}")
        configuration = _device_configuration(root, ddf_device)
        if configuration["ddf"] != ddf.relative_to(root).as_posix():
            raise ValueError(f"IAR i79 与 DDF 映射不一致：{device}")
        record = {
            "id": device,
            "core": "Cortex-M7",
            "rust_target": "thumbv7em-none-eabihf",
            "svd": svd_name,
            "ddf": ddf.relative_to(root).as_posix(),
            "memory": _memory_ranges(ddf),
            "configuration": configuration["configuration"],
            "linker": configuration["linker"],
            "flash": configuration["flash"],
        }
        if inherited_from is not None:
            record["package_variant_evidence"] = inherited_from
        output.append(record)

    svds = []
    for name, path in sorted(svd_paths.items()):
        stats = svd_audit.svd_stats(path)
        svds.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": common._sha256(path),
                "size": path.stat().st_size,
                "devices": mapped[name],
                **{
                    key: stats[key]
                    for key in ("device_name", "peripherals", "interrupts", "registers", "fields")
                },
            }
        )
    return output, svds


def analyze(source: IarSource, root: Path, models: dict[str, object]) -> dict[str, object]:
    identifiers: set[str] = set()
    matched_files: list[str] = []
    license_files: list[str] = []
    nested_archives: list[str] = []
    files = [path for path in root.rglob("*") if path.is_file() and path.name != ".source.json"]
    for path in files:
        relative = path.relative_to(root).as_posix()
        lower = path.name.casefold()
        if "license" in lower or "licence" in lower or "eula" in lower:
            license_files.append(relative)
        if lower.endswith((".tar", ".tar.gz", ".tar.bz2", ".tgz", ".zip", ".7z", ".deb")):
            nested_archives.append(relative)
        if path.suffix.casefold() not in TEXT_SUFFIXES or path.stat().st_size > 16 * 1024 * 1024:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = set(re.findall(r"(?i)\bGD32A7[A-Z0-9x]*\b", f"{relative}\n{text}"))
        if found:
            identifiers.update(name.upper() for name in found)
            matched_files.append(relative)
    devices, svds = _a7_resources(root, models)
    for svd in svds:
        svd["source_pack_name"] = "IAR Arm Device Support"
        svd["source_pack_version"] = source.version
    return {
        "schema_version": 1,
        "source": _record_data(source),
        "file_count": len(files),
        "gd32a7_identifiers": sorted(identifiers),
        "matched_files": sorted(matched_files),
        "license_files": sorted(license_files),
        "nested_archives": sorted(nested_archives),
        "devices": devices,
        "svd_files": svds,
        "summary": {
            "devices": len(devices),
            "svd_files": len(svds),
            "devices_with_register_source": len(devices),
            "devices_with_normalized_memory": len(devices),
            "devices_with_normalized_flash": len(devices),
        },
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--download", action="store_true")
    action.add_argument("--locked", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=repo_root / ".cache/research/gigadevice/iar")
    parser.add_argument(
        "--extract-root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/iar-device-support-v1",
    )
    parser.add_argument("--manifest", type=Path, default=repo_root / "sources/gigadevice/iar.lock.json")
    parser.add_argument("--report", type=Path, default=repo_root / "reports/gigadevice-iar-a7.json")
    parser.add_argument("--models", type=Path, default=repo_root / "reports/gigadevice-models.json")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.locked:
        locked = json.loads(args.manifest.read_text(encoding="utf-8")).get("iar")
        if not isinstance(locked, dict):
            raise ValueError("IAR 锁文件缺少 iar")
        source = _record_from_data(locked)
    else:
        source = discover()
    if not args.download and not args.locked:
        print(f"{source.version}\t{source.published}\t{source.name}")
        print("添加 --download 后下载、解包并分析。")
        return 0
    record, changed = incremental_record(source, args.manifest, args.cache_dir)
    archive = args.cache_dir / record.name if changed else materialize(record, args.cache_dir)
    root = extract(record, archive, args.extract_root)
    models = json.loads(args.models.read_text(encoding="utf-8"))
    report = analyze(record, root, models)
    write_manifest(args.manifest, record)
    common._write_text_atomic(
        args.report, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(f"已{'更新' if changed else '复用'} IAR {record.version}；发现 {len(report['gd32a7_identifiers'])} 个 A7 标识")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
