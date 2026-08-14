#!/usr/bin/env python3
"""归一 GD32 CMSIS Pack 的内存区域与 Flash 算法几何。"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path, PurePosixPath

import gigadevice_sources as common
import gigadevice_builder


ELF_HEADER = struct.Struct("<16sHHIIIIIHHHHHH")
SECTION_HEADER = struct.Struct("<IIIIIIIIII")
SYMBOL = struct.Struct("<IIIBBH")
LINKER_MEMORY_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*\([^)]*\)\s*:\s*ORIGIN\s*=\s*"
    r"(0x[0-9A-Fa-f]+)\s*,\s*LENGTH\s*=\s*(\d+)\s*([KM]?)\s*$",
    re.MULTILINE,
)


def parse_linker_ram(text: str) -> list[dict[str, object]]:
    scales = {"": 1, "K": 1024, "M": 1024 * 1024}
    rows = [
        {
            "name": name,
            "kind": "ram",
            "address": int(address, 16),
            "size": int(size) * scales[unit],
        }
        for name, address, size, unit in LINKER_MEMORY_RE.findall(text)
        if name not in {"CNVM", "ECNVM"}
    ]
    if not rows:
        raise ValueError("Builder 链接脚本不含固定 RAM 区域")
    return sorted(rows, key=lambda row: (int(row["address"]), str(row["name"])))


def _wildcard_matches(pattern: str, part: str) -> bool:
    return len(pattern) == len(part) and all(
        expected.casefold() == "x" or expected.upper() == actual.upper()
        for expected, actual in zip(pattern, part, strict=True)
    )


def build_programmer_profiles(
    models: dict[str, object],
    programmer: dict[str, object],
    ram: list[dict[str, object]],
    linker_source: dict[str, str],
) -> list[dict[str, object]]:
    flash_profiles = programmer.get("flash_profiles")
    if not isinstance(flash_profiles, list):
        raise ValueError("Programmer 数据缺少 flash_profiles")
    profiles = []
    for model in models["devices"]:
        assert isinstance(model, dict)
        if model.get("source") != "programmer":
            continue
        parts = [str(part) for part in model.get("part_numbers", [])]
        matches = [
            profile
            for profile in flash_profiles
            if isinstance(profile, dict)
            and any(_wildcard_matches(str(profile["pattern"]), part) for part in parts)
        ]
        if not matches:
            continue
        if len(matches) != 1:
            raise ValueError(f"Programmer Flash profile 非唯一：{model['id']}")
        profile = matches[0]
        pages = profile.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError(f"Programmer Flash profile 无页面：{model['id']}")
        flash = []
        for index, page in enumerate(pages):
            assert isinstance(page, dict)
            size = int(page["count"]) * int(page["page_size"])
            resolutions = []
            if str(page["geometry_status"]) == "source-inconsistent":
                resolutions.append("page-count-and-declared-total-supersede-end-address")
            flash.append(
                {
                    "name": f"{str(page['type'] or 'Flash').upper()}_{index}",
                    "address": int(page["address"]),
                    "size": size,
                    "erase_size": int(page["page_size"]),
                    "write_size": 32 if str(page["type"]).upper() == "RRAM" else 8,
                    "erase_value": 255,
                    "bank": str(page["bank"]),
                    "source_resolutions": resolutions,
                }
            )
        total = sum(int(region["size"]) for region in flash)
        declared = int(profile.get("rram_size", 0)) + int(profile.get("flash_size", 0))
        if declared and total != declared:
            raise ValueError(f"Programmer Flash 总容量不闭合：{model['id']}")
        profiles.append(
            {
                "device": model["id"],
                "source_kind": "programmer-and-builder",
                "source": profile["source"],
                "linker_source": linker_source,
                "memory": [*ram, *({**region, "kind": "flash"} for region in flash)],
                "flash": flash,
                "flash_status": "geometry-only",
            }
        )
    return profiles


def build_riscv_profiles(
    models: dict[str, object],
    programmer: dict[str, object],
    riscv: dict[str, object],
) -> list[dict[str, object]]:
    libraries = riscv.get("libraries")
    if not isinstance(libraries, list):
        raise ValueError("RISC-V 来源缺少 libraries")
    linkers = [
        profile
        for library in libraries
        if isinstance(library, dict)
        for profile in library.get("linker_profiles", [])
        if isinstance(profile, dict)
    ]
    profiles = []
    for model in models["devices"]:
        assert isinstance(model, dict)
        matches = [
            profile
            for profile in linkers
            if _wildcard_matches(str(profile["pattern"]), str(model["id"]))
        ]
        if not matches:
            continue
        if len(matches) != 1:
            raise ValueError(f"RISC-V 链接内存 profile 非唯一：{model['id']}")
        linker = matches[0]
        memory = linker.get("memory")
        if not isinstance(memory, list):
            raise ValueError(f"RISC-V 链接内存 profile 无效：{model['id']}")
        flash = [row for row in memory if row["kind"] == "flash"]
        ram = [row for row in memory if row["kind"] == "ram"]
        if len(flash) != 1 or not ram:
            raise ValueError(f"RISC-V 链接内存区域不闭合：{model['id']}")
        generated = build_programmer_profiles(
            {"devices": [{**model, "source": "programmer"}]},
            programmer,
            ram,
            linker["source"],
        )
        if len(generated) != 1:
            raise ValueError(f"RISC-V 型号缺少 Programmer Flash 几何：{model['id']}")
        row = generated[0]
        if (
            len(row["flash"]) != 1
            or int(row["flash"][0]["address"]) != int(flash[0]["address"])
            or int(row["flash"][0]["size"]) != int(flash[0]["size"])
        ):
            raise ValueError(f"RISC-V linker 与 Programmer Flash 冲突：{model['id']}")
        row["source_kind"] = "programmer-and-firmware"
        row["memory"] = sorted(row["memory"], key=lambda item: int(item["address"]))
        profiles.append(row)
    return profiles


def _checked_slice(data: bytes, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ValueError(f"ELF {label} 越界")
    return data[offset : offset + size]


def _cstring(data: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        raise ValueError("ELF 字符串偏移越界")
    end = data.find(b"\0", offset)
    if end < 0:
        raise ValueError("ELF 字符串缺少终止符")
    return data[offset:end].decode("ascii")


def _elf_symbol(path: Path, name: str) -> bytes:
    data = path.read_bytes()
    if len(data) < ELF_HEADER.size:
        raise ValueError(f"FLM 不是完整 ELF：{path}")
    header = ELF_HEADER.unpack_from(data)
    ident = header[0]
    if ident[:4] != b"\x7fELF" or ident[4] != 1 or ident[5] != 1:
        raise ValueError(f"FLM 必须是小端 ELF32：{path}")
    section_offset = header[6]
    section_entry_size = header[11]
    section_count = header[12]
    if section_entry_size < SECTION_HEADER.size or section_count == 0:
        raise ValueError(f"FLM 缺少 ELF section：{path}")
    sections = [
        SECTION_HEADER.unpack_from(data, section_offset + index * section_entry_size)
        for index in range(section_count)
        if _checked_slice(
            data,
            section_offset + index * section_entry_size,
            SECTION_HEADER.size,
            "section header",
        )
    ]
    for section in sections:
        section_type, offset, size, link, entry_size = (
            section[1],
            section[4],
            section[5],
            section[6],
            section[9],
        )
        if section_type != 2:
            continue
        if link >= len(sections) or entry_size < SYMBOL.size:
            raise ValueError(f"FLM symtab 无效：{path}")
        strings_section = sections[link]
        strings = _checked_slice(data, strings_section[4], strings_section[5], "strtab")
        symbols = _checked_slice(data, offset, size, "symtab")
        for symbol_offset in range(0, len(symbols) - SYMBOL.size + 1, entry_size):
            string_offset, value, symbol_size, _, _, section_index = SYMBOL.unpack_from(
                symbols, symbol_offset
            )
            if _cstring(strings, string_offset) != name:
                continue
            if section_index == 0 or section_index >= len(sections):
                raise ValueError(f"FLM 符号 {name} 未绑定有效 section：{path}")
            target = sections[section_index]
            relative = value - target[3]
            return _checked_slice(data, target[4] + relative, symbol_size, name)
    raise ValueError(f"FLM 缺少 {name} 符号：{path}")


def decode_flash_device(data: bytes) -> dict[str, object]:
    if len(data) < 168:
        raise ValueError("FlashDevice 结构过短")
    version = struct.unpack_from("<H", data, 0)[0]
    name = data[2:130].split(b"\0", 1)[0].decode("ascii")
    device_type = struct.unpack_from("<H", data, 130)[0]
    address, size, page_size, reserved = struct.unpack_from("<IIII", data, 132)
    empty_value = data[148]
    program_timeout, erase_timeout = struct.unpack_from("<II", data, 152)
    if not name or version == 0 or size == 0 or page_size == 0 or reserved != 0:
        raise ValueError("FlashDevice 固定字段无效")
    sectors = []
    for offset in range(160, len(data) - 7, 8):
        sector_size, sector_offset = struct.unpack_from("<II", data, offset)
        if sector_size == 0xFFFFFFFF and sector_offset == 0xFFFFFFFF:
            break
        if sector_size in {0, 0xFFFFFFFF} or sector_offset == 0xFFFFFFFF:
            raise ValueError("FlashDevice sector 表无效")
        sectors.append({"offset": sector_offset, "size": sector_size})
    if not sectors or sectors[0]["offset"] != 0:
        raise ValueError("FlashDevice sector 表必须从偏移 0 开始")
    if sectors != sorted(sectors, key=lambda item: int(item["offset"])):
        raise ValueError("FlashDevice sector 表未按偏移递增")
    return {
        "version": version,
        "name": name,
        "device_type": device_type,
        "address": address,
        "size": size,
        "page_size": page_size,
        "empty_value": empty_value,
        "program_timeout_ms": program_timeout,
        "erase_timeout_ms": erase_timeout,
        "sectors": sectors,
    }


def parse_flm(path: Path) -> dict[str, object]:
    return decode_flash_device(_elf_symbol(path, "FlashDevice"))


def normalize_memory(memory: dict[str, object]) -> dict[str, object]:
    name = str(memory.get("id") or memory.get("name") or "")
    if name.startswith("IROM"):
        kind = "flash"
    elif name.startswith("IRAM") or name in {
        "AXISRAM",
        "DTCMRAM",
        "ITCMRAM",
        "SRAM",
        "SRAM0",
        "SRAM1",
        "TCM",
    }:
        kind = "ram"
    else:
        raise ValueError(f"未知 PDSC 内存类型：{name}")
    address, size = int(memory["start"]), int(memory["size"])
    if address < 0 or size <= 0 or address + size > 1 << 32:
        raise ValueError(f"PDSC 内存范围无效：{name}")
    return {"name": name, "kind": kind, "address": address, "size": size}


def _pack_path(pdsc_root: Path, resource: dict[str, object], file: dict[str, object]) -> Path:
    pdsc = resource["source_pdsc"]
    relative_pdsc = PurePosixPath(str(pdsc["path"]))
    relative_file = PurePosixPath(str(file["path"]))
    if ".." in relative_pdsc.parts or ".." in relative_file.parts:
        raise ValueError("Pack 资源路径包含上级目录")
    return pdsc_root.joinpath(*relative_pdsc.parent.parts, *relative_file.parts)


def _flash_region(
    algorithm: dict[str, object],
    descriptor: dict[str, object],
    memory: dict[str, object] | None = None,
) -> dict[str, object]:
    algorithm_address, algorithm_size = int(algorithm["start"]), int(algorithm["size"])
    address = int(memory["address"]) if memory is not None else algorithm_address
    size = int(memory["size"]) if memory is not None else algorithm_size
    descriptor_address, descriptor_size = int(descriptor["address"]), int(descriptor["size"])
    conflicts = []
    resolutions = []
    if algorithm_address != descriptor_address:
        conflicts.append("address")
    if address + size > descriptor_address + descriptor_size:
        conflicts.append("size")
    capacity = re.search(r"(?<!\d)(\d+)\s*(KB|K|MB|M)(?![A-Z0-9])", str(descriptor.get("name", "")), re.IGNORECASE)
    if capacity is not None:
        scale = 1024 if capacity.group(2).upper() in {"K", "KB"} else 1024 * 1024
        if int(capacity.group(1)) * scale == size and "size" in conflicts:
            conflicts.remove("size")
            resolutions.append("flm-name-and-pdsc-supersede-embedded-size")
    sectors = [
        sector for sector in descriptor["sectors"] if int(sector["offset"]) < size
    ]
    if not sectors:
        raise ValueError("Flash 区域没有有效 sector 几何")
    return {
        "address": address,
        "size": size,
        "algorithm_address": algorithm_address,
        "algorithm_size": algorithm_size,
        "program_page_size": descriptor["page_size"],
        "erase_value": descriptor["empty_value"],
        "sectors": sectors,
        "descriptor_conflicts": conflicts,
        "descriptor_resolutions": resolutions,
    }


def build_outputs(
    models: dict[str, object],
    resources: dict[str, object],
    pdsc_root: Path,
    programmer: dict[str, object] | None = None,
    builder_root: Path | None = None,
    riscv: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    resources_by_device = {str(item["device"]): item for item in resources["devices"]}
    if len(resources_by_device) != len(resources["devices"]):
        raise ValueError("Pack 内存资源 device 重复")
    algorithms: dict[str, dict[str, object]] = {}
    profiles = []
    for device in sorted(resources_by_device):
        resource = resources_by_device[device]
        memory = sorted(
            (normalize_memory(item) for item in resource["memory"]),
            key=lambda item: (int(item["address"]), str(item["name"])),
        )
        algorithm_rows = []
        for item in resource["algorithm"]:
            if str(item.get("default", "1")) == "0":
                continue
            file = item.get("file")
            if not isinstance(file, dict):
                continue
            sha256 = str(file["sha256"])
            path = _pack_path(pdsc_root, resource, file)
            if not path.is_file() or common._sha256(path) != sha256:
                raise ValueError(f"FLM 文件缺失或哈希漂移：{path}")
            if sha256 not in algorithms:
                algorithms[sha256] = {
                    "sha256": sha256,
                    "size": path.stat().st_size,
                    "descriptor": parse_flm(path),
                    "sources": [],
                }
            source = {
                "pack": resource["source_pack_name"],
                "version": resource["source_pack_version"],
                "path": file["path"],
            }
            if source not in algorithms[sha256]["sources"]:
                algorithms[sha256]["sources"].append(source)  # type: ignore[union-attr]
            algorithm_rows.append((item, sha256, algorithms[sha256]["descriptor"]))
        flash_memory = [item for item in memory if item["kind"] == "flash"]
        flash = []
        for memory_region in flash_memory:
            memory_start = int(memory_region["address"])
            memory_end = memory_start + int(memory_region["size"])
            candidates = [
                row
                for row in algorithm_rows
                if int(row[0]["start"]) <= memory_start
                and memory_end <= int(row[0]["start"]) + int(row[0]["size"])
            ]
            if not candidates:
                raise ValueError(f"PDSC Flash 内存没有覆盖算法：{device}:{memory_region['name']}")
            smallest = min(int(row[0]["size"]) for row in candidates)
            candidates = [row for row in candidates if int(row[0]["size"]) == smallest]
            if len({str(row[1]) for row in candidates}) != 1:
                raise ValueError(f"PDSC Flash 内存匹配多个等价算法：{device}:{memory_region['name']}")
            item, sha256, descriptor = candidates[0]
            flash.append(
                {
                    "algorithm_sha256": sha256,
                    **_flash_region(item, descriptor, memory_region),  # type: ignore[arg-type]
                }
            )
        if not memory or not flash:
            raise ValueError(f"Pack device 缺少内存或 Flash 算法：{device}")
        profiles.append(
            {
                "device": device,
                "source_pack_name": resource["source_pack_name"],
                "source_pack_version": resource["source_pack_version"],
                "source_pdsc": resource["source_pdsc"],
                "memory": memory,
                "flash": sorted(flash, key=lambda item: int(item["address"])),
                "flash_status": (
                    "conflict"
                    if any(region["descriptor_conflicts"] for region in flash)
                    else "normalized"
                ),
            }
        )
    if programmer is not None:
        if builder_root is None:
            raise ValueError("Programmer 内存归一缺少 Builder 来源根目录")
        linker_matches = list(builder_root.rglob("gd32h77x_78x_flash.ld"))
        if len(linker_matches) != 1:
            raise ValueError("Builder H77 链接脚本必须唯一")
        linker = linker_matches[0]
        profiles.extend(
            build_programmer_profiles(
                models,
                programmer,
                parse_linker_ram(linker.read_text(encoding="utf-8")),
                {
                    "path": linker.relative_to(builder_root).as_posix(),
                    "sha256": common._sha256(linker),
                },
            )
        )
        if riscv is not None:
            profiles.extend(build_riscv_profiles(models, programmer, riscv))
    for algorithm in algorithms.values():
        algorithm["sources"] = sorted(
            algorithm["sources"],
            key=lambda source: (str(source["pack"]), str(source["version"]), str(source["path"])),
        )
    profiles_by_device = {str(profile["device"]): profile for profile in profiles}
    devices = []
    for model in models["devices"]:
        cmsis_devices = [str(device) for device in model["cmsis_devices"]]
        missing = [device for device in cmsis_devices if device not in profiles_by_device]
        if missing:
            raise ValueError(f"规范型号引用未知内存 profile：{model['id']}:{missing}")
        profile_ids = [*cmsis_devices]
        if str(model["id"]) in profiles_by_device and str(model["id"]) not in profile_ids:
            profile_ids.append(str(model["id"]))
        selected = [profiles_by_device[device] for device in profile_ids]
        devices.append(
            {
                "id": model["id"],
                "memory_status": "normalized" if selected else "missing",
                "flash_status": (
                    "conflict"
                    if any(profile["flash_status"] == "conflict" for profile in selected)
                    else "normalized"
                    if selected and all(profile["flash_status"] == "normalized" for profile in selected)
                    else "geometry-only"
                    if selected
                    else "missing"
                ),
                "profiles": profile_ids,
                "memory_regions": sum(len(profile["memory"]) for profile in selected),
                "flash_regions": sum(len(profile["flash"]) for profile in selected),
                "algorithms": sorted(
                    {
                        str(region["algorithm_sha256"])
                        for profile in selected
                        for region in profile["flash"]
                        if "algorithm_sha256" in region
                    }
                ),
            }
        )
    full = {
        "schema_version": 1,
        "algorithms": [algorithms[key] for key in sorted(algorithms)],
        "profiles": profiles,
        "devices": devices,
    }
    normalized = sum(device["memory_status"] == "normalized" for device in devices)
    flash_normalized = sum(device["flash_status"] == "normalized" for device in devices)
    flash_conflicts = sum(device["flash_status"] == "conflict" for device in devices)
    flash_geometry = sum(device["flash_status"] == "geometry-only" for device in devices)
    report = {
        "schema_version": 1,
        "summary": {
            "normalized_devices": len(devices),
            "devices_with_normalized_memory": normalized,
            "devices_without_memory_source": len(devices) - normalized,
            "devices_with_normalized_flash": flash_normalized,
            "devices_with_flash_source_conflict": flash_conflicts,
            "devices_with_flash_geometry_only": flash_geometry,
            "devices_without_flash_source": len(devices) - flash_normalized - flash_conflicts - flash_geometry,
            "cmsis_profiles": len(profiles),
            "unique_flash_algorithms": len(algorithms),
            "memory_regions": sum(len(profile["memory"]) for profile in profiles),
            "flash_regions": sum(len(profile["flash"]) for profile in profiles),
        },
        "algorithms": [
            {
                "sha256": algorithm["sha256"],
                "size": algorithm["size"],
                "name": algorithm["descriptor"]["name"],
                "address": algorithm["descriptor"]["address"],
                "device_size": algorithm["descriptor"]["size"],
                "page_size": algorithm["descriptor"]["page_size"],
                "sector_regions": len(algorithm["descriptor"]["sectors"]),
                "sources": algorithm["sources"],
            }
            for algorithm in full["algorithms"]
        ],
        "devices": devices,
    }
    return full, report


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models", type=Path, default=repo_root / "reports/gigadevice-models.json"
    )
    parser.add_argument(
        "--resources",
        type=Path,
        default=repo_root / "reports/gigadevice-pack-resources.json",
    )
    parser.add_argument(
        "--pdsc-root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/addon-packs-v1",
    )
    parser.add_argument(
        "--programmer-data",
        type=Path,
        default=repo_root / "reports/gigadevice-programmer-data.json",
    )
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
        "--riscv", type=Path, default=repo_root / "reports/gigadevice-riscv.json"
    )
    parser.add_argument(
        "--normalized-output",
        type=Path,
        default=repo_root / ".cache/normalized/gigadevice-memory.json",
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-memory.json"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    models = json.loads(args.models.read_text(encoding="utf-8"))
    builder_lock = json.loads(args.builder_lock.read_text(encoding="utf-8"))
    builder_root = gigadevice_builder.find_extracted_root(
        args.builder_cache, str(builder_lock["builder"]["sha256"])
    )
    full, report = build_outputs(
        models,
        json.loads(args.resources.read_text(encoding="utf-8")),
        args.pdsc_root,
        json.loads(args.programmer_data.read_text(encoding="utf-8")),
        builder_root,
        json.loads(args.riscv.read_text(encoding="utf-8")),
    )
    common._write_text_atomic(
        args.normalized_output,
        json.dumps(full, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    report["provenance"] = {
        "models": {"path": args.models.name, "sha256": common._sha256(args.models)},
        "resources": {
            "path": args.resources.name,
            "sha256": common._sha256(args.resources),
        },
        "programmer_data": {
            "path": args.programmer_data.name,
            "sha256": common._sha256(args.programmer_data),
        },
        "builder_lock": {
            "path": args.builder_lock.name,
            "sha256": common._sha256(args.builder_lock),
        },
        "riscv": {"path": args.riscv.name, "sha256": common._sha256(args.riscv)},
        "normalized_data": {
            "path": args.normalized_output.name,
            "sha256": common._sha256(args.normalized_output),
        },
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    if int(summary["normalized_devices"]) != int(
        models["summary"]["normalized_devices"]
    ):
        raise ValueError("内存归一结果未闭合全部规范设备")
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"内存与 Flash 归一报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        struct.error,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
