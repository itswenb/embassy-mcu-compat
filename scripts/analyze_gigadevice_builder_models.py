#!/usr/bin/env python3
"""把 GD32 Embedded Builder 的 DataSheet XML 映射到规范型号与订货号。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import gigadevice_builder
import gigadevice_sources as common


def builder_pattern(relative: Path) -> dict[str, object]:
    code = re.sub(r"(?i)(?:data)?sheet$", "", relative.stem)
    code = re.sub(r"(?i)-B$", "", code).upper().removeprefix("GD32")
    if code and code[0].isdigit():
        group = relative.parts[0].upper().removeprefix("GD32")
        if not group or not group[0].isalpha():
            raise ValueError(f"无法从 Builder 路径补齐产品线：{relative}")
        code = group[0] + code
    if not code or re.fullmatch(r"[A-Z0-9]+", code) is None:
        raise ValueError(f"无法解析 Builder 型号模式：{relative}")
    ancestors = {
        part.upper().removeprefix("GD32") for part in relative.parts[:-1]
    }
    regex = "".join("[A-Z0-9]" if char == "X" else re.escape(char) for char in code)
    return {"code": code, "regex": regex, "generic_family": code in ancestors}


def pattern_matches(pattern: dict[str, object], model: str, *, part_number: bool) -> bool:
    candidate = model.upper().removeprefix("GD32")
    code = str(pattern["code"])
    if bool(pattern["generic_family"]):
        return candidate.startswith(code)
    regex = str(pattern["regex"])
    if re.fullmatch(regex, candidate) is not None:
        return True
    return part_number and re.fullmatch(regex + r"[0-9](?:TR|TA|[A-Z])?", candidate) is not None


def pattern_matches_device_prefix(pattern: dict[str, object], model: str) -> bool:
    """把封装专用矩阵归入同一裸片 device，但不用于其他 part_number。"""
    if bool(pattern["generic_family"]):
        return False
    candidate = model.upper().removeprefix("GD32")
    code = str(pattern["code"])
    if len(candidate) >= len(code):
        return False
    prefix = "".join(
        "[A-Z0-9]" if character == "X" else re.escape(character)
        for character in code[: len(candidate)]
    )
    suffix = code[len(candidate) :]
    return (
        re.fullmatch(prefix, candidate) is not None
        and re.fullmatch(r"[A-Z][A-Z0-9]{0,3}", suffix) is not None
    )


def xml_packages(path: Path) -> tuple[dict[str, int], int]:
    root = ET.parse(path).getroot()
    packages: Counter[str] = Counter()
    pins = 0
    for pin in root.iter():
        if pin.tag.rsplit("}", 1)[-1] != "PinPad":
            continue
        pins += 1
        for item in pin:
            tag = item.tag.rsplit("}", 1)[-1]
            if tag.startswith("Package_") and item.attrib.get("Number") not in {None, "", "-"}:
                packages[tag.removeprefix("Package_")] += 1
    return dict(sorted(packages.items())), pins


def _matrices(root: Path) -> list[dict[str, object]]:
    data_root = root / "DataSheet"
    rows = []
    for path in sorted(data_root.rglob("*.xml")):
        relative = path.relative_to(data_root)
        pattern = builder_pattern(relative)
        packages, pins = xml_packages(path)
        if not packages or pins == 0:
            raise ValueError(f"Builder XML 不含有效封装/引脚：{relative}")
        rows.append(
            {
                "path": relative.as_posix(),
                "sha256": common._sha256(path),
                "model_pattern": pattern["code"],
                "generic_family": pattern["generic_family"],
                "packages": packages,
                "pin_pads": pins,
                "_pattern": pattern,
            }
        )
    return rows


def build_report(models: dict[str, object], matrices: list[dict[str, object]]) -> dict[str, object]:
    raw_devices = models["devices"]
    raw_entries = models["catalog_entries"]
    assert isinstance(raw_devices, list) and isinstance(raw_entries, list)
    device_rows = {str(row["id"]): row for row in raw_devices}
    parts_by_device: dict[str, list[str]] = defaultdict(list)
    for entry in raw_entries:
        assert isinstance(entry, dict)
        if entry["kind"] == "part_number":
            parts_by_device[str(entry["device"])].append(str(entry["id"]))

    direct_part_paths: dict[str, list[str]] = {}
    for parts in parts_by_device.values():
        for part in parts:
            direct_part_paths[part] = [
                str(matrix["path"])
                for matrix in matrices
                if pattern_matches(matrix["_pattern"], part, part_number=True)  # type: ignore[arg-type]
            ]

    device_direct_paths: dict[str, list[str]] = {}
    devices = []
    for device in sorted(device_rows):
        direct = {
            str(matrix["path"])
            for matrix in matrices
            if pattern_matches(matrix["_pattern"], device, part_number=False)  # type: ignore[arg-type]
        }
        prefixed = {
            str(matrix["path"])
            for matrix in matrices
            if pattern_matches_device_prefix(matrix["_pattern"], device)  # type: ignore[arg-type]
        }
        device_direct_paths[device] = sorted(direct)
        part_paths = {
            path for part in parts_by_device[device] for path in direct_part_paths[part]
        }
        paths = sorted(direct | prefixed | part_paths)
        devices.append(
            {
                "id": device,
                "evidence": "builder-model" if paths else "none",
                "matrix_paths": paths,
            }
        )

    parts = []
    for device in sorted(parts_by_device):
        device_paths = device_direct_paths[device]
        for part in sorted(parts_by_device[device]):
            direct = sorted(direct_part_paths[part])
            evidence = "part-pattern" if direct else "device-matrix" if device_paths else "none"
            parts.append(
                {
                    "id": part,
                    "device": device,
                    "evidence": evidence,
                    "matrix_paths": direct if direct else device_paths,
                }
            )

    device_counts = Counter(str(row["evidence"]) for row in devices)
    part_counts = Counter(str(row["evidence"]) for row in parts)
    used_paths = {
        str(path)
        for row in [*devices, *parts]
        for path in row["matrix_paths"]
    }
    public_matrices = [
        {key: value for key, value in matrix.items() if key != "_pattern"}
        for matrix in matrices
    ]
    return {
        "schema_version": 1,
        "summary": {
            "builder_xml_files": len(matrices),
            "normalized_devices": len(devices),
            "devices_with_builder_evidence": device_counts["builder-model"],
            "devices_without_builder_evidence": device_counts["none"],
            "part_numbers": len(parts),
            "part_pattern_verified": part_counts["part-pattern"],
            "part_device_matrix_only": part_counts["device-matrix"],
            "part_without_builder_evidence": part_counts["none"],
            "unmatched_matrices": len(matrices) - len(used_paths),
        },
        "matrices": public_matrices,
        "devices": devices,
        "part_numbers": parts,
        "unmatched_matrices": [
            row for row in public_matrices if str(row["path"]) not in used_paths
        ],
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--builder-lock", type=Path, default=repo_root / "sources/gigadevice/builder.lock.json"
    )
    parser.add_argument(
        "--builder-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/builder-resources",
    )
    parser.add_argument(
        "--models", type=Path, default=repo_root / "reports/gigadevice-models.json"
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-builder-models.json"
    )
    parser.add_argument("--show-unmatched", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    lock = json.loads(args.builder_lock.read_text(encoding="utf-8"))
    builder = lock["builder"]
    assert isinstance(builder, dict)
    root = gigadevice_builder.find_extracted_root(args.builder_cache, str(builder["sha256"]))
    report = build_report(
        json.loads(args.models.read_text(encoding="utf-8")), _matrices(root)
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    if int(summary["builder_xml_files"]) < 227:
        raise ValueError("Builder DataSheet XML 数量低于覆盖门限")
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if args.show_unmatched:
        print(json.dumps(report["unmatched_matrices"], ensure_ascii=False, indent=2))
        return 0
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Builder 型号证据：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
