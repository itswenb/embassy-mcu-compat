#!/usr/bin/env python3
"""轻量检查 GigaDevice 来源变化并决定后续同步动作。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import gigadevice_addons
import gigadevice_builder
import gigadevice_catalog
import gigadevice_iar
import gigadevice_manuals
import gigadevice_official_tool
import gigadevice_products
import gigadevice_sources as common
import sync_upstream_research


def pipeline_fingerprint(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.resolve().as_posix()):
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError as error:
            raise ValueError(f"流水线文件位于项目之外：{path}") from error
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"流水线文件缺失或类型无效：{path}")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(common._sha256(path)))
    return digest.hexdigest()


def decide_action(
    changes: dict[str, dict[str, list[str]]],
    fingerprint: str,
    successful_fingerprint: str | None,
) -> str:
    if any(
        names
        for plan in changes.values()
        for key, names in plan.items()
        if key != "unchanged"
    ):
        return "materialize"
    if successful_fingerprint is None:
        return "materialize"
    if fingerprint != successful_fingerprint:
        return "derive"
    return "noop"


def plan_snapshot_change(
    name: str, current: object | None, discovered: object
) -> dict[str, list[str]]:
    plan = {key: [] for key in ("unchanged", "added", "updated", "withdrawn")}
    if current is None:
        plan["added"] = [name]
    elif current == discovered:
        plan["unchanged"] = [name]
    else:
        plan["updated"] = [name]
    return plan


def read_successful_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get("pipeline_fingerprint")
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("成功标记中的流水线指纹无效")
    return value


def mark_success(plan_path: Path, marker_path: Path) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    fingerprint = plan.get("pipeline_fingerprint")
    action = plan.get("action")
    if (
        plan.get("schema_version") != 1
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or action not in {"noop", "derive", "materialize"}
    ):
        raise ValueError("更新计划格式无效，拒绝写成功标记")
    common._write_text_atomic(
        marker_path,
        json.dumps(
            {
                "schema_version": 1,
                "action": action,
                "pipeline_fingerprint": fingerprint,
                "source_change_digest": plan.get("source_change_digest"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _load_lock(root: Path, name: str) -> dict[str, object]:
    data = json.loads(
        (root / f"sources/gigadevice/{name}.lock.json").read_text(encoding="utf-8")
    )
    if not isinstance(data, dict):
        raise ValueError(f"来源锁文件格式无效：{name}")
    return data


def _list_plan(
    current: object,
    discovered: list[dict[str, object]],
    *,
    compare_fields: tuple[str, ...] = ("version", "document_id", "published"),
) -> dict[str, list[str]]:
    if not isinstance(current, list) or not all(isinstance(row, dict) for row in current):
        raise ValueError("来源锁当前记录格式无效")
    return common.plan_source_updates(current, discovered, compare_fields=compare_fields)


def _single_plan(
    current: object,
    discovered: dict[str, object],
    *,
    fallback_name: str,
    compare_fields: tuple[str, ...] = ("version", "document_id", "published"),
) -> dict[str, list[str]]:
    if current is not None and not isinstance(current, dict):
        raise ValueError(f"单例来源锁格式无效：{fallback_name}")
    locked = [] if current is None else [{"name": fallback_name, **current}]
    return common.plan_source_updates(
        locked,
        [{"name": fallback_name, **discovered}],
        compare_fields=compare_fields,
    )


def normalize_catalog_lock_record(record: dict[str, object]) -> dict[str, object]:
    if "available_path_types" in record:
        return dict(record)
    documents = record.get("documents")
    if not isinstance(documents, dict):
        raise ValueError("选型手册锁文件缺少下载入口")
    path_types = [value for key, value in (("en", 1), ("zh", 2)) if key in documents]
    return {**record, "available_path_types": path_types}


def discover_changes(root: Path) -> dict[str, dict[str, list[str]]]:
    firmware = common.discover_firmware()
    addons = gigadevice_addons.discover_addons()
    manuals = gigadevice_manuals.discover_documents(gigadevice_manuals.MANUAL_KIND)
    datasheets = gigadevice_manuals.discover_documents(gigadevice_manuals.DATASHEET_KIND)
    minimums = (("firmware", firmware, 33), ("addons", addons, 30), ("manuals", manuals, 25), ("datasheets", datasheets, 60))
    for name, rows, minimum in minimums:
        if len(rows) < minimum:
            raise ValueError(f"官网仅发现 {len(rows)} 个 {name} 来源，少于下限 {minimum}")

    changes = {
        "firmware": _list_plan(
            _load_lock(root, "firmware").get("firmware"),
            [source._asdict() for source in firmware],
        ),
        "addons": _list_plan(
            _load_lock(root, "addons").get("addons"),
            [source._asdict() for source in addons],
        ),
        "manuals": _list_plan(
            _load_lock(root, "manuals").get("manuals"),
            [gigadevice_manuals._manual_source_data(source) for source in manuals],
            compare_fields=("version", "document_id", "published", "available_path_types"),
        ),
        "datasheets": _list_plan(
            _load_lock(root, "datasheets").get("datasheets"),
            [gigadevice_manuals._manual_source_data(source) for source in datasheets],
            compare_fields=("version", "document_id", "published", "available_path_types"),
        ),
    }

    builder = gigadevice_builder.discover_builder()
    changes["builder"] = _single_plan(
        _load_lock(root, "builder").get("builder"),
        builder._asdict(),
        fallback_name="GD32 Embedded Builder",
    )
    catalog = gigadevice_catalog.discover_catalog()
    catalog_lock = _load_lock(root, "catalog").get("catalog")
    if not isinstance(catalog_lock, dict):
        raise ValueError("选型手册锁文件格式无效")
    changes["catalog"] = _single_plan(
        normalize_catalog_lock_record(catalog_lock),
        {
            "version": catalog.version,
            "document_id": catalog.document_id,
            "published": catalog.published,
            "available_path_types": list(catalog.path_types),
        },
        fallback_name="GD32 MCU Selection Guide",
        compare_fields=("version", "document_id", "published", "available_path_types"),
    )
    tool = gigadevice_official_tool.discover_tool(
        gigadevice_official_tool.DEFAULT_NAME,
        gigadevice_official_tool.DEFAULT_NAME,
    )
    changes["programmer"] = _single_plan(
        _load_lock(root, "programmer").get("tool"),
        tool._asdict(),
        fallback_name=tool.name,
        compare_fields=("version", "document_id", "published", "box_id"),
    )
    iar = gigadevice_iar.discover()
    changes["iar"] = _single_plan(
        _load_lock(root, "iar").get("iar"),
        iar._asdict(),
        fallback_name=iar.name,
        compare_fields=("version", "published", "url", "size", "sha256"),
    )

    product_html = gigadevice_products.read_product_page()
    products = gigadevice_products.parse_product_page(product_html)
    if len(products) < 44:
        raise ValueError(f"官网仅发现 {len(products)} 个 GD32A7 产品，少于下限 44")
    product_report = json.loads(
        (root / "reports/gigadevice-products.json").read_text(encoding="utf-8")
    )
    locked_products = product_report.get("products")
    if not isinstance(locked_products, list):
        raise ValueError("GD32A7 产品报告缺少产品事实")
    changes["products"] = plan_snapshot_change(
        "GD32A7 产品选择器", locked_products, products
    )

    target_lock = _load_lock(root, "target-db")
    generator = target_lock.get("generator")
    if not isinstance(generator, dict):
        raise ValueError("target-db 锁文件缺少 generator")
    remote_revision = sync_upstream_research.remote_head(
        sync_upstream_research.TARGET_DB[0]
    )
    changes["target-db"] = _single_plan(
        {"revision": generator.get("revision")},
        {"revision": remote_revision},
        fallback_name="cmsis-rust-target-db",
        compare_fields=("revision",),
    )
    return dict(sorted(changes.items()))


def pipeline_paths(root: Path) -> list[Path]:
    paths = [
        *root.glob("scripts/*.py"),
        *root.glob("scripts/*.sh"),
        *root.glob("src/**/*.rs"),
    ]
    paths.extend(
        path
        for path in (
            root / "Cargo.toml",
            root / "Cargo.lock",
            root / "sources.lock.toml",
            root / ".github/workflows/update-sources.yml",
        )
        if path.is_file()
    )
    return paths


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=root / ".cache/research/gigadevice/update-plan.json",
    )
    parser.add_argument(
        "--success-marker",
        type=Path,
        default=root / "reports/gigadevice-sync-success.json",
    )
    parser.add_argument("--mark-success", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.mark_success:
        mark_success(args.output, args.success_marker)
        print(f"已记录成功流水线指纹：{args.success_marker}")
        return 0
    root = Path(__file__).resolve().parent.parent
    changes = discover_changes(root)
    fingerprint = pipeline_fingerprint(root, pipeline_paths(root))
    previous = read_successful_fingerprint(args.success_marker)
    encoded_changes = json.dumps(changes, ensure_ascii=False, sort_keys=True).encode()
    plan = {
        "schema_version": 1,
        "action": decide_action(changes, fingerprint, previous),
        "pipeline_fingerprint": fingerprint,
        "previous_successful_fingerprint": previous,
        "source_change_digest": hashlib.sha256(encoded_changes).hexdigest(),
        "changes": changes,
    }
    common._write_text_atomic(
        args.output,
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(f"更新动作：{plan['action']}；计划：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
