#!/usr/bin/env python3
"""把 Builder 固件插件适配成现有 Firmware 提取流水线的只读输入。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import gigadevice_builder
import gigadevice_sources as common


VERSION_RE = re.compile(r"_(\d+\.\d+\.\d+)\.\d+$")


def build_adapter(
    report: dict[str, object], source_root: Path, output: Path
) -> dict[str, object]:
    provenance = report["provenance"]
    plugins = report["plugins"]
    if not isinstance(provenance, dict) or not isinstance(plugins, list):
        raise ValueError("Builder 固件索引结构无效")
    archive_sha256 = str(provenance["builder_archive_sha256"])
    tree_sha256 = str(provenance["builder_tree_sha256"])
    libraries_root = output / "libraries"
    libraries_root.mkdir(parents=True, exist_ok=True)
    lock_items = []
    header_libraries = []
    for plugin in plugins:
        if not isinstance(plugin, dict):
            raise ValueError("Builder 固件插件记录无效")
        plugin_id = str(plugin["id"])
        version_match = VERSION_RE.search(plugin_id)
        if version_match is None:
            raise ValueError(f"Builder 固件插件版本无法解析：{plugin_id}")
        version = version_match.group(1)
        series = str(plugin["series"]).upper()
        filename = f"{series}_Firmware_Library_V{version}.7z"
        link = libraries_root / filename.removesuffix(".7z")
        target = (source_root / "plugins" / plugin_id).resolve()
        if not target.is_dir():
            raise ValueError(f"Builder 固件插件目录不存在：{target}")
        if link.is_symlink():
            if link.resolve() != target:
                raise ValueError(f"Builder 固件适配链接目标冲突：{link}")
        elif link.exists():
            raise ValueError(f"Builder 固件适配路径不是符号链接：{link}")
        else:
            link.symlink_to(target, target_is_directory=True)

        raw_headers = plugin["device_headers"]
        if not isinstance(raw_headers, list) or len(raw_headers) != 1:
            raise ValueError(f"Builder 固件插件必须恰有一个器件头：{plugin_id}")
        raw_header = raw_headers[0]
        assert isinstance(raw_header, dict)
        prefix = f"plugins/{plugin_id}/"
        source_path = str(raw_header["path"])
        if not source_path.startswith(prefix):
            raise ValueError(f"Builder 器件头不属于插件：{source_path}")
        relative = source_path.removeprefix(prefix)
        if common._sha256(target / relative) != str(raw_header["sha256"]):
            raise ValueError(f"Builder 器件头哈希不一致：{source_path}")
        lock_items.append(
            {
                "filename": filename,
                "version": version,
                "sha256": archive_sha256,
                "source": "gd32-embedded-builder",
            }
        )
        header_libraries.append(
            {
                "series": series,
                "version": version,
                "archive_sha256": archive_sha256,
                "tree_sha256": tree_sha256,
                "device_headers": [
                    {"path": relative, "sha256": str(raw_header["sha256"])}
                ],
            }
        )
    if len({item["filename"] for item in lock_items}) != len(lock_items):
        raise ValueError("Builder 固件适配后系列重复")
    adapter = {
        "schema_version": 1,
        "lock": {"schema_version": 1, "firmware": lock_items},
        "headers": {"schema_version": 1, "libraries": header_libraries},
        "root": str(libraries_root),
        "provenance": provenance,
    }
    for name, value in (("lock.json", adapter["lock"]), ("headers.json", adapter["headers"]), ("manifest.json", adapter)):
        common._write_text_atomic(
            output / name,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    return adapter


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--builder-lock",
        type=Path,
        default=repo_root / "sources/gigadevice/builder.lock.json",
    )
    parser.add_argument(
        "--builder-report",
        type=Path,
        default=repo_root / "reports/gigadevice-builder-firmware.json",
    )
    parser.add_argument(
        "--builder-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/builder-firmware-v1",
    )
    parser.add_argument(
        "--output-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/builder-firmware-adapter-v1",
    )
    parser.add_argument(
        "--headers-output",
        type=Path,
        default=repo_root / "reports/gigadevice-builder-headers.json",
    )
    parser.add_argument("--minimum-plugins", type=int, default=36)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    lock = json.loads(args.builder_lock.read_text(encoding="utf-8"))
    archive_sha256 = str(lock["builder"]["sha256"])
    source_root = gigadevice_builder.find_extracted_root(
        args.builder_cache, archive_sha256
    )
    report = json.loads(args.builder_report.read_text(encoding="utf-8"))
    output = args.output_cache / archive_sha256[:12]
    adapter = build_adapter(report, source_root, output)
    common._write_text_atomic(
        args.headers_output,
        json.dumps(adapter["headers"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    count = len(adapter["lock"]["firmware"])
    if count < args.minimum_plugins:
        raise ValueError("Builder 固件适配数量低于覆盖门限")
    print(f"Builder 固件适配：{output}（插件={count}）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
