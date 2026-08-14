#!/usr/bin/env python3
"""合并 GigaDevice Builder 与官方数据手册中的全型号引脚事实。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import gigadevice_sources as common


def pattern_matches(pattern: str, device: str) -> bool:
    expression = "".join(
        "[A-Z0-9]" if character.casefold() == "x" else re.escape(character.upper())
        for character in pattern
    )
    return re.match(r"^" + expression, device.upper()) is not None


def _specificity(pattern: str) -> int:
    return sum(character.casefold() != "x" for character in pattern)


def _new_pin(name: str) -> dict[str, object]:
    return {
        "name": name,
        "types": set(),
        "packages": {},
        "functions": {},
        "positions_by_origin": {},
    }


def _add_pin(
    pins: dict[str, dict[str, object]],
    source_pin: dict[str, object],
    package_positions: dict[str, list[str]],
    origin: str,
) -> None:
    name = str(source_pin["name"])
    pin = pins.setdefault(name, _new_pin(name))
    types = source_pin.get("types", [source_pin.get("type")])
    pin["types"].update(str(value) for value in types if value)  # type: ignore[union-attr]
    for package, positions in package_positions.items():
        normalized = {str(position) for position in positions}
        pin["packages"].setdefault(package, set()).update(normalized)  # type: ignore[union-attr]
        pin["positions_by_origin"].setdefault((package, origin), set()).update(normalized)  # type: ignore[union-attr]
    for function in source_pin.get("functions", []):
        if not isinstance(function, dict):
            raise ValueError(f"引脚功能条目非法：{name}")
        key = (
            str(function.get("source", "")),
            function.get("af"),
            str(function["name"]),
            function.get("footnote"),
        )
        pin["functions"].setdefault(key, dict(function))  # type: ignore[union-attr]


def _public_pin(
    pin: dict[str, object], packages: dict[str, set[str]]
) -> dict[str, object]:
    functions = pin["functions"]
    return {
        "name": pin["name"],
        "types": sorted(pin["types"]),  # type: ignore[arg-type]
        "packages": {
            package: sorted(positions, key=lambda value: (not value.isdigit(), value))
            for package, positions in sorted(packages.items())  # type: ignore[union-attr]
        },
        "functions": [functions[key] for key in sorted(functions, key=lambda key: tuple("" if value is None else str(value) for value in key))],  # type: ignore[index]
    }


def _resolve_positions(
    pin: dict[str, object],
) -> tuple[dict[str, set[str]], list[dict[str, object]], list[dict[str, object]]]:
    by_package: dict[str, dict[str, set[str]]] = {}
    for (package, origin), positions in pin["positions_by_origin"].items():  # type: ignore[union-attr]
        group = "builder" if str(origin).startswith("builder:") else "datasheet"
        by_package.setdefault(str(package), {}).setdefault(group, set()).update(positions)
    selected = {}
    conflicts = []
    resolutions = []
    for package, groups in sorted(by_package.items()):
        builder_positions = groups.get("builder", set())
        datasheet_positions = groups.get("datasheet", set())
        if not builder_positions or not datasheet_positions:
            selected[package] = builder_positions | datasheet_positions
            continue
        if builder_positions == datasheet_positions:
            selected[package] = datasheet_positions
            continue
        difference = {
            "pin": pin["name"],
            "package": package,
            "builder_positions": sorted(builder_positions),
            "datasheet_positions": sorted(datasheet_positions),
        }
        if builder_positions & datasheet_positions:
            selected[package] = datasheet_positions
            resolutions.append(
                {
                    **difference,
                    "selected_positions": sorted(datasheet_positions),
                    "reason": "current-datasheet-supersedes-builder",
                }
            )
        else:
            selected[package] = builder_positions | datasheet_positions
            conflicts.append(difference)
    return selected, conflicts, resolutions


def build_outputs(
    models: dict[str, object],
    builder: dict[str, object],
    datasheets: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    matrices = {str(row["path"]): row for row in builder["matrices"]}
    builder_devices = {str(row["id"]): row for row in builder["devices"]}
    sourced_tables = [
        (datasheet, table)
        for datasheet in datasheets["datasheets"]
        for table in datasheet["pin_tables"]
        if table.get("device_pattern")
    ]
    devices = []
    reports = []
    for model in models["devices"]:
        device = str(model["id"])
        pins: dict[str, dict[str, object]] = {}
        builder_row = builder_devices.get(device)
        if builder_row is None:
            raise ValueError(f"Builder 引脚记录缺少规范型号：{device}")
        matrix_paths = [str(path) for path in builder_row.get("matrix_paths", [])]
        for path in matrix_paths:
            matrix = matrices.get(path)
            if matrix is None:
                raise ValueError(f"Builder 引脚矩阵不存在：{device}:{path}")
            for pin in matrix["pins"]:
                _add_pin(pins, pin, pin["packages"], f"builder:{path}")
        for route in builder_row.get("routes", []):
            if not isinstance(route, dict):
                raise ValueError(f"Builder AFIO 路由非法：{device}")
            remap = str(route["remap"])
            _add_pin(
                pins,
                {
                    "name": route["pin"],
                    "functions": [
                        {
                            "source": "afio",
                            "af": route["value"] if remap.startswith("AF") else None,
                            "name": route["function"],
                            "group": route["group"],
                            "remap": remap,
                        }
                    ],
                },
                {},
                f"builder-afio:{remap}",
            )

        matches = [
            (datasheet, table)
            for datasheet, table in sourced_tables
            if pattern_matches(str(table["device_pattern"]), device)
        ]
        if matches:
            specificity = max(_specificity(str(table["device_pattern"])) for _, table in matches)
            matches = [
                (datasheet, table)
                for datasheet, table in matches
                if _specificity(str(table["device_pattern"])) == specificity
            ]
        table_sources = []
        for datasheet, table in matches:
            package = str(table["package"])
            origin = f"datasheet:{datasheet['name']}:{table['table']}"
            table_sources.append(
                {
                    "name": datasheet["name"],
                    "pdf_sha256": datasheet["pdf"]["sha256"],
                    "table": table["table"],
                    "page_start": table["page"],
                    "page_end": table["page_end"],
                    "device_pattern": table["device_pattern"],
                    "package": package,
                }
            )
            for pin in table["pins"]:
                _add_pin(
                    pins,
                    pin,
                    {package: [str(pin["position"])]},
                    origin,
                )
        conflicts = []
        resolutions = []
        public_pins = []
        for _, pin in sorted(pins.items()):
            packages, pin_conflicts, pin_resolutions = _resolve_positions(pin)
            conflicts.extend(pin_conflicts)
            resolutions.extend(pin_resolutions)
            public_pins.append(_public_pin(pin, packages))
        status = "conflict" if conflicts else "normalized" if public_pins else "missing"
        row = {
            "id": device,
            "status": status,
            "builder_matrix_paths": sorted(matrix_paths),
            "builder_afio_paths": sorted(str(path) for path in builder_row.get("afio_paths", [])),
            "datasheet_tables": table_sources,
            "conflicts": conflicts,
            "position_resolutions": resolutions,
            "pins": public_pins,
        }
        devices.append(row)
        reports.append(
            {
                "id": device,
                "status": status,
                "matrix_paths": sorted(matrix_paths),
                "afio_paths": sorted(str(path) for path in builder_row.get("afio_paths", [])),
                "datasheet_tables": table_sources,
                "gpio_pins": len(public_pins),
                "functions": sum(len(pin["functions"]) for pin in public_pins),
                "packages": sorted({package for pin in public_pins for package in pin["packages"]}),
                "afio_routes": int(builder_row.get("afio_routes", 0)),
                "conflicts": conflicts,
                "position_resolutions": resolutions,
            }
        )
    summary = {
        "normalized_devices": len(devices),
        "devices_with_normalized_pins": sum(row["status"] == "normalized" for row in devices),
        "devices_with_pin_conflict": sum(row["status"] == "conflict" for row in devices),
        "devices_without_pin_source": sum(row["status"] == "missing" for row in devices),
        "devices_with_builder_pins": sum(bool(row["builder_matrix_paths"]) for row in devices),
        "devices_with_datasheet_pins": sum(bool(row["datasheet_tables"]) for row in devices),
        "resolved_position_differences": sum(
            len(row["position_resolutions"]) for row in devices
        ),
    }
    return (
        {"schema_version": 1, "summary": summary, "devices": devices},
        {"schema_version": 1, "summary": summary, "devices": reports},
    )


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=repo_root / "reports/gigadevice-models.json")
    parser.add_argument(
        "--builder-pins",
        type=Path,
        default=repo_root / ".cache/normalized/gigadevice-builder-pins.json",
    )
    parser.add_argument(
        "--datasheet-pins",
        type=Path,
        default=repo_root / "reports/gigadevice-datasheet-pins.json",
    )
    parser.add_argument(
        "--normalized-output",
        type=Path,
        default=repo_root / ".cache/normalized/gigadevice-pins.json",
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-pins.json"
    )
    parser.add_argument("--minimum-normalized-devices", type=int, default=588)
    parser.add_argument("--maximum-conflicts", type=int, default=0)
    parser.add_argument("--show-status", choices=("normalized", "conflict", "missing"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    inputs = {
        "models": json.loads(args.models.read_text(encoding="utf-8")),
        "builder": json.loads(args.builder_pins.read_text(encoding="utf-8")),
        "datasheets": json.loads(args.datasheet_pins.read_text(encoding="utf-8")),
    }
    full, report = build_outputs(inputs["models"], inputs["builder"], inputs["datasheets"])
    provenance = {
        name: {"path": path.name, "sha256": common._sha256(path)}
        for name, path in {
            "models": args.models,
            "builder_pins": args.builder_pins,
            "datasheet_pins": args.datasheet_pins,
        }.items()
    }
    full["provenance"] = provenance
    common._write_text_atomic(
        args.normalized_output,
        json.dumps(full, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    report["provenance"] = {
        **provenance,
        "normalized_data": {
            "path": args.normalized_output.name,
            "sha256": common._sha256(args.normalized_output),
        },
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    if args.show_status is not None:
        print(
            json.dumps(
                [row for row in report["devices"] if row["status"] == args.show_status],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if int(summary["devices_with_normalized_pins"]) < args.minimum_normalized_devices:
        raise ValueError("完整引脚来源设备数低于覆盖门限")
    if int(summary["devices_with_pin_conflict"]) > args.maximum_conflicts:
        raise ValueError("Builder 与数据手册存在引脚位置冲突")
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"全来源引脚归一报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
