#!/usr/bin/env python3
"""解析最新 GD32 CMSIS Pack 的 SVD、头文件、内存和 Flash 算法继承。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath

import gigadevice_sources as common


PROPERTY_TAGS = ("compile", "debug", "memory", "algorithm")
NUMERIC_ATTRIBUTES = {"start", "size", "RAMstart", "RAMsize"}
FILE_ATTRIBUTES = {"compile": "header", "debug": "svd", "algorithm": "name"}


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _direct(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _tag(child) == name), None)


def _direct_text(element: ET.Element, name: str) -> str | None:
    child = _direct(element, name)
    return child.text.strip() if child is not None and child.text else None


def normalize_pack_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:", normalized)
        or str(path) != normalized
    ):
        raise ValueError(f"PDSC 包含不安全资源路径：{value!r}")
    return normalized


def _property_key(tag: str, attributes: dict[str, object]) -> tuple[str, str]:
    processor = str(attributes.get("Pname", ""))
    if tag in {"compile", "debug"}:
        return processor, ""
    if tag == "memory":
        identity = attributes.get("id") or attributes.get("name") or attributes.get("start")
    else:
        identity = attributes.get("name") or attributes.get("start")
    if identity is None:
        raise ValueError(f"PDSC {tag} 缺少可继承主键")
    return processor, str(identity)


def _attributes(tag: str, element: ET.Element) -> dict[str, object]:
    values: dict[str, object] = dict(element.attrib)
    file_attribute = FILE_ATTRIBUTES.get(tag)
    if file_attribute is not None and file_attribute in values:
        values[file_attribute] = normalize_pack_path(str(values[file_attribute]))
    for name in NUMERIC_ATTRIBUTES & values.keys():
        try:
            values[name] = int(str(values[name]), 0)
        except ValueError as error:
            raise ValueError(f"PDSC {tag}.{name} 不是整数：{values[name]!r}") from error
    return values


def _apply(
    context: dict[str, dict[tuple[str, str], dict[str, object]]], node: ET.Element
) -> dict[str, dict[tuple[str, str], dict[str, object]]]:
    result = {
        tag: {key: dict(value) for key, value in context[tag].items()}
        for tag in PROPERTY_TAGS
    }
    for child in node:
        tag = _tag(child)
        if tag not in PROPERTY_TAGS:
            continue
        values = _attributes(tag, child)
        key = _property_key(tag, values)
        inherited = dict(result[tag].get(key, {}))
        inherited.update(values)
        result[tag][key] = inherited
    return result


def _snapshot(
    context: dict[str, dict[tuple[str, str], dict[str, object]]]
) -> dict[str, list[dict[str, object]]]:
    return {
        tag: [context[tag][key] for key in sorted(context[tag])]
        for tag in PROPERTY_TAGS
    }


def parse_pdsc_resources(path: Path) -> tuple[tuple[str, str], list[dict[str, object]]]:
    package = ET.parse(path).getroot()
    if _tag(package) != "package":
        package = next((item for item in package.iter() if _tag(item) == "package"), package)
    name = _direct_text(package, "name")
    releases = _direct(package, "releases")
    release = _direct(releases, "release") if releases is not None else None
    version = release.attrib.get("version") if release is not None else None
    if not name or not version:
        raise ValueError(f"PDSC 缺少 Pack 名称或版本：{path}")
    devices = _direct(package, "devices")
    if devices is None:
        return (name, version), []

    empty = {tag: {} for tag in PROPERTY_TAGS}
    records: list[dict[str, object]] = []

    def walk(
        node: ET.Element,
        parent: dict[str, dict[tuple[str, str], dict[str, object]]],
        family: str | None,
        subfamily: str | None,
        parent_device: str | None,
    ) -> None:
        tag = _tag(node)
        current = _apply(parent, node)
        if tag == "family":
            family = node.attrib.get("Dfamily", family)
        elif tag == "subFamily":
            subfamily = node.attrib.get("DsubFamily", subfamily)
        elif tag in {"device", "variant"}:
            attribute = "Dname" if tag == "device" else "Dvariant"
            device = node.attrib.get(attribute)
            if not device:
                raise ValueError(f"PDSC {tag} 缺少 {attribute}：{path}")
            records.append(
                {
                    "device": device,
                    "device_kind": tag,
                    "parent_device": parent_device,
                    "family": family,
                    "subfamily": subfamily,
                    **_snapshot(current),
                }
            )
            if tag == "device":
                parent_device = device
        for child in node:
            if _tag(child) in {"subFamily", "device", "variant"}:
                walk(child, current, family, subfamily, parent_device)

    for family in devices:
        if _tag(family) == "family":
            walk(family, empty, None, None, None)
    return (name, version), records


def _file_evidence(pack_root: Path, relative: str) -> dict[str, object]:
    normalized = normalize_pack_path(relative)
    path = pack_root.joinpath(*PurePosixPath(normalized).parts)
    if not path.is_file():
        raise ValueError(f"PDSC 引用的资源不存在：{path}")
    return {"path": normalized, "sha256": common._sha256(path), "size": path.stat().st_size}


def _materialize(record: dict[str, object], pack_root: Path) -> dict[str, object]:
    result = {key: value for key, value in record.items() if key not in PROPERTY_TAGS}
    for tag in PROPERTY_TAGS:
        values = []
        for raw in record[tag]:  # type: ignore[index]
            item = dict(raw)
            file_attribute = FILE_ATTRIBUTES.get(tag)
            if file_attribute is not None and file_attribute in item:
                item["file"] = _file_evidence(pack_root, str(item.pop(file_attribute)))
            values.append(item)
        result[tag] = values
    return result


def _target_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def build_report(pdsc_root: Path, targets: list[dict[str, object]]) -> dict[str, object]:
    desired = {
        (str(record["source_pack_name"]), str(record["source_pack_version"]))
        for record in targets
    }
    packs: dict[tuple[str, str], tuple[Path, dict[str, dict[str, object]]]] = {}
    for path in sorted(pdsc_root.rglob("*.pdsc")):
        identity, records = parse_pdsc_resources(path)
        if identity not in desired:
            continue
        if identity in packs:
            raise ValueError(f"最新 Pack 的 PDSC 不唯一：{identity}")
        by_device = {str(record["device"]): record for record in records}
        if len(by_device) != len(records):
            raise ValueError(f"PDSC device 重复：{path}")
        packs[identity] = (path, by_device)
    missing_packs = sorted(desired - packs.keys())
    if missing_packs:
        raise ValueError(f"缺少最新 PDSC：{missing_packs}")

    output = []
    for target in sorted(
        targets,
        key=lambda item: (
            str(item["source_pack_name"]),
            str(item["source_pack_version"]),
            str(item["device"]),
        ),
    ):
        identity = (str(target["source_pack_name"]), str(target["source_pack_version"]))
        pdsc, records = packs[identity]
        device = str(target["device"])
        if device not in records:
            raise ValueError(f"目标数据库 device 不在锁定 PDSC 中：{identity} {device}")
        record = _materialize(records[device], pdsc.parent)
        record.update(
            {
                "source_pack_name": identity[0],
                "source_pack_version": identity[1],
                "source_pdsc": {
                    "path": pdsc.relative_to(pdsc_root).as_posix(),
                    "sha256": common._sha256(pdsc),
                },
            }
        )
        output.append(record)

    counts = Counter()
    svd_files: dict[tuple[str, str], dict[str, object]] = {}
    for record in output:
        debug = record["debug"]
        compile_entries = record["compile"]
        memory = record["memory"]
        algorithms = record["algorithm"]
        assert isinstance(debug, list) and isinstance(compile_entries, list)
        assert isinstance(memory, list) and isinstance(algorithms, list)
        svds = [entry["file"] for entry in debug if "file" in entry]
        headers = [entry["file"] for entry in compile_entries if "file" in entry]
        counts["records_with_svd"] += bool(svds)
        counts["records_with_header"] += bool(headers)
        counts["records_with_memory"] += bool(memory)
        counts["records_with_flash_algorithm"] += any("file" in item for item in algorithms)
        for svd in svds:
            assert isinstance(svd, dict)
            pdsc = record["source_pdsc"]
            assert isinstance(pdsc, dict)
            key = (str(record["source_pack_name"]), str(svd["path"]))
            svd_files[key] = {
                "source_pack_name": record["source_pack_name"],
                "source_pack_version": record["source_pack_version"],
                "source_pdsc_path": pdsc["path"],
                **svd,
            }
    return {
        "schema_version": 1,
        "summary": {
            "latest_packs": len(packs),
            "cmsis_records": len(output),
            "records_with_svd": counts["records_with_svd"],
            "records_without_svd": len(output) - counts["records_with_svd"],
            "unique_svd_files": len(svd_files),
            "records_with_header": counts["records_with_header"],
            "records_with_memory": counts["records_with_memory"],
            "records_with_flash_algorithm": counts["records_with_flash_algorithm"],
        },
        "svd_files": [svd_files[key] for key in sorted(svd_files)],
        "devices": output,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdsc-root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/addon-packs-v1",
    )
    parser.add_argument(
        "--target-db",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/target-db-v1/latest/devices.jsonl",
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-pack-resources.json"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_report(args.pdsc_root, _target_records(args.target_db))
    summary = report["summary"]
    assert isinstance(summary, dict)
    if int(summary["latest_packs"]) < 29 or int(summary["cmsis_records"]) < 598:
        raise ValueError("CMSIS Pack 资源覆盖低于门限")
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Pack 资源报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
