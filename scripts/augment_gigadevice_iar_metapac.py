#!/usr/bin/env python3
"""把 IAR A7 PAC 与最小 metadata 合入原生 mcu-metapac 生成树。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath

import audit_gigadevice_svds as svd_audit
import compare_gigadevice_svd_headers as svd_compare
import compile_gigadevice_pacs as pac_compile
import gigadevice_sources as common


MARKER = ".m32-metapac-generation.json"


def strip_inner_attributes(source: str) -> str:
    position = 0
    while True:
        start = position
        while position < len(source) and source[position].isspace():
            position += 1
        if not source.startswith("#", position):
            return source[position:] if position != start else source[start:]
        cursor = position + 1
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source) or source[cursor] != "!":
            return source[position:]
        cursor += 1
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source) or source[cursor] != "[":
            return source[position:]
        depth = 0
        quoted = False
        escaped = False
        for cursor in range(cursor, len(source)):
            character = source[cursor]
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
                continue
            if character == '"':
                quoted = True
            elif character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth == 0:
                    position = cursor + 1
                    break
        else:
            raise ValueError("chiptool PAC 内部属性未闭合")


def strip_embedded_common(source: str) -> str:
    marker = "pub mod common {"
    if source.count(marker) != 1:
        raise ValueError("chiptool PAC 必须包含唯一内置 common 模块")
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    quoted = False
    escaped = False
    for cursor in range(opening, len(source)):
        character = source[cursor]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return (
                    source[:start].rstrip()
                    + "\n"
                    + source[cursor + 1 :].lstrip()
                ).strip()
    raise ValueError("chiptool PAC 内置 common 模块未闭合")


def _rust_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _metadata(device: dict[str, object], facts: dict[str, object]) -> str:
    memories = []
    for region in device["memory"]:
        name = str(region["name"])
        if name in {"Bank0", "Bank1", "Dflash"}:
            kind = "Flash"
        elif name in {"ITCM", "DTCM", "SRAM0"}:
            kind = "Ram"
        else:
            continue
        memories.append(
            "MemoryRegion { "
            f"name: {_rust_string(name)}, kind: MemoryRegionKind::{kind}, "
            f"address: 0x{int(region['start']):08x}, size: {int(region['size'])}, settings: None "
            "}"
        )
    if not memories:
        raise ValueError(f"IAR A7 型号缺少应用内存：{device['id']}")
    bases = facts["peripheral_base_addresses"]
    interrupts = facts["interrupts"]
    assert isinstance(bases, dict) and isinstance(interrupts, list)
    peripherals = "\n".join(
        "Peripheral { "
        f"name: {_rust_string(str(name))}, address: 0x{int(address):x}, registers: None, "
        "rcc: None, pins: &[], dma_channels: &[], triggers: &[], interrupts: &[], afio: None },"
        for name, address in sorted(bases.items())
    )
    interrupt_rows = "\n".join(
        "Interrupt { "
        f"name: {_rust_string(str(row['name']))}, number: {int(row['value'])} "
        "},"
        for row in interrupts
    )
    memory_rows = ",\n".join(memories)
    name = str(device["id"])
    return f"""\
