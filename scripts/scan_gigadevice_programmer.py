#!/usr/bin/env python3
"""安全解包并扫描 GD32 All-In-One Programmer 的器件数据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

import gigadevice_sources as common


TOKEN_RE = re.compile(rb"GD32[A-Z0-9_]{2,40}", re.IGNORECASE)
STRUCTURED_SUFFIXES = {".alg", ".flm", ".json", ".pack", ".pdsc", ".svd", ".xml"}


def validate_archive_members(members: list[str]) -> None:
    for member in members:
        path = PurePosixPath(member)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"归档包含不安全路径：{member!r}")


def _file_tokens(path: Path) -> set[str]:
    tokens = {match.decode("ascii").upper() for match in TOKEN_RE.findall(path.name.encode("utf-8"))}
    tail = b""
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            data = tail + chunk
            tokens.update(match.decode("ascii").upper() for match in TOKEN_RE.findall(data))
            tail = data[-64:]
    return tokens


def scan_tree(root: Path) -> dict[str, object]:
    h77_tokens, a7_tokens = set(), set()
    h77_files, a7_files, structured = set(), set(), set()
    file_count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name == ".source.json":
            continue
        file_count += 1
        relative = path.relative_to(root).as_posix()
        tokens = _file_tokens(path)
        h77 = {token for token in tokens if token.startswith("GD32H77")}
        a7 = {token for token in tokens if token.startswith("GD32A7")}
        h77_tokens.update(h77)
        a7_tokens.update(a7)
        if h77:
            h77_files.add(relative)
        if a7:
            a7_files.add(relative)
        if path.suffix.casefold() in STRUCTURED_SUFFIXES:
            structured.add(relative)
    return {
        "files": file_count,
        "structured_files": sorted(structured),
        "h77_tokens": sorted(h77_tokens),
        "h77_files": sorted(h77_files),
        "a7_tokens": sorted(a7_tokens),
        "a7_files": sorted(a7_files),
    }


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name == ".source.json":
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(common._sha256(path)))
    return digest.hexdigest()


def extract_programmer(archive: Path, archive_sha256: str, cache_dir: Path) -> tuple[Path, list[dict[str, object]]]:
    if not archive.is_file() or common._sha256(archive) != archive_sha256:
        raise ValueError(f"Programmer 归档缺失或哈希漂移：{archive}")
    output = cache_dir / archive_sha256[:12]
    marker = output / ".source.json"
    if marker.is_file():
        source = json.loads(marker.read_text(encoding="utf-8"))
        if source.get("archive_sha256") == archive_sha256 and source.get("tree_sha256") == _tree_sha256(output):
            return output, list(source.get("nested_archives", []))
        raise ValueError(f"Programmer 解包缓存校验失败，请人工检查后删除：{output}")
    if output.exists():
        raise ValueError(f"Programmer 解包目标已存在且未锁定：{output}")

    listing = subprocess.run(
        ["bsdtar", "-tf", str(archive)], check=True, text=True, stdout=subprocess.PIPE
    ).stdout.splitlines()
    validate_archive_members(listing)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".programmer-extract.", dir=cache_dir) as directory:
        work = Path(directory) / "result"
        work.mkdir()
        subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(work)], check=True)
        nested = []
        seven_zip = shutil.which("7zz") or shutil.which("7z")
        if seven_zip is not None:
            for installer in sorted(work.rglob("*.exe")):
                target = installer.with_name(installer.name + ".unpacked")
                result = subprocess.run(
                    [seven_zip, "x", "-y", f"-o{target}", str(installer)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                nested.append(
                    {
                        "path": installer.relative_to(work).as_posix(),
                        "extracted": result.returncode == 0 and target.is_dir(),
                    }
                )
                if result.returncode != 0 and target.exists():
                    shutil.rmtree(target)
        work.rename(output)
    source = {
        "schema_version": 1,
        "archive_sha256": archive_sha256,
        "nested_archives": nested,
        "tree_sha256": _tree_sha256(output),
    }
    common._write_text_atomic(
        marker, json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return output, nested


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo_root / "sources/gigadevice/programmer.lock.json",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/tools",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/programmer-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-programmer.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    tool = manifest["tool"]
    archive = args.archive_dir / str(tool["filename"])
    root, nested = extract_programmer(archive, str(tool["sha256"]), args.cache_dir)
    report = {
        "schema_version": 1,
        "source": {
            "name": tool["name"],
            "version": tool["version"],
            "published": tool["published"],
            "archive_sha256": tool["sha256"],
            "tree_sha256": _tree_sha256(root),
            "nested_archives": nested,
        },
        "summary": scan_tree(root),
        "provenance": {
            "manifest": {"path": args.manifest.name, "sha256": common._sha256(args.manifest)}
        },
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={len(value) if isinstance(value, list) else value}" for key, value in report["summary"].items()))
    print(f"Programmer 器件扫描报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
