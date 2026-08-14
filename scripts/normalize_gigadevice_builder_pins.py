#!/usr/bin/env python3
"""把 GD32 Embedded Builder 的封装、引脚函数和重映射表归一为可重建事实。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import gigadevice_builder
import gigadevice_sources as common


GPIO_PIN_RE = re.compile(r"P[A-Z][0-9]+")
GPIO_PIN_PREFIX_RE = re.compile(r"^(P[A-Z][0-9]+)(?:$|[-_/ (])")
ALTERNATE_RE = re.compile(r"Alternate([0-9]+)Functions")
REMAPPING_RE = re.compile(r"REMAP([01]+)")
AF_RE = re.compile(r"AF([0-9]+)")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _functions(name: str, value: str) -> list[tuple[str, int | None, str]]:
    if name == "MainFunction":
        source, af = "main", None
    elif name == "AlternateFunctions":
        source, af = "alternate", None
    elif match := ALTERNATE_RE.fullmatch(name):
        source, af = "alternate", int(match.group(1))
    elif name == "AdditionalFunctions":
        source, af = "additional", None
    elif name == "RemapFunctions":
        source, af = "remap", None
    else:
        return []
    return [
        (source, af, item)
        for item in (part.strip() for part in re.split(r"[,;]", value))
        if item and item != "-"
    ]


def parse_matrix(path: Path, relative: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    if _local(root.tag) != "PinPadMatrix":
        raise ValueError(f"Builder DataSheet 根节点异常：{relative}")
    merged: dict[str, dict[str, object]] = {}
    for element in root.iter():
        if _local(element.tag) != "PinPad":
            continue
        name = element.attrib.get("Name", "").strip().upper()
        if not name:
            raise ValueError(f"Builder DataSheet 引脚名缺失：{relative}")
        packages = {
            (
                _local(child.tag).removeprefix("Package_"),
                str(child.attrib["Number"]),
            )
            for child in element
            if _local(child.tag).startswith("Package_")
            and child.attrib.get("Number") not in {None, "", "-"}
        }
        if not packages:
            continue
        functions = {
            item
            for attribute, value in element.attrib.items()
            for item in _functions(attribute, value)
        }
        pin_type = element.attrib.get("Type", "")
        pin = merged.setdefault(
            name,
            {"name": name, "types": set(), "packages": set(), "functions": set()},
        )
        if pin_type:
            pin["types"].add(pin_type)  # type: ignore[union-attr]
        pin["packages"].update(packages)  # type: ignore[union-attr]
        pin["functions"].update(functions)  # type: ignore[union-attr]
    if not merged:
        raise ValueError(f"Builder DataSheet 不含实际封装引脚：{relative}")
    pins = []
    for pin in merged.values():
        packages: dict[str, list[str]] = {}
        for package, position in pin["packages"]:  # type: ignore[union-attr]
            packages.setdefault(package, []).append(position)
        functions = pin["functions"]
        pins.append(
            {
                "name": pin["name"],
                "types": sorted(pin["types"]),  # type: ignore[arg-type]
                "packages": {
                    package: sorted(
                        set(positions),
                        key=lambda position: (
                            (0, int(position)) if position.isdigit() else (1, position)
                        ),
                    )
                    for package, positions in sorted(packages.items())
                },
                "functions": [
                    {"source": source, "af": af, "name": function}
                    for source, af, function in sorted(
                        functions,  # type: ignore[arg-type]
                        key=lambda item: (
                            item[0],
                            -1 if item[1] is None else item[1],
                            item[2],
                        ),
                    )
                ],
            }
        )
    return {
        "path": relative.as_posix(),
        "sha256": common._sha256(path),
        "pins": sorted(pins, key=lambda pin: str(pin["name"])),
    }


def afio_pattern(relative: Path) -> dict[str, object]:
    code = relative.stem.upper().removeprefix("GD32")
    if not code or re.fullmatch(r"[A-Z0-9]+", code) is None:
        raise ValueError(f"无法解析 Builder AFIO 型号模式：{relative}")
    expression = "".join("[A-Z0-9]" if character == "X" else character for character in code)
    return {
        "code": code,
        "regex": expression,
        "specificity": sum(character != "X" for character in code),
    }


def pattern_matches_device(pattern: dict[str, object], device: str) -> bool:
    candidate = device.upper().removeprefix("GD32")
    return re.match("^" + str(pattern["regex"]), candidate) is not None


def _remap_value(remap: str) -> int | None:
    if match := REMAPPING_RE.fullmatch(remap):
        return int(match.group(1), 2)
    if match := AF_RE.fullmatch(remap):
        return int(match.group(1))
    return None


def _gpio_pin(value: str) -> str | None:
    match = GPIO_PIN_PREFIX_RE.match(value)
    return match.group(1) if match else None


def parse_afio(path: Path, relative: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    if _local(root.tag) != "AFIOPins":
        raise ValueError(f"Builder AFIO 根节点异常：{relative}")
    routes = set()
    for names in root.iter():
        if _local(names.tag) != "PinNames":
            continue
        group = names.attrib.get("FunctionGroupName", "").strip().upper()
        remap = names.attrib.get("RemapAFValue", "").strip().upper()
        if not group or not remap:
            raise ValueError(f"Builder AFIO 路由缺少分组或重映射值：{relative}")
        for pin in names:
            if _local(pin.tag) != "Pin":
                continue
            values = {_local(child.tag): (child.text or "").strip().upper() for child in pin}
            function = values.get("PinUsedFunction", "")
            pin_name = _gpio_pin(values.get("PinName", ""))
            if not function or pin_name is None:
                raise ValueError(f"Builder AFIO 路由缺少函数或引脚：{relative}")
            routes.add((function, group, pin_name, remap, _remap_value(remap)))
    if not routes:
        raise ValueError(f"Builder AFIO 不含路由：{relative}")
    pattern = afio_pattern(relative)
    return {
        "path": relative.as_posix(),
        "sha256": common._sha256(path),
        "model_pattern": pattern["code"],
        "specificity": pattern["specificity"],
        "_pattern": pattern,
        "routes": [
            {
                "function": function,
                "group": group,
                "pin": pin,
                "remap": remap,
                "value": value,
            }
            for function, group, pin, remap, value in sorted(
                routes,
                key=lambda row: (
                    row[1],
                    row[3],
                    row[2],
                    row[0],
                    -1 if row[4] is None else row[4],
                ),
            )
        ],
    }


def _matrix_summary(matrix: dict[str, object]) -> dict[str, object]:
    pins = matrix["pins"]
    assert isinstance(pins, list)
    return {
        "path": matrix["path"],
        "sha256": matrix["sha256"],
        "pins": len(pins),
        "gpio_pins": sum(GPIO_PIN_RE.fullmatch(str(pin["name"])) is not None for pin in pins),
        "functions": sum(len(pin["functions"]) for pin in pins),
        "packages": sorted(
            {package for pin in pins for package in pin["packages"]}
        ),
    }


def _afio_summary(source: dict[str, object]) -> dict[str, object]:
    return {
        "path": source["path"],
        "sha256": source["sha256"],
        "model_pattern": source["model_pattern"],
        "routes": len(source["routes"]),
    }


def build_outputs(
    builder_models: dict[str, object],
    matrices: list[dict[str, object]],
    afio_sources: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    matrices_by_path = {str(matrix["path"]): matrix for matrix in matrices}
    if len(matrices_by_path) != len(matrices):
        raise ValueError("Builder DataSheet 路径重复")
    devices = []
    for source_device in builder_models["devices"]:
        device = str(source_device["id"])
        matrix_paths = [str(path) for path in source_device["matrix_paths"]]
        selected_matrices = []
        for path in matrix_paths:
            if path not in matrices_by_path:
                raise ValueError(f"Builder 型号引用未知 DataSheet：{device}:{path}")
            selected_matrices.append(matrices_by_path[path])
        matches = [
            source
            for source in afio_sources
            if pattern_matches_device(source["_pattern"], device)  # type: ignore[arg-type]
        ]
        if matches:
            specificity = max(int(source["specificity"]) for source in matches)
            matches = [source for source in matches if int(source["specificity"]) == specificity]
        device_routes = [
            dict(route) for source in matches for route in source["routes"]
        ]
        pins = {
            str(pin["name"])
            for matrix in selected_matrices
            for pin in matrix["pins"]
            if GPIO_PIN_RE.fullmatch(str(pin["name"])) is not None
        } | {
            str(route["pin"])
            for route in device_routes
            if GPIO_PIN_RE.fullmatch(str(route["pin"])) is not None
        }
        functions = {
            (str(pin["name"]), str(function["name"]), function["af"], str(function["source"]))
            for matrix in selected_matrices
            for pin in matrix["pins"]
            for function in pin["functions"]
            if GPIO_PIN_RE.fullmatch(str(pin["name"])) is not None
        } | {
            (str(route["pin"]), str(route["function"]), route["value"], "afio")
            for route in device_routes
            if GPIO_PIN_RE.fullmatch(str(route["pin"])) is not None
        }
        packages = {
            package
            for matrix in selected_matrices
            for pin in matrix["pins"]
            for package in pin["packages"]
        }
        devices.append(
            {
                "id": device,
                "status": "normalized" if pins else "missing",
                "matrix_paths": sorted(matrix_paths),
                "afio_paths": sorted(str(source["path"]) for source in matches),
                "routes": device_routes,
                "gpio_pins": len(pins),
                "functions": len(functions),
                "packages": sorted(packages),
                "afio_routes": sum(len(source["routes"]) for source in matches),
            }
        )
    public_afio = [
        {key: value for key, value in source.items() if key != "_pattern"}
        for source in afio_sources
    ]
    full = {
        "schema_version": 1,
        "matrices": matrices,
        "afio": public_afio,
        "devices": devices,
    }
    normalized = sum(device["status"] == "normalized" for device in devices)
    report = {
        "schema_version": 1,
        "summary": {
            "builder_xml_files": len(matrices),
            "afio_xml_files": len(afio_sources),
            "normalized_devices": len(devices),
            "devices_with_normalized_pins": normalized,
            "devices_without_pin_source": len(devices) - normalized,
            "matrix_pins": sum(len(matrix["pins"]) for matrix in matrices),
            "matrix_gpio_pins": sum(
                GPIO_PIN_RE.fullmatch(str(pin["name"])) is not None
                for matrix in matrices
                for pin in matrix["pins"]
            ),
            "matrix_functions": sum(
                len(pin["functions"]) for matrix in matrices for pin in matrix["pins"]
            ),
            "afio_routes": sum(len(source["routes"]) for source in afio_sources),
        },
        "matrices": [_matrix_summary(matrix) for matrix in matrices],
        "afio": [_afio_summary(source) for source in afio_sources],
        "devices": devices,
    }
    return full, report


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
        "--builder-models",
        type=Path,
        default=repo_root / "reports/gigadevice-builder-models.json",
    )
    parser.add_argument(
        "--normalized-output",
        type=Path,
        default=repo_root / ".cache/normalized/gigadevice-builder-pins.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-builder-pins.json",
    )
    parser.add_argument("--show-matrix")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    lock = json.loads(args.builder_lock.read_text(encoding="utf-8"))
    builder = lock["builder"]
    root = gigadevice_builder.find_extracted_root(args.builder_cache, str(builder["sha256"]))
    if args.show_matrix is not None:
        relative = Path(args.show_matrix)
        print(
            json.dumps(
                parse_matrix(root / "DataSheet" / relative, relative),
                ensure_ascii=False,
                indent=2,
                default=sorted,
            )
        )
        return 0
    builder_models = json.loads(args.builder_models.read_text(encoding="utf-8"))
    matrices = []
    for item in builder_models["matrices"]:
        relative = Path(str(item["path"]))
        matrix = parse_matrix(root / "DataSheet" / relative, relative)
        if matrix["sha256"] != item["sha256"]:
            raise ValueError(f"Builder DataSheet 哈希与型号报告不一致：{relative}")
        matrices.append(matrix)
    afio_sources = [
        parse_afio(path, path.relative_to(root / "AFIO"))
        for path in sorted((root / "AFIO").rglob("*.xml"))
    ]
    full, report = build_outputs(builder_models, matrices, afio_sources)
    full_text = json.dumps(full, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    common._write_text_atomic(args.normalized_output, full_text)
    report["provenance"] = {
        "builder_lock": {
            "path": args.builder_lock.name,
            "sha256": common._sha256(args.builder_lock),
        },
        "builder_models": {
            "path": args.builder_models.name,
            "sha256": common._sha256(args.builder_models),
        },
        "normalized_data": {
            "path": args.normalized_output.name,
            "sha256": common._sha256(args.normalized_output),
        },
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    if int(summary["builder_xml_files"]) < 227 or int(summary["afio_xml_files"]) < 26:
        raise ValueError("Builder pins 来源数量低于覆盖门限")
    if int(summary["normalized_devices"]) < 657:
        raise ValueError("Builder pins 型号闭包低于覆盖门限")
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Builder 引脚归一报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
