#!/usr/bin/env python3
"""把已锁定的 GD32 技术文档确定性转换为可解析文本。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import gigadevice_sources as common


CONVERSION_VERSION = 1


def pdftotext_identity(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "-v"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    first = result.stdout.splitlines()
    if not first:
        raise ValueError("pdftotext 未返回版本信息")
    return first[0]


def convert_pdf(binary: Path, source: Path, output: Path) -> None:
    subprocess.run(
        [
            str(binary),
            "-layout",
            "-enc",
            "UTF-8",
            "-eol",
            "unix",
            str(source),
            str(output),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _page_count(text: str) -> int:
    return text.count("\f") + (1 if text and not text.endswith("\f") else 0)


def build_report(
    lock: dict[str, object],
    cache_dir: Path,
    text_dir: Path,
    binary: Path,
    identity: str,
    collection_key: str = "manuals",
) -> dict[str, object]:
    documents = lock.get(collection_key)
    if lock.get("schema_version") != 1 or not isinstance(documents, list):
        raise ValueError(f"技术文档锁文件缺少 {collection_key} 列表")
    tool_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    rows = []
    for document in sorted(documents, key=lambda row: str(row["name"]).casefold()):
        if not isinstance(document, dict):
            raise ValueError("技术文档锁文件包含非法条目")
        source = cache_dir / str(document["filename"])
        expected_sha256 = str(document["sha256"])
        if not source.is_file() or common._sha256(source) != expected_sha256:
            raise ValueError(f"技术文档缓存缺失或哈希不匹配：{source}")
        output = (
            text_dir
            / f"v{CONVERSION_VERSION}-{expected_sha256[:16]}-{tool_key}.txt"
        )
        if not output.is_file():
            text_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                dir=text_dir, prefix=".manual-text."
            ) as directory:
                temporary = Path(directory) / "manual.txt"
                convert_pdf(binary, source, temporary)
                if not temporary.is_file():
                    raise ValueError(f"pdftotext 未生成输出：{source.name}")
                temporary.replace(output)
        text = output.read_text(encoding="utf-8")
        if not text.strip() or "\x00" in text:
            raise ValueError(f"用户手册文本为空或含 NUL：{source.name}")
        rows.append(
            {
                "name": document["name"],
                "version": document["version"],
                "document_id": document["document_id"],
                "selected_path_type": document["selected_path_type"],
                "pdf": {
                    "filename": document["filename"],
                    "sha256": expected_sha256,
                },
                "text_cache": output.name,
                "text_sha256": common._sha256(output),
                "pages": _page_count(text),
                "characters": len(text),
            }
        )
    return {
        "schema_version": 1,
        "conversion_version": CONVERSION_VERSION,
        "pdftotext": identity,
        "summary": {
            collection_key: len(rows),
            "pages": sum(int(row["pages"]) for row in rows),
            "characters": sum(int(row["characters"]) for row in rows),
        },
        collection_key: rows,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    default_binary = shutil.which("pdftotext")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("manual", "datasheet"), default="manual")
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--text-dir", type=Path)
    parser.add_argument(
        "--pdftotext",
        type=Path,
        default=Path(default_binary) if default_binary else None,
        required=default_binary is None,
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--minimum-count", type=int)
    args = parser.parse_args()
    collection_key = "manuals" if args.kind == "manual" else "datasheets"
    prefix = "manual" if args.kind == "manual" else "datasheet"
    args.collection_key = collection_key
    args.lock = args.lock or repo_root / f"sources/gigadevice/{collection_key}.lock.json"
    args.cache_dir = args.cache_dir or repo_root / f".cache/research/gigadevice/{collection_key}"
    args.text_dir = args.text_dir or repo_root / f".cache/research/gigadevice/{prefix}-text-v1"
    args.output = args.output or repo_root / f"reports/gigadevice-{collection_key}.json"
    args.minimum_count = args.minimum_count or (32 if args.kind == "manual" else 60)
    return args


def main() -> int:
    args = _parse_args()
    report = build_report(
        json.loads(args.lock.read_text(encoding="utf-8")),
        args.cache_dir,
        args.text_dir,
        args.pdftotext,
        pdftotext_identity(args.pdftotext),
        args.collection_key,
    )
    report["provenance"] = {
        f"{args.collection_key}_lock": {
            "path": args.lock.name,
            "sha256": common._sha256(args.lock),
        }
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"技术文档文本报告：{args.output}")
    if int(summary[args.collection_key]) < args.minimum_count:
        raise ValueError("技术文档文本数量低于覆盖门限")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
