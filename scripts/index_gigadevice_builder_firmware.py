#!/usr/bin/env python3
"""索引 GD32 Embedded Builder 内置固件插件，不发布受限原始文件。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import gigadevice_builder
import gigadevice_sources as common


PLUGIN_RE = re.compile(
    r"^com\.gigadevice\.templatefwlib\.(arm|riscv)\.(gd32.+?)_\d+\.\d+\.\d+\..+$",
    re.IGNORECASE,
)
SELECTOR_RE = re.compile(
    r"(?:defined\s*\(\s*|^\s*#\s*ifn?def\s+)(GD32[A-Za-z0-9_]+)", re.MULTILINE
)
CORE_TARGETS = {
    "cm0": ("Cortex-M0", "thumbv6m-none-eabi"),
    "cm0plus": ("Cortex-M0+", "thumbv6m-none-eabi"),
    "cm3": ("Cortex-M3", "thumbv7m-none-eabi"),
    "cm4": ("Cortex-M4", "thumbv7em-none-eabi"),
    "cm7": ("Cortex-M7", "thumbv7em-none-eabi"),
    "cm23": ("Cortex-M23", "thumbv8m.base-none-eabi"),
    "cm33": ("Cortex-M33", "thumbv8m.main-none-eabi"),
}


def _device_headers(plugin: Path) -> list[Path]:
    candidates = {
        *plugin.glob("Firmware/Firmware/CMSIS/GD/*/Include/gd32*.h"),
        *plugin.glob("Firmware/Firmware/GD32*_standard_peripheral/gd32*.h"),
    }
    return sorted(
        path
        for path in candidates
        if "_err_" not in path.name.casefold()
        and not path.stem.casefold().endswith("_err")
        and not path.stem.casefold().startswith("system_")
    )


def _families(root: Path, kind: str) -> list[str]:
    directory = root / "resources" / kind
    return sorted(path.name for path in directory.iterdir() if path.is_dir())


def build_report(root: Path) -> dict[str, object]:
    plugins = []
    for path in sorted((root / "plugins").iterdir()):
        if not path.is_dir():
            continue
        plugin_match = PLUGIN_RE.fullmatch(path.name)
        if plugin_match is None:
            raise ValueError(f"Builder 固件插件名无法解析：{path.name}")
        headers = []
        selectors = set()
        for header in _device_headers(path):
            source = header.read_text(encoding="utf-8", errors="ignore")
            selected = sorted(set(SELECTOR_RE.findall(source)))
            selectors.update(selected)
            headers.append(
                {
                    "path": header.relative_to(root).as_posix(),
                    "sha256": common._sha256(header),
                    "selectors": selected,
                }
            )
        licenses = [
            {
                "path": license_path.relative_to(root).as_posix(),
                "sha256": common._sha256(license_path),
            }
            for license_path in sorted(path.rglob("*"))
            if license_path.is_file() and "license" in license_path.name.casefold()
        ]
        core_headers = sorted(path.glob("Firmware/Firmware/CMSIS/core_*.h"))
        cores = {
            core_id: CORE_TARGETS[core_id]
            for core_header in core_headers
            if (core_id := core_header.stem.removeprefix("core_")) in CORE_TARGETS
        }
        if plugin_match.group(1).lower() == "arm" and len(cores) != 1:
            raise ValueError(f"Builder Arm 固件插件内核无法唯一识别：{path.name}")
        core, rust_target = next(iter(cores.values()), (None, None))
        if core in {"Cortex-M4", "Cortex-M7", "Cortex-M33"} and any(
            "#define __FPU_PRESENT" in header.read_text(encoding="utf-8", errors="ignore")
            and re.search(r"#define\s+__FPU_PRESENT\s+1[Uu]?\b", header.read_text(encoding="utf-8", errors="ignore"))
            for header in _device_headers(path)
        ):
            rust_target += "hf"
        plugins.append(
            {
                "id": path.name,
                "architecture": plugin_match.group(1).lower(),
                "series": plugin_match.group(2).lower(),
                "core": core,
                "rust_target": rust_target,
                "device_headers": headers,
                "selectors": sorted(selectors),
                "model_selectors": sorted(
                    selector for selector in selectors if "_" not in selector
                ),
                "licenses": licenses,
                "files": sum(item.is_file() for item in path.rglob("*")),
            }
        )
    missing = [plugin["id"] for plugin in plugins if not plugin["device_headers"]]
    code_generate = _families(root, "CodeGenerate")
    code_template = _families(root, "CodeTemplate")
    return {
        "schema_version": 1,
        "summary": {
            "plugins": len(plugins),
            "arm_plugins": sum(plugin["architecture"] == "arm" for plugin in plugins),
            "riscv_plugins": sum(
                plugin["architecture"] == "riscv" for plugin in plugins
            ),
            "device_headers": sum(len(plugin["device_headers"]) for plugin in plugins),
            "model_selectors": len(
                {selector for plugin in plugins for selector in plugin["model_selectors"]}
            ),
            "plugins_without_device_headers": len(missing),
            "code_generate_families": len(code_generate),
            "code_template_families": len(code_template),
        },
        "plugins_without_device_headers": missing,
        "plugins": plugins,
        "code_generate_families": code_generate,
        "code_template_families": code_template,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--builder-lock",
        type=Path,
        default=repo_root / "sources/gigadevice/builder.lock.json",
    )
    parser.add_argument(
        "--builder-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/builder-firmware-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-builder-firmware.json",
    )
    parser.add_argument("--minimum-plugins", type=int, default=36)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    lock = json.loads(args.builder_lock.read_text(encoding="utf-8"))
    archive_sha256 = str(lock["builder"]["sha256"])
    root = gigadevice_builder.find_extracted_root(args.builder_cache, archive_sha256)
    report = build_report(root)
    report["provenance"] = {
        "builder_archive_sha256": archive_sha256,
        "builder_tree_sha256": (root / ".tree-sha256").read_text(encoding="utf-8").strip(),
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    if int(summary["plugins"]) < args.minimum_plugins:
        raise ValueError("Builder 固件插件数量低于覆盖门限")
    if int(summary["plugins_without_device_headers"]) != 0:
        raise ValueError("Builder 固件插件缺少 CMSIS 器件头")
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Builder 固件索引：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