pub(crate) static PERIPHERALS: &[Peripheral] = &[{peripherals}];
pub(crate) static INTERRUPTS: &[Interrupt] = &[{interrupt_rows}];
pub(crate) static DMA_CHANNELS: &[DmaChannel] = &[];
pub(crate) static PINS: &[Pin] = &[];
pub static METADATA: Metadata = Metadata {{
    name: {_rust_string(name)},
    family: "GD32",
    line: {_rust_string(name[:8])},
    memory: &[&[{memory_rows}]],
    peripherals: PERIPHERALS,
    nvic_priority_bits: Some(4),
    interrupts: INTERRUPTS,
    dma_channels: DMA_CHANNELS,
    pins: PINS,
}};
"""


def _rewrite_build(path: Path, chips: list[str]) -> None:
    source = path.read_text(encoding="utf-8")
    start = source.find("    let chips = [")
    end = source.find("    ];", start)
    if start < 0 or end < 0:
        raise ValueError("原生 metapac build.rs 芯片表格式异常")
    entries = "".join(
        f'        ("CARGO_FEATURE_{chip.upper().replace("-", "_")}", "{chip.lower()}"),\n'
        for chip in chips
    )
    prefix_end = source.find("\n", start) + 1
    path.write_text(source[:prefix_end] + entries + source[end:], encoding="utf-8")


def _rewrite_all_chips(path: Path, chips: list[str]) -> None:
    path.write_text(
        "pub static ALL_CHIPS: &[&str] = &[\n"
        + "".join(f'    "{chip}",\n' for chip in chips)
        + "];\n",
        encoding="utf-8",
    )


def augment(
    base: Path,
    output: Path,
    iar: dict[str, object],
    audit: dict[str, object],
    source_root: Path,
    generated_cache: Path,
    *,
    replace: bool,
) -> dict[str, object]:
    base_marker = base / MARKER
    if not base_marker.is_file():
        raise ValueError("原生 metapac 基础树缺少生成标记")
    if output.exists() and (not replace or not (output / MARKER).is_file()):
        raise ValueError(f"拒绝替换非生成输出：{output}")
    raw_devices = iar.get("devices")
    raw_svds = audit.get("svds")
    if not isinstance(raw_devices, list) or not isinstance(raw_svds, list):
        raise ValueError("IAR 报告缺少 devices/svds")
    if len(raw_devices) != 48 or len(raw_svds) != 3:
        raise ValueError("IAR A7 增量必须闭包 48 个型号和 3 份 SVD")

    svds = {Path(str(row["path"])).name: row for row in raw_svds}
    pacs = {}
    facts = {}
    for name, row in svds.items():
        lib, marker = pac_compile._find_generated(generated_cache, row)
        if marker.get("svd_sha256") != row["sha256"]:
            raise ValueError(f"IAR PAC 生成标记不一致：{name}")
        relative = PurePosixPath(str(row["path"]))
        source = source_root.joinpath(*relative.parts)
        if not source.is_file() or common._sha256(source) != row["sha256"]:
            raise ValueError(f"IAR SVD 来源不一致：{source}")
        normalized, _ = svd_audit.normalized_svd_bytes(source)
        pacs[name] = lib
        facts[name] = svd_compare.svd_facts(normalized)

    base_generation = json.loads(base_marker.read_text(encoding="utf-8"))
    base_chips = re.findall(r'^\s*"([A-Za-z0-9-]+)",\s*$', (base / "src/all_chips.rs").read_text(encoding="utf-8"), re.MULTILINE)
    added = sorted(str(row["id"]) for row in raw_devices)
    if set(base_chips) & set(added):
        raise ValueError("原生 metapac 基础树已包含 IAR A7 型号")
    chips = sorted([*base_chips, *added])

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".iar-metapac.") as directory:
        temporary = Path(directory) / "output"
        shutil.copytree(base, temporary)
        for row in raw_devices:
            assert isinstance(row, dict)
            chip = str(row["id"]).lower()
            directory_path = temporary / "src/chips" / chip
            directory_path.mkdir()
            svd_name = str(row["svd"])
            pac_source = strip_embedded_common(
                strip_inner_attributes(pacs[svd_name].read_text(encoding="utf-8"))
            )
            (directory_path / "pac.rs").write_text(pac_source, encoding="utf-8")
            shutil.copy2(pacs[svd_name].with_name("device.x"), directory_path / "device.x")
            (directory_path / "metadata.rs").write_text(
                _metadata(row, facts[svd_name]), encoding="utf-8"
            )

        manifest = temporary / "Cargo.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").rstrip()
            + "\n"
            + "".join(f"{chip.lower()} = []\n" for chip in added),
            encoding="utf-8",
        )
        _rewrite_build(temporary / "build.rs", chips)
        _rewrite_all_chips(temporary / "src/all_chips.rs", chips)
        generation = {
            **base_generation,
            "chips": len(chips),
            "iar_devices": len(added),
            "iar_svd_files": len(svds),
            "iar_source_sha256": common._sha256(source_root / ".source.json"),
        }
        common._write_text_atomic(
            temporary / MARKER,
            json.dumps(generation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        if output.exists():
            shutil.rmtree(output)
        temporary.rename(output)
    return {
        "schema_version": 1,
        "base_chips": len(base_chips),
        "iar_devices": len(added),
        "chips": len(chips),
        "output_tree_sha256": common.tree_sha256(output),
    }


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=root / ".cache/generated/gigadevice-metapac-v1")
    parser.add_argument("--output", type=Path, default=root / ".cache/generated/gigadevice-metapac-complete-v1")
    parser.add_argument("--iar", type=Path, default=root / "reports/gigadevice-iar-a7.json")
    parser.add_argument("--audit", type=Path, default=root / "reports/gigadevice-iar-svd-audit.json")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--generated-cache", type=Path, default=root / ".cache/research/gigadevice/chiptool-svd-v1")
    parser.add_argument("--report", type=Path, default=root / "reports/gigadevice-complete-metapac.json")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    report = augment(
        args.base,
        args.output,
        json.loads(args.iar.read_text(encoding="utf-8")),
        json.loads(args.audit.read_text(encoding="utf-8")),
        args.source_root,
        args.generated_cache,
        replace=args.replace,
    )
    common._write_text_atomic(
        args.report, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(f"chips={report['chips']} iar_devices={report['iar_devices']} output={args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
