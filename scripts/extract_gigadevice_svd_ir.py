#!/usr/bin/env python3
"""把已审计 GD32 SVD 提取为可重复使用的 chiptool JSON IR。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import audit_gigadevice_svds as svd_audit
import gigadevice_sources as common

EXTRACTION_VERSION = 2


def _files(root: Path, suffix: str) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": common._sha256(path),
            "size": path.stat().st_size,
        }
        for path in sorted(root.rglob(f"*{suffix}"))
        if path.is_file()
    ]


def _valid_cache(
    output: Path,
    svd_sha256: str,
    normalized_sha256: str,
    chiptool_revision: str,
    converter_sha256: str,
) -> dict[str, object] | None:
    marker = output / "source.json"
    if not marker.is_file():
        return None
    data = json.loads(marker.read_text(encoding="utf-8"))
    if data.get("schema_version") != EXTRACTION_VERSION:
        return None
    if any(
        data.get(key) != value
        for key, value in {
            "svd_sha256": svd_sha256,
            "normalized_svd_sha256": normalized_sha256,
            "chiptool_revision": chiptool_revision,
            "converter_sha256": converter_sha256,
        }.items()
    ):
        return None
    if data.get("yaml") != _files(output / "yaml", ".yaml"):
        return None
    if data.get("json") != _files(output / "json", ".json"):
        return None
    return data


def _run(command: list[str], description: str) -> None:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise ValueError(f"{description}失败：{detail}")


def extract(
    audit: dict[str, object],
    pdsc_root: Path,
    normalized_dir: Path,
    chiptool: Path,
    converter: Path,
    cache_dir: Path,
) -> dict[str, object]:
    rows = audit.get("svds")
    chiptool_data = audit.get("chiptool")
    if not isinstance(rows, list) or not isinstance(chiptool_data, dict):
        raise ValueError("SVD 审计报告格式无效")
    revision = str(chiptool_data.get("revision", ""))
    converter_sha256 = common._sha256(converter)
    results = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") == "failed":
            raise ValueError("SVD 审计包含无效或失败记录")
        svd_sha256 = str(row["sha256"])
        normalized_sha256 = str(row["normalized_sha256"])
        source = (
            normalized_dir / f"{svd_sha256}.svd"
            if row.get("normalizations")
            else svd_audit._source_path(pdsc_root, row)
        )
        if not source.is_file() or common._sha256(source) != normalized_sha256:
            raise ValueError(f"规范化 SVD 缓存无效：{source}")
        output = cache_dir / svd_sha256
        marker = _valid_cache(
            output,
            svd_sha256,
            normalized_sha256,
            revision,
            converter_sha256,
        )
        status = "cached"
        if marker is None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".svd-ir-", dir=cache_dir) as directory:
                workspace = Path(directory)
                temporary = workspace / "candidate"
                temporary.mkdir()
                yaml_dir = temporary / "yaml"
                json_dir = temporary / "json"
                _run(
                    [
                        str(chiptool),
                        "extract-all",
                        "--svd",
                        str(source),
                        "--output",
                        str(yaml_dir),
                    ],
                    f"提取 {row['path']} chiptool IR",
                )
                _run(
                    [str(converter), str(yaml_dir), str(json_dir)],
                    f"转换 {row['path']} chiptool IR",
                )
                yaml_files = _files(yaml_dir, ".yaml")
                json_files = _files(json_dir, ".json")
                if not yaml_files or len(yaml_files) != len(json_files):
                    raise ValueError(f"SVD IR 转换数量不闭合：{row['path']}")
                marker = {
                    "schema_version": EXTRACTION_VERSION,
                    "svd_sha256": svd_sha256,
                    "normalized_svd_sha256": normalized_sha256,
                    "chiptool_revision": revision,
                    "converter_sha256": converter_sha256,
                    "yaml": yaml_files,
                    "json": json_files,
                }
                common._write_text_atomic(
                    temporary / "source.json",
                    json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )
                stale = workspace / "stale"
                if output.exists() or output.is_symlink():
                    if output.is_symlink() or not output.is_dir():
                        raise ValueError(f"SVD IR 缓存不是安全目录：{output}")
                    output.rename(stale)
                try:
                    temporary.rename(output)
                except BaseException:
                    if stale.exists() and not output.exists():
                        stale.rename(output)
                    raise
            status = "generated"
        register_roots = row.get("peripheral_register_roots")
        interrupt_vectors = row.get("interrupt_vectors")
        if not isinstance(register_roots, list) or not isinstance(interrupt_vectors, list):
            raise ValueError(f"SVD 审计缺少逐实例寄存器根或中断：{row['path']}")
        results.append(
            {
                "svd_sha256": svd_sha256,
                "path": row["path"],
                "status": status,
                "register_roots": register_roots,
                "interrupt_vectors": interrupt_vectors,
                "yaml_files": len(marker["yaml"]),
                "json_files": len(marker["json"]),
            }
        )
    return {
        "schema_version": 1,
        "summary": {
            "svds": len(results),
            "generated": sum(row["status"] == "generated" for row in results),
            "cached": sum(row["status"] == "cached" for row in results),
            "json_files": sum(int(row["json_files"]) for row in results),
        },
        "svds": results,
    }


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=root / "reports/gigadevice-svd-audit.json")
    parser.add_argument(
        "--pdsc-root",
        type=Path,
        default=root / ".cache/research/gigadevice/addon-packs-v1",
    )
    parser.add_argument(
        "--normalized-dir",
        type=Path,
        default=root / ".cache/research/gigadevice/normalized-svd-v4",
    )
    parser.add_argument(
        "--chiptool", type=Path, default=root / ".cache/tools/chiptool-target/release/chiptool"
    )
    parser.add_argument("--converter", type=Path, default=root / "target/debug/m32-chiptool-ir-json")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=root / ".cache/research/gigadevice/svd-ir-v1",
    )
    parser.add_argument(
        "--output", type=Path, default=root / "reports/gigadevice-svd-ir.json"
    )
    args = parser.parse_args()
    report = extract(
        json.loads(args.audit.read_text(encoding="utf-8")),
        args.pdsc_root,
        args.normalized_dir,
        args.chiptool,
        args.converter,
        args.cache_dir,
    )
    report["provenance"] = {
        "audit": {"path": args.audit.name, "sha256": common._sha256(args.audit)}
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in report["summary"].items()))
    print(f"SVD chiptool IR：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
