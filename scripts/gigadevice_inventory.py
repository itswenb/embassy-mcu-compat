#!/usr/bin/env python3
"""汇总 GigaDevice 锁定来源和可发布生成物。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import gigadevice_sources as common


CURRENT_KEYS = {
    "addons": "addons",
    "builder": "builder",
    "catalog": "catalog",
    "datasheets": "datasheets",
    "firmware": "firmware",
    "iar": "iar",
    "manuals": "manuals",
    "products": "source",
    "programmer": "tool",
    "target-db": "input",
}


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(f"路径位于项目之外：{path}") from error


def _records(value: object, *, lock: Path, key: str) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        return value
    raise ValueError(f"锁文件字段格式无效：{lock.name}:{key}")


def _source_row(
    record: dict[str, object],
    *,
    source_type: str,
    lock_path: str,
    historical: bool,
) -> dict[str, object]:
    row = {
        key: value
        for key, value in record.items()
        if key
        in {
            "name",
            "version",
            "document_id",
            "published",
            "url",
            "filename",
            "size",
            "sha256",
            "status",
            "revision",
        }
    }
    row.setdefault("name", source_type)
    row["status"] = str(row.get("status", "superseded" if historical else "active"))
    row["source_type"] = source_type
    row["lock_path"] = lock_path
    row["historical"] = historical
    return row


def _source_lock_digest(locks: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for lock in locks:
        digest.update(str(lock["path"]).encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(str(lock["sha256"])))
    return digest.hexdigest()


def build_inventory(
    repo_root: Path,
    lock_dir: Path,
    publication_dir: Path,
    generated_paths: list[Path] | None = None,
) -> dict[str, object]:
    locks = []
    sources = []
    for path in sorted(lock_dir.glob("*.lock.json"), key=lambda item: item.name):
        source_type = path.name.removesuffix(".lock.json")
        key = CURRENT_KEYS.get(source_type)
        if key is None:
            raise ValueError(f"未知来源锁文件：{path.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or key not in data:
            raise ValueError(f"锁文件缺少字段：{path.name}:{key}")
        relative = _relative(path, repo_root)
        locks.append({"path": relative, "sha256": common._sha256(path)})
        current = _records(data[key], lock=path, key=key)
        names = [str(row.get("name", source_type)) for row in current]
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        if duplicates:
            raise ValueError(f"来源锁包含重复逻辑标识：{path.name}:{duplicates[0]}")
        sources.extend(
            _source_row(
                row,
                source_type=source_type,
                lock_path=relative,
                historical=False,
            )
            for row in current
        )
        history = data.get("history", [])
        if not isinstance(history, list) or not all(isinstance(row, dict) for row in history):
            raise ValueError(f"来源历史格式无效：{path.name}")
        sources.extend(
            _source_row(
                row,
                source_type=source_type,
                lock_path=relative,
                historical=True,
            )
            for row in history
        )

    entries = (
        [(path, "file") for path in generated_paths]
        if generated_paths is not None
        else [
            (path, "tree" if path.is_dir() else "file")
            for path in sorted(publication_dir.iterdir(), key=lambda item: item.name)
        ]
    )
    generated = []
    source_digest = _source_lock_digest(locks)
    for path, kind in sorted(entries, key=lambda item: item[0].as_posix()):
        try:
            path.resolve().relative_to(publication_dir.resolve())
        except ValueError as error:
            raise ValueError(f"生成物位于发布目录之外：{path}") from error
        if path.is_symlink() or (kind == "file" and not path.is_file()) or (
            kind == "tree" and not path.is_dir()
        ):
            raise ValueError(f"生成物缺失或类型无效：{path}")
        row = {
            "path": _relative(path, publication_dir),
            "kind": kind,
            "sha256": common._sha256(path) if kind == "file" else common.tree_sha256(path),
            "input_source_digest": source_digest,
        }
        if kind == "tree":
            row["file_count"] = sum(child.is_file() for child in path.rglob("*"))
        generated.append(row)

    statuses = Counter(str(row["status"]) for row in sources)
    return {
        "schema_version": 1,
        "sources": sorted(
            sources,
            key=lambda row: (
                str(row["source_type"]),
                bool(row["historical"]),
                str(row["name"]).casefold(),
                str(row.get("version", "")),
            ),
        ),
        "generated": generated,
        "locks": locks,
        "summary": {
            "lock_count": len(locks),
            "source_count": len(sources),
            "sources_by_status": dict(sorted(statuses.items())),
            "generated_count": len(generated),
            "source_lock_digest": source_digest,
        },
    }


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-dir", type=Path, default=root / "sources/gigadevice")
    parser.add_argument(
        "--publication-dir",
        type=Path,
        default=root / ".cache/generated/mcu-metapac-publication-v1",
    )
    parser.add_argument(
        "--output", type=Path, default=root / "reports/gigadevice-inventory.json"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = Path(__file__).resolve().parent.parent
    inventory = build_inventory(root, args.lock_dir, args.publication_dir)
    common._write_text_atomic(
        args.output,
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(
        f"已汇总 {inventory['summary']['source_count']} 条来源和 "
        f"{inventory['summary']['generated_count']} 个生成物：{args.output}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
