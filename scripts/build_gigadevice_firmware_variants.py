#!/usr/bin/env python3
"""按 CMSIS Pack 的设备 define 预处理宽松许可 Firmware 头文件。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import analyze_gigadevice_coverage as coverage
import build_gigadevice_firmware_ir as firmware_ir
import build_gigadevice_mcu_data as mcu_data
import compare_gigadevice_svd_headers as svd_compare
import extract_gigadevice_firmware_registers as register_extract
import gigadevice_sources as common
import index_gigadevice_firmware_headers as header_index


DEFINE_NAME_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)", re.MULTILINE)
DEFINE_COMMENT_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[^\r\n]*?"
    r"\bREG(?:8|16|32|64)\s*\([^\r\n]*?(/\*.*?\*/)[ \t]*$",
    re.MULTILINE,
)
IFNDEF_RE = re.compile(r"^\s*#\s*ifndef\s+([A-Za-z_]\w*)", re.MULTILINE)
SELECTOR_DIRECTIVE_RE = re.compile(
    r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|define|undef)\b.*$", re.MULTILINE
)
IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")
CORE_INCLUDE_RE = re.compile(r'#\s*include\s*[<"](core_[A-Za-z0-9_]+\.h)[>"]')
COMPAT_DEFINES = {"HXTAL_VALUE=0U"}
CORE_SHIM_VERSION = 1
CORE_SHIM = "/* 由脚本生成：厂商固件缺失的 CMSIS Core 占位头，仅用于宏预处理。 */\n"
SOURCE_ISSUE_FIELDS = (
    "series",
    "defines",
    "tree_sha256",
    "path",
    "sha256",
    "unresolved_registers",
    "unassigned_instances",
    "unassigned_registers",
    "invalid_fields",
)
RCU_ENUM_RE = re.compile(
    r"typedef\s+enum(?:\s+[A-Za-z_]\w*)?\s*\{(?P<body>[^}]*)\}\s*"
    r"(?P<kind>rcu_periph_enum|rcu_periph_reset_enum)\s*;",
)
RCU_ITEM_RE = re.compile(r"\b(RCU_[A-Z0-9_]+)\s*=\s*([^,}]+)")
DMAMUX_REQUEST_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+(DMA_REQUES(?:T|R)_([A-Z0-9_]+))[ \t]+"
    r"RM_CHXCFG_MUXID\([ \t]*(\d+)[uUlL]*[ \t]*\)[ \t]*$",
    re.MULTILINE,
)
DMAMUX_CHANNEL_RE = re.compile(r"\bDMAMUX_(?:MULTIPLEXER_CH|MUXCH)(\d+)\b")
DMAMUX_GENERATOR_CHANNEL_RE = re.compile(r"\bDMAMUX_RG_CH(\d+)CFG\b")
DMA_CHANNEL_ENUM_RE = re.compile(
    r"typedef\s+enum(?:\s+[A-Za-z_]\w*)?\s*\{(?P<body>[^}]*)\}\s*"
    r"dma_channel_enum\s*;"
)
DMA_CHANNEL_RE = re.compile(r"\bDMA_CH(\d+)\b")
MDMA_REQUEST_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+(MDMA_REQUEST_([A-Z0-9_]+))[ \t]+"
    r"CHXCTL1_TRIGSEL\([ \t]*(\d+)[uUlL]*[ \t]*\)[ \t]*$",
    re.MULTILINE,
)
MDMA_SOFTWARE_REQUEST_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+(MDMA_REQUEST_(SW))[ \t]+.*?"
    r"0x([0-9A-Fa-f]+)[uUlL]*\)?[ \t]*$",
    re.MULTILINE,
)
MDMA_CHANNEL_RE = re.compile(r"\bMDMA_CH(\d+)\b")
ENUM_RE = re.compile(r"typedef\s+enum(?:\s+[A-Za-z_]\w*)?\s*\{(?P<body>[^}]*)\}")
ENUM_ITEM_RE = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*([^,}]+)")
ARRAY_PARAMETER_ALIASES = {
    "CLAx": ("cla_periph",),
    "CMPx": ("cmp_periph",),
    "channel": ("channelx",),
    "chx": ("channelx",),
    "flty": ("filtery",),
    "mdma_chx": ("channelx",),
}
ARRAY_INDEX_PREFIXES = {
    ("AXIIM", "mportx"): "MASTER_PORT",
    ("AXIIM", "sportx"): "SLAVE_PORT",
    ("EXMC", "region"): "EXMC_BANK0_NORSRAM_REGION",
    ("RAMECCMU0", "rameccmu_monitor"): "RAMECCMU0_MONITOR",
    ("RTDEC", "rtdec_areax"): "RTDEC_AREA",
    ("SAI", "blocky"): "SAI_BLOCK",
    ("SYSCFG", "syscfg_timerx"): "SYSCFG_TIMER",
}
ARRAY_REGISTER_INDEX_PREFIXES = {
    ("EDIM_BISS", "EDIM_BISS_SnDATA0", "n"): "EDIM_BISS_SLAVE",
    ("EDIM_BISS", "EDIM_BISS_SnDATA1", "n"): "EDIM_BISS_SLAVE",
    ("EDIM_BISS", "EDIM_BISS_SnPDTCFG", "n"): "EDIM_BISS_SLAVE",
    ("EDIM_AFMT", "EDIM_AFMT_ENCRDATA", "m"): "EDIM_AFMT_RDATA",
    ("EDIM_AFMT", "EDIM_AFMT_ENCRDATA", "n"): "EDIM_AFMT_SLAVE",
    ("EDIM_TFMT", "EDIM_TFMT_RDATA", "x"): "EDIM_TFMT_RDATA",
    ("MFCOM", "MFCOM_S", "x"): "MFCOM_SHIFTER_",
    ("MFCOM", "MFCOM_TM", "x"): "MFCOM_TIMER_",
    ("NVMC", "NVMC_CNVM_ROBBADDRX", "x"): "NVMC_CNVM_ROBBADDR",
}
TZBMPC_COMMON_RANGE_RE = re.compile(
    r"for\s+TZBMPC0\s+and\s+TZBMPC1\s+block\s+position\s+number\s+is\s+0-(\d+)",
    re.IGNORECASE,
)
TZBMPC_RANGE_RE = re.compile(
    r"for\s+TZBMPC([23])\s+block\s+position\s+number\s+is\s+0-(\d+)"
    r"(?:\s+only\s+for\s+([A-Za-z0-9_]+))?",
    re.IGNORECASE,
)
TZBMPC_DIVISOR_RE = re.compile(r"\binteger\s*=\s*block_pos_num\s*/\s*(\d+)U?\s*;")


def classify_source_issue(
    issue: dict[str, object], expected: dict[str, object] | None
) -> str:
    if expected is None or expected.get("resolution") not in {"block", "prefer-pack-svd"}:
        return "unexpected"
    for field in SOURCE_ISSUE_FIELDS:
        default: object = [] if field.endswith("s") else None
        if issue.get(field, default) != expected.get(field, default):
            return "unexpected"
    return (
        "known-blocking"
        if expected.get("resolution") == "block"
        else "source-resolved"
    )


def _issue_key(data: dict[str, object]) -> tuple[str, tuple[str, ...], str]:
    raw_defines = data.get("defines")
    if not isinstance(raw_defines, list):
        raise ValueError("Firmware 已知问题缺少 defines")
    return (
        str(data.get("series", "")),
        tuple(map(str, raw_defines)),
        str(data.get("path", "")),
    )


def _known_source_issues(
    data: dict[str, object],
) -> dict[tuple[str, tuple[str, ...], str], dict[str, object]]:
    raw_conflicts = data.get("conflicts")
    if data.get("schema_version") != 1 or not isinstance(raw_conflicts, list):
        raise ValueError("Firmware 已知问题清单格式无效")
    result = {}
    for raw in raw_conflicts:
        if not isinstance(raw, dict):
            raise ValueError("Firmware 已知问题清单包含非法条目")
        for field in ("tree_sha256", "sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", str(raw.get(field, ""))) is None:
                raise ValueError(f"Firmware 已知问题缺少有效 {field}")
        if raw.get("resolution") not in {"block", "prefer-pack-svd"} or not str(raw.get("reason", "")).strip():
            raise ValueError("Firmware 已知问题必须明确标记解决策略和原因")
        key = _issue_key(raw)
        if key in result:
            raise ValueError(f"Firmware 已知问题重复：{key}")
        result[key] = raw
    return result


def active_definitions(original: str, preprocessed: str) -> str:
    names = set(DEFINE_NAME_RE.findall(original.replace("\\\n", "")))
    comments = dict(DEFINE_COMMENT_RE.findall(original.replace("\\\n", "")))
    active = {}
    for line in preprocessed.splitlines():
        match = DEFINE_NAME_RE.match(line)
        if match is not None and match.group(1) in names:
            name = match.group(1)
            active[name] = line.strip() + (f" {comments[name]}" if name in comments else "")
    return "".join(f"{active[name]}\n" for name in sorted(active))


def active_interrupts(preprocessed: str) -> list[dict[str, int | str]]:
    return sorted(
        (
            interrupt
            for interrupt in header_index.parse_header_facts(preprocessed)["interrupts"]
            if int(interrupt["value"]) >= 0
        ),
        key=lambda interrupt: (int(interrupt["value"]), str(interrupt["name"])),
    )


def parse_rcu_facts(preprocessed: str) -> dict[str, object]:
    result: dict[str, list[dict[str, int | str]]] = {"enable": [], "reset": []}
    expressions = {
        identifier: expression
        for enum_match in ENUM_RE.finditer(preprocessed)
        for identifier, expression in ENUM_ITEM_RE.findall(enum_match.group("body"))
    }
    values: dict[str, int] = {}
    pending = dict(expressions)
    while pending:
        resolved = {
            identifier: value
            for identifier, expression in pending.items()
            if (value := header_index._evaluate(expression, values)) is not None
        }
        if not resolved:
            break
        values.update(resolved)
        for identifier in resolved:
            del pending[identifier]
    seen_kinds = set()
    for match in RCU_ENUM_RE.finditer(preprocessed):
        kind = "reset" if match.group("kind") == "rcu_periph_reset_enum" else "enable"
        if kind in seen_kinds:
            raise ValueError(f"Firmware 当前分支包含多个 RCU {kind} 枚举")
        seen_kinds.add(kind)
        for identifier, expression in RCU_ITEM_RE.findall(match.group("body")):
            value = values.get(identifier)
            if value is None:
                raise ValueError(f"无法计算 RCU 枚举值：{identifier}={expression.strip()}")
            values[identifier] = value
            bit = value & 0x3F
            register_offset = value >> 6
            if bit >= 32 or register_offset % 4:
                raise ValueError(f"RCU 枚举编码无效：{identifier}={value:#x}")
            name = identifier.removeprefix("RCU_")
            if kind == "reset":
                if not name.endswith("RST"):
                    raise ValueError(f"RCU 复位枚举缺少 RST 后缀：{identifier}")
                name = name.removesuffix("RST")
            result[kind].append(
                {"name": name, "register_offset": register_offset, "bit": bit}
            )
        result[kind].sort(key=lambda item: (str(item["name"]), int(item["register_offset"]), int(item["bit"])))
    if "enable" not in seen_kinds or "reset" not in seen_kinds:
        raise ValueError("Firmware 当前分支缺少 RCU 外设使能或复位枚举")
    return result


def parse_dma_facts(macros: str, source: str) -> dict[str, object]:
    requests_by_name = {}
    for source_name, name, raw_request in DMAMUX_REQUEST_RE.findall(macros):
        row = {
            "name": name,
            "request": int(raw_request),
            "source_name": source_name,
        }
        previous = requests_by_name.get(name)
        if previous is not None and previous != row:
            raise ValueError(f"Firmware DMAMUX 请求名称冲突：{name}")
        requests_by_name[name] = row
    channels = sorted({int(value) for value in DMAMUX_CHANNEL_RE.findall(source)})
    generator_channels = sorted(
        {
            int(value)
            for value in DMAMUX_GENERATOR_CHANNEL_RE.findall(macros + "\n" + source)
        }
    )
    channel_enums = DMA_CHANNEL_ENUM_RE.findall(source)
    if len(channel_enums) > 1:
        raise ValueError("Firmware 当前分支包含多个 DMA 通道枚举")
    dma_channels = sorted(
        {int(value) for value in DMA_CHANNEL_RE.findall(channel_enums[0])}
    ) if channel_enums else []
    requests = sorted(
        requests_by_name.values(),
        key=lambda row: (int(row["request"]), str(row["name"])),
    )
    if bool(channels) != bool(requests):
        raise ValueError("Firmware DMAMUX 请求表与通道表不完整")
    if generator_channels and generator_channels != list(range(len(generator_channels))):
        raise ValueError("Firmware DMAMUX 请求生成器通道编号不连续")
    if dma_channels and dma_channels != list(range(len(dma_channels))):
        raise ValueError("Firmware DMA 通道编号不连续")
    return {
        "kind": "dmamux" if requests else "fixed",
        "dma_channels": dma_channels,
        "dmamux_channels": channels,
        "dmamux_generator_channels": generator_channels,
        "requests": requests,
    }


def parse_mdma_facts(macros: str, source: str) -> dict[str, object]:
    requests_by_name = {}
    for source_name, name, raw_request in MDMA_REQUEST_RE.findall(macros):
        requests_by_name[name] = {
            "kind": "hardware",
            "name": name,
            "request": int(raw_request),
            "source_name": source_name,
        }
    for source_name, name, raw_request in MDMA_SOFTWARE_REQUEST_RE.findall(macros):
        requests_by_name[name] = {
            "kind": "software",
            "name": name,
            "request": int(raw_request, 16),
            "source_name": source_name,
        }
    channels = sorted({int(value) for value in MDMA_CHANNEL_RE.findall(source)})
    if channels != list(range(len(channels))):
        raise ValueError("Firmware MDMA 通道编号不连续")
    requests = sorted(
        requests_by_name.values(),
        key=lambda row: (int(row["request"]), str(row["name"])),
    )
    if not channels or not requests:
        raise ValueError("Firmware MDMA 请求表或通道表为空")
    return {"channels": channels, "requests": requests}


def parse_tzbmpc_instance_array_bounds(
    source: str,
    defines: list[str],
    source_info: dict[str, str],
) -> list[dict[str, object]]:
    common = TZBMPC_COMMON_RANGE_RE.search(source)
    ranges = TZBMPC_RANGE_RE.findall(source)
    if common is None and not ranges:
        return []
    divisors = {int(value) for value in TZBMPC_DIVISOR_RE.findall(source)}
    if common is None or not ranges or divisors != {32}:
        raise ValueError("Firmware TZBMPC 数组范围证据不完整")
    selectors = {define.split("=", 1)[0].casefold() for define in defines}
    maxima: dict[str, set[int]] = {
        "TZBMPC0": {int(common.group(1))},
        "TZBMPC1": {int(common.group(1))},
    }
    for raw_instance, raw_maximum, qualifier in ranges:
        if qualifier and qualifier.casefold() not in selectors:
            continue
        maxima.setdefault(f"TZBMPC{raw_instance}", set()).add(int(raw_maximum))
    if set(maxima) != {"TZBMPC0", "TZBMPC1", "TZBMPC2", "TZBMPC3"}:
        raise ValueError("Firmware TZBMPC 当前芯片缺少实例范围")
    rows = []
    for instance, candidates in sorted(maxima.items()):
        if len(candidates) != 1:
            raise ValueError(f"Firmware {instance} 数组范围冲突")
        maximum = next(iter(candidates))
        if maximum < 31 or maximum % 32 != 31:
            raise ValueError(f"Firmware {instance} block 范围无法整除为寄存器")
        rows.append(
            {
                "instance": instance,
                "register": "TZPCU_TZBMPC_VEC",
                "parameter": "y",
                "end": maximum // 32,
                "bound_evidence": "source-block-position-range",
                "source": dict(source_info),
            }
        )
    return rows


def tzbmpc_instance_array_bounds(
    root: Path, defines: list[str]
) -> list[dict[str, object]]:
    candidates = []
    for path in sorted(root.rglob("*tzpcu.c")):
        if not any(part.casefold() == "firmware" for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        source_info = {
            "path": path.relative_to(root).as_posix(),
            "sha256": common._sha256(path),
        }
        rows = parse_tzbmpc_instance_array_bounds(source, defines, source_info)
        if not rows:
            continue
        if coverage.source_license(source[:16384]) not in header_index.PERMISSIVE_LICENSES:
            raise ValueError(f"Firmware TZBMPC 范围来源许可不明确：{source_info['path']}")
        candidates.append(rows)
    if not candidates:
        return []
    encoded = {
        json.dumps(
            [{key: value for key, value in row.items() if key != "source"} for row in rows],
            ensure_ascii=False,
            sort_keys=True,
        )
        for rows in candidates
    }
    if len(encoded) != 1:
        raise ValueError("Firmware TZBMPC 数组范围来源冲突")
    return candidates[0]


def typed_enum_candidates(source: str) -> dict[str, list[tuple[str, int]]]:
    return register_extract.typed_enum_candidates(source)


def apply_typed_enum_bounds(
    registers: list[dict[str, object]],
    candidates: dict[str, list[tuple[str, int]]],
    register_blocks: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    result = [
        {
            **register,
            **(
                {
                    "array_parameters": {
                        parameter: dict(bounds)
                        for parameter, bounds in register["array_parameters"].items()
                    }
                }
                if register.get("array_parameters")
                else {}
            ),
        }
        for register in registers
    ]
    for register in result:
        for parameter, bounds in register.get("array_parameters", {}).items():
            names = [parameter, *ARRAY_PARAMETER_ALIASES.get(parameter, ())]
            choices = [choice for name in names for choice in candidates.get(name, [])]
            block = str((register_blocks or {}).get(str(register["name"]), ""))
            block_stem = re.sub(r"\d+$", "", block.split("_", 1)[0]).casefold()
            relevant = [
                choice
                for choice in choices
                if block_stem and block_stem in choice[0].casefold()
            ]
            if relevant:
                choices = relevant
            ends = {end for _, end in choices}
            if "end" not in bounds and "indices" not in bounds and len(ends) == 1:
                enum_types = sorted(
                    enum_type for enum_type, end in choices if end in ends
                )
                bounds.update(
                    {
                        "end": next(iter(ends)),
                        "bound_evidence": "typed-enum:" + "+".join(enum_types),
                    }
                )
    return result


def infer_typed_enum_bounds(
    registers: list[dict[str, object]],
    source: str,
    register_blocks: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    return apply_typed_enum_bounds(
        registers, typed_enum_candidates(source), register_blocks
    )


def apply_source_array_bounds(
    registers: list[dict[str, object]],
    source_registers: list[dict[str, object]],
) -> list[dict[str, object]]:
    evidence: dict[tuple[str, str], dict[str, object]] = {}
    conflicts: set[tuple[str, str]] = set()
    for register in source_registers:
        for parameter, bounds in register.get("array_parameters", {}).items():
            if "end" not in bounds and "indices" not in bounds:
                continue
            key = (str(register["name"]), str(parameter))
            candidate = {
                field: bounds[field]
                for field in ("start", "end", "stride", "bound_evidence")
                if field in bounds
            }
            previous = evidence.get(key)
            if previous is not None and previous != candidate:
                conflicts.add(key)
            evidence[key] = candidate
    result = [
        {
            **register,
            **(
                {
                    "array_parameters": {
                        parameter: dict(bounds)
                        for parameter, bounds in register["array_parameters"].items()
                    }
                }
                if register.get("array_parameters")
                else {}
            ),
        }
        for register in registers
    ]
    for register in result:
        for parameter, bounds in register.get("array_parameters", {}).items():
            key = (str(register["name"]), str(parameter))
            candidate = evidence.get(key)
            if candidate is None or key in conflicts:
                continue
            if int(candidate["stride"]) != int(bounds["stride"]):
                raise ValueError(f"Firmware 数组范围步长冲突：{key[0]}.{key[1]}")
            old_start = int(bounds["start"])
            new_start = int(candidate["start"])
            register["offset"] = int(register["offset"]) + (
                new_start - old_start
            ) * int(bounds["stride"])
            bounds.update(candidate)
    return result


def apply_source_loop_bounds(
    registers: list[dict[str, object]], rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    evidence: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        source = row.get("source")
        if (
            not isinstance(source, dict)
            or re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))) is None
            or not str(source.get("path", ""))
        ):
            raise ValueError("Firmware 源文件循环证据缺少来源路径或哈希")
        evidence.setdefault(str(row["register"]), []).append(row)
    result = [
        {
            **register,
            **(
                {
                    "array_parameters": {
                        parameter: dict(bounds)
                        for parameter, bounds in register["array_parameters"].items()
                    }
                }
                if register.get("array_parameters")
                else {}
            ),
        }
        for register in registers
    ]
    for register in result:
        candidates = evidence.get(str(register["name"]), [])
        if not candidates:
            continue
        ranges = {(int(row["start"]), int(row["end"])) for row in candidates}
        if len(ranges) != 1:
            raise ValueError(f"Firmware 源文件循环数组范围冲突：{register['name']}")
        unbounded = [
            bounds
            for bounds in register.get("array_parameters", {}).values()
            if "end" not in bounds and "indices" not in bounds
        ]
        if len(unbounded) != 1:
            continue
        start, end = next(iter(ranges))
        bounds = unbounded[0]
        old_start = int(bounds["start"])
        register["offset"] = int(register["offset"]) + (start - old_start) * int(
            bounds["stride"]
        )
        paths = sorted({str(row["source"]["path"]) for row in candidates})
        bounds.update(
            {
                "start": start,
                "end": end,
                "bound_evidence": "source-loop:" + "+".join(paths),
            }
        )
    return result


def apply_indexed_identifier_bounds(
    registers: list[dict[str, object]],
    groups: dict[str, list[int]],
    register_blocks: dict[str, object],
) -> list[dict[str, object]]:
    result = [
        {
            **register,
            **(
                {
                    "array_parameters": {
                        parameter: dict(bounds)
                        for parameter, bounds in register["array_parameters"].items()
                    }
                }
                if register.get("array_parameters")
                else {}
            ),
        }
        for register in registers
    ]
    for register in result:
        block = str(register_blocks.get(str(register["name"]), ""))
        register_name = str(register["name"])
        for parameter, bounds in register.get("array_parameters", {}).items():
            if "end" in bounds or "indices" in bounds:
                continue
            prefix = ARRAY_INDEX_PREFIXES.get((block, str(parameter)))
            for (candidate_block, name_prefix, candidate_parameter), candidate in (
                ARRAY_REGISTER_INDEX_PREFIXES.items()
            ):
                if (
                    block == candidate_block
                    and register_name.startswith(name_prefix)
                    and str(parameter) == candidate_parameter
                ):
                    prefix = candidate
                    break
            indices = list(map(int, groups.get(prefix, []))) if prefix else []
            if not indices:
                continue
            old_start = int(bounds["start"])
            new_start = indices[0]
            register["offset"] = int(register["offset"]) + (
                new_start - old_start
            ) * int(bounds["stride"])
            bounds.update({"start": new_start, "bound_evidence": f"indexed-identifiers:{prefix}"})
            if indices == list(range(indices[0], indices[-1] + 1)):
                bounds["end"] = indices[-1]
            else:
                bounds["indices"] = indices
    return result


def apply_dma_array_bounds(
    registers: list[dict[str, object]], dma: dict[str, object] | None
) -> list[dict[str, object]]:
    result = [
        {
            **register,
            **(
                {
                    "array_parameters": {
                        parameter: dict(bounds)
                        for parameter, bounds in register["array_parameters"].items()
                    }
                }
                if register.get("array_parameters")
                else {}
            ),
        }
        for register in registers
    ]
    if dma is None:
        return result
    topology = {
        "DMA_CHCTL": "dma_channels",
        "DMA_CHCNT": "dma_channels",
        "DMA_CHPADDR": "dma_channels",
        "DMA_CHMADDR": "dma_channels",
        "DMAMUX_RM_CHXCFG": "dmamux_channels",
        "DMAMUX_RG_CHXCFG": "dmamux_generator_channels",
    }
    for register in result:
        source = topology.get(str(register["name"]))
        bounds = register.get("array_parameters", {}).get("channel")
        channels = dma.get(source, []) if source is not None else []
        if (
            not isinstance(bounds, dict)
            or "end" in bounds
            or "indices" in bounds
            or not isinstance(channels, list)
        ):
            continue
        indices = list(map(int, channels))
        if not indices or indices != list(range(indices[0], indices[-1] + 1)):
            continue
        old_start = int(bounds["start"])
        register["offset"] = int(register["offset"]) + (
            indices[0] - old_start
        ) * int(bounds["stride"])
        bounds.update(
            {
                "start": indices[0],
                "end": indices[-1],
                "bound_evidence": f"dma-topology:{source}",
            }
        )
    return result


def _defines(value: str) -> list[str]:
    return sorted(
        {
            token.removeprefix("-D")
            for token in shlex.split(value)
            if token.removeprefix("-D")
        }
    )


def collect_variants(
    resources: dict[str, object],
    available_series: set[str],
    selectors_by_series: dict[str, list[str]] | None = None,
    models: dict[str, object] | None = None,
    device_selectors: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    selectors_by_series = selectors_by_series or {}
    device_selectors = device_selectors or {}
    raw_devices = resources.get("devices")
    if not isinstance(raw_devices, list):
        raise ValueError("Pack 资源报告缺少 devices")
    raw_models = models.get("devices") if models is not None else []
    if not isinstance(raw_models, list):
        raise ValueError("规范型号报告缺少 devices")
    canonical_by_cmsis = {}
    for model in raw_models:
        if not isinstance(model, dict) or not isinstance(model.get("cmsis_devices"), list):
            raise ValueError("规范型号报告的 cmsis_devices 无效")
        for cmsis_device in model["cmsis_devices"]:
            alias = str(cmsis_device)
            if alias in canonical_by_cmsis:
                raise ValueError(f"CMSIS 设备别名重复：{alias}")
            canonical_by_cmsis[alias] = str(model["id"])
    grouped: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    missing = []

    def add(name: str, series: str, defines: list[str]) -> None:
        selector_candidates = [
            selector
            for selector in selectors_by_series.get(series, [])
            if selector.isalnum()
            and (
                name.casefold().startswith(selector.casefold())
                or (
                    len(selector) == len(name)
                    and all(
                        expected == "x" or expected == actual
                        for expected, actual in zip(
                            selector.casefold(), name.casefold(), strict=True
                        )
                    )
                )
            )
        ]
        if selector_candidates:
            longest = max(map(len, selector_candidates))
            best = sorted(
                {selector for selector in selector_candidates if len(selector) == longest}
            )
            if len(best) != 1:
                missing.append({"device": name, "reason": "ambiguous-device-selector"})
                return
            defines = sorted(set(defines) | {best[0]})
        defines = sorted(set(defines) | set(device_selectors.get(name, [])))
        grouped.setdefault((series, tuple(defines)), set()).add(name)

    for device in raw_devices:
        if not isinstance(device, dict):
            raise ValueError("Pack 资源报告包含非法设备")
        source_name = str(device["device"])
        name = canonical_by_cmsis.get(source_name, source_name)
        pack_name = str(device["source_pack_name"])
        try:
            svd_compare.firmware_series_for_pack(pack_name)
        except ValueError:
            missing.append({"device": name, "reason": "invalid-pack-name"})
            continue
        series = mcu_data.choose_firmware_series(
            name, [{"name": pack_name}], available_series
        )
        if series is None:
            missing.append({"device": name, "reason": "firmware-series-not-matched"})
            continue
        raw_compile = device.get("compile")
        if not isinstance(raw_compile, list):
            raise ValueError(f"Pack 设备 {name} 的 compile 记录无效")
        defines = sorted(
            {
                define
                for entry in raw_compile
                if isinstance(entry, dict)
                for define in _defines(str(entry.get("define", "")))
            }
        )
        add(name, series, defines)

    if models is not None:
        for model in raw_models:
            if not isinstance(model, dict):
                raise ValueError("规范型号报告包含非法设备")
            cmsis_devices = model.get("cmsis_devices")
            source_packs = model.get("source_packs")
            if not isinstance(cmsis_devices, list) or not isinstance(source_packs, list):
                raise ValueError("规范型号报告的 cmsis_devices/source_packs 无效")
            if cmsis_devices:
                continue
            name = str(model["id"])
            series = mcu_data.choose_firmware_series(
                name, source_packs, available_series
            )
            if series is None:
                missing.append(
                    {"device": name, "reason": "firmware-series-not-matched"}
                )
                continue
            add(name, series, [])

    variants = []
    for (series, defines), devices in sorted(grouped.items()):
        identity = json.dumps(
            {"series": series, "defines": defines},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        variants.append(
            {
                "id": f"{series.lower()}-{hashlib.sha256(identity).hexdigest()[:16]}",
                "series": series,
                "defines": list(defines),
                "devices": sorted(devices),
            }
        )
    missing = [
        {"device": device, "reason": reason}
        for device, reason in sorted(
            {(row["device"], row["reason"]) for row in missing}
        )
    ]
    return variants, missing


def validated_device_selectors(
    config: dict[str, object],
    models: dict[str, object],
    available_series: set[str],
    selectors_by_series: dict[str, list[str]],
    device_headers: dict[str, dict[str, str]],
) -> dict[str, list[str]]:
    raw_mappings = config.get("mappings")
    raw_models = models.get("devices")
    if not isinstance(raw_mappings, list) or not isinstance(raw_models, list):
        raise ValueError("Builder 器件选择器映射结构无效")
    model_by_id = {str(model["id"]): model for model in raw_models}
    result: dict[str, list[str]] = {}
    for mapping in raw_mappings:
        if not isinstance(mapping, dict):
            raise ValueError("Builder 器件选择器映射记录无效")
        device = str(mapping["device"])
        requested_series = str(mapping["series"])
        series_matches = [
            series
            for series in available_series
            if series.casefold() == requested_series.casefold()
        ]
        if device not in model_by_id or len(series_matches) != 1:
            raise ValueError(f"Builder 器件选择器映射 device/series 不存在：{device}")
        series = series_matches[0]
        model = model_by_id[device]
        assert isinstance(model, dict)
        source_packs = model.get("source_packs", [])
        if not isinstance(source_packs, list) or mcu_data.choose_firmware_series(
            device, source_packs, available_series
        ) != series:
            raise ValueError(f"Builder 器件选择器映射系列不匹配：{device}")
        selector_matches = [
            selector
            for selector in selectors_by_series.get(series, [])
            if selector.casefold() == str(mapping["selector"]).casefold()
        ]
        if len(selector_matches) != 1:
            raise ValueError(f"Builder 器件选择器不存在：{device}")
        selector = selector_matches[0]
        if sorted(device.casefold()) != sorted(selector.casefold()):
            raise ValueError(f"Builder 器件选择器不是同一型号编码重排：{device}")
        if str(mapping["device_header_sha256"]) != device_headers[series]["sha256"]:
            raise ValueError(f"Builder 器件选择器头文件哈希不一致：{device}")
        matrix_paths = mapping.get("matrix_paths")
        if not isinstance(matrix_paths, list) or not matrix_paths:
            raise ValueError(f"Builder 器件选择器缺少矩阵证据：{device}")
        if device in result:
            raise ValueError(f"Builder 器件选择器重复：{device}")
        result[device] = [selector]
    return result


def include_directories(root: Path) -> list[Path]:
    return sorted(
        {
            path.parent
            for path in root.rglob("*.h")
            if not any(part.casefold() == "examples" for part in path.parts)
        },
        key=lambda path: path.as_posix().casefold(),
    )


def find_device_header(root: Path, sha256: str) -> dict[str, str]:
    matches = sorted(
        path
        for path in root.rglob("*.h")
        if common._sha256(path) == sha256
    )
    if not matches:
        raise ValueError(f"Firmware 器件头文件哈希未命中：{sha256}")
    return {
        "path": matches[0].relative_to(root).as_posix(),
        "sha256": sha256,
    }


def missing_core_headers(root: Path) -> list[str]:
    existing = {path.name for path in root.rglob("core_*.h")}
    referenced = {
        name
        for path in root.rglob("*.h")
        if not any(part.casefold() == "examples" for part in path.parts)
        for name in CORE_INCLUDE_RE.findall(
            path.read_text(encoding="utf-8", errors="ignore")
        )
    }
    return sorted(referenced - existing)


def ensure_core_shims(root: Path, names: list[str]) -> Path:
    for name in names:
        path = root / name
        if path.is_file():
            if path.read_text(encoding="utf-8") != CORE_SHIM:
                raise ValueError(f"CMSIS Core shim 内容不一致：{path}")
        else:
            common._write_text_atomic(path, CORE_SHIM)
    return root


def cmsis_guard_defines(root: Path) -> list[str]:
    guards = set()
    for path in root.rglob("core_*.h"):
        if any(part.casefold() == "examples" for part in path.parts):
            continue
        guards.update(
            name
            for name in IFNDEF_RE.findall(
                path.read_text(encoding="utf-8", errors="ignore")
            )
            if name.startswith("__CORE_")
        )
    return sorted(guards)


def selector_spellings(root: Path) -> dict[str, list[str]]:
    result: dict[str, set[str]] = {}
    for path in root.rglob("*.h"):
        if any(part.casefold() == "examples" for part in path.parts):
            continue
        text = header_index.COMMENT_RE.sub(
            "", path.read_text(encoding="utf-8", errors="ignore")
        )
        text = re.sub(r"\\\r?\n", " ", text)
        for line in SELECTOR_DIRECTIVE_RE.findall(text):
            for name in IDENTIFIER_RE.findall(line):
                if name.casefold().startswith("gd32"):
                    result.setdefault(name.casefold(), set()).add(name)
    return {name: sorted(values) for name, values in sorted(result.items())}


def preprocessor_defines(
    variant: dict[str, object],
    cmsis_guards: list[str],
    spellings: dict[str, list[str]],
) -> list[str]:
    def canonical(name: str) -> str:
        candidates = spellings.get(name.casefold(), [])
        if name in candidates:
            return name
        return candidates[0] if len(candidates) == 1 else name

    result = set(cmsis_guards) | COMPAT_DEFINES
    for raw in variant["defines"]:
        name, separator, value = str(raw).partition("=")
        result.add(canonical(name) + separator + value)
    series = str(variant["series"])
    if series.casefold().startswith("gd32"):
        result.add(canonical(series))
    return sorted(result)


def _preprocess(
    compiler: Path,
    compiler_identity: str,
    headers: list[tuple[Path, str]],
    tree_sha256: str,
    defines: list[str],
    core_shims: list[str],
    include_dirs: list[Path],
    cache_dir: Path,
    mode: str = "macros",
) -> str:
    if mode not in {"macros", "source"}:
        raise ValueError(f"未知 Firmware 预处理模式：{mode}")
    key_data = {
        "cache_version": 3,
        "core_shim_version": CORE_SHIM_VERSION,
        "core_shims": core_shims,
        "compiler": compiler_identity,
        "headers": [sha256 for _, sha256 in headers],
        "tree_sha256": tree_sha256,
        "defines": defines,
    }
    if mode != "macros":
        key_data["mode"] = mode
    key = hashlib.sha256(
        json.dumps(key_data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache = cache_dir / f"{key}.json"
    if cache.is_file():
        data = json.loads(cache.read_text(encoding="utf-8"))
        output = str(data.get("output", ""))
        if data.get("key") != key or hashlib.sha256(output.encode()).hexdigest() != data.get(
            "output_sha256"
        ):
            raise ValueError(f"Firmware 预处理缓存损坏：{cache}")
        return output

    source = "".join(
        f'#include "{path.as_posix()}"\n' for path, _ in headers
    )
    command = [
        str(compiler),
        "-E",
        "-dM" if mode == "macros" else "-P",
        "-w",
        "-x",
        "c",
        "-std=c11",
        *(f"-D{define}" for define in defines),
        *(argument for directory in include_dirs for argument in ("-I", str(directory))),
        "-",
    ]
    result = subprocess.run(
        command,
        input=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"Firmware 头文件批量预处理失败：headers={len(headers)}\n"
            f"defines={' '.join(defines)}\n"
            + (result.stderr or result.stdout).strip()[-4000:]
        )
    output = result.stdout
    cache_dir.mkdir(parents=True, exist_ok=True)
    common._write_text_atomic(
        cache,
        json.dumps(
            {
                "schema_version": 1,
                "key": key,
                "mode": mode,
                "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
                "output": output,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    return output


def _variant_ir(
    variant: dict[str, object],
    library: dict[str, object],
    root: Path,
    compiler: Path,
    compiler_identity: str,
    spellings: dict[str, list[str]],
    cache_dir: Path,
    device_header: dict[str, object] | None = None,
) -> dict[str, object]:
    core_shims = missing_core_headers(root)
    include_dirs = include_directories(root)
    if core_shims:
        shim_root = ensure_core_shims(
            cache_dir / f"shims-v{CORE_SHIM_VERSION}" / str(library["tree_sha256"])[:16],
            core_shims,
        )
        include_dirs.insert(0, shim_root)
    internal_defines = preprocessor_defines(
        variant, cmsis_guard_defines(root), spellings
    )
    originals = []
    source_headers = []
    headers = library["register_headers"]
    assert isinstance(headers, list)
    for header in headers:
        assert isinstance(header, dict)
        path = root.joinpath(*Path(str(header["path"])).parts)
        if not path.is_file() or common._sha256(path) != header["sha256"]:
            raise ValueError(f"Firmware 寄存器头文件来源哈希不一致：{path}")
        original = path.read_text(encoding="utf-8", errors="ignore")
        originals.append((header, original))
        source_headers.append((path, str(header["sha256"])))

    output = _preprocess(
        compiler,
        compiler_identity,
        source_headers,
        str(library["tree_sha256"]),
        internal_defines,
        core_shims,
        include_dirs,
        cache_dir,
    )

    rcu_headers = [
        (path, sha256)
        for path, sha256 in source_headers
        if path.name.casefold().endswith("_rcu.h")
    ]
    if len(rcu_headers) > 1:
        raise ValueError(f"Firmware 变体包含多个 RCU 头文件：{variant['id']}")
    rcu = None
    if rcu_headers:
        rcu_output = _preprocess(
            compiler,
            compiler_identity,
            rcu_headers,
            str(library["tree_sha256"]),
            internal_defines,
            core_shims,
            include_dirs,
            cache_dir,
            mode="source",
        )
        rcu = {
            "source": {
                "path": rcu_headers[0][0].relative_to(root).as_posix(),
                "sha256": rcu_headers[0][1],
            },
            **parse_rcu_facts(rcu_output),
        }

    dma_headers = [
        (path, sha256)
        for path, sha256 in source_headers
        if path.name.casefold().endswith("_dma.h")
    ]
    if len(dma_headers) > 1:
        raise ValueError(f"Firmware 变体包含多个 DMA 头文件：{variant['id']}")
    dma = None
    if dma_headers:
        dma_path, dma_sha256 = dma_headers[0]
        dma_source = _preprocess(
            compiler,
            compiler_identity,
            dma_headers,
            str(library["tree_sha256"]),
            internal_defines,
            core_shims,
            include_dirs,
            cache_dir,
            mode="source",
        )
        dma_original = next(
            original
            for header, original in originals
            if header["sha256"] == dma_sha256
        )
        try:
            dma_facts = parse_dma_facts(
                active_definitions(dma_original, output), dma_source
            )
        except ValueError as error:
            raise ValueError(f"Firmware DMA 变体 {variant['id']}：{error}") from error
        dma = {
            "source": {
                "path": dma_path.relative_to(root).as_posix(),
                "sha256": dma_sha256,
            },
            **dma_facts,
        }

    mdma_headers = [
        (path, sha256)
        for path, sha256 in source_headers
        if path.name.casefold().endswith("_mdma.h")
    ]
    if len(mdma_headers) > 1:
        raise ValueError(f"Firmware 变体包含多个 MDMA 头文件：{variant['id']}")
    mdma = None
    if mdma_headers:
        mdma_path, mdma_sha256 = mdma_headers[0]
        mdma_source = _preprocess(
            compiler,
            compiler_identity,
            mdma_headers,
            str(library["tree_sha256"]),
            internal_defines,
            core_shims,
            include_dirs,
            cache_dir,
            mode="source",
        )
        mdma_original = next(
            original
            for header, original in originals
            if header["sha256"] == mdma_sha256
        )
        try:
            mdma_facts = parse_mdma_facts(
                active_definitions(mdma_original, output), mdma_source
            )
        except ValueError as error:
            raise ValueError(f"Firmware MDMA 变体 {variant['id']}：{error}") from error
        mdma = {
            "source": {
                "path": mdma_path.relative_to(root).as_posix(),
                "sha256": mdma_sha256,
            },
            **mdma_facts,
        }

    interrupts = []
    base_addresses = {}
    device_header_source = None
    if device_header is not None:
        device_path = root.joinpath(*Path(str(device_header["path"])).parts)
        if not device_path.is_file() or common._sha256(device_path) != device_header["sha256"]:
            raise ValueError(f"Firmware 器件头文件来源哈希不一致：{device_path}")
        device_output = _preprocess(
            compiler,
            compiler_identity,
            [(device_path, str(device_header["sha256"]))],
            str(library["tree_sha256"]),
            internal_defines,
            core_shims,
            include_dirs,
            cache_dir,
            mode="source",
        )
        device_macros = _preprocess(
            compiler,
            compiler_identity,
            [(device_path, str(device_header["sha256"]))],
            str(library["tree_sha256"]),
            internal_defines,
            core_shims,
            include_dirs,
            cache_dir,
        )
        device_facts = header_index.parse_header_facts(device_output)
        interrupts = sorted(
            (
                interrupt
                for interrupt in device_facts["interrupts"]
                if int(interrupt["value"]) >= 0
            ),
            key=lambda interrupt: (int(interrupt["value"]), str(interrupt["name"])),
        )
        base_addresses = header_index.parse_header_facts(device_macros)[
            "base_addresses"
        ]
        device_header_source = {
            "path": device_header["path"],
            "sha256": device_header["sha256"],
        }

    values = register_extract.resolve_library_integer_definitions([output])
    active_headers = []
    for header, original in originals:
        active = active_definitions(original, output)
        facts = register_extract.parse_register_facts(active, values)
        facts["registers"] = apply_source_array_bounds(
            facts["registers"], header.get("registers", [])
        )
        facts["registers"] = apply_source_loop_bounds(
            facts["registers"], library.get("source_loop_bounds", [])
        )
        groups = header.get("array_index_groups", {})
        if not isinstance(groups, dict):
            raise ValueError(f"Firmware 数组编号组格式无效：{header['path']}")
        value_groups = header.get("array_value_groups", {})
        if not isinstance(value_groups, dict):
            raise ValueError(f"Firmware 数组编号值组格式无效：{header['path']}")
        facts["registers"] = apply_indexed_identifier_bounds(
            facts["registers"], {**groups, **value_groups}, facts["register_blocks"]
        )
        facts["registers"] = apply_dma_array_bounds(facts["registers"], dma)
        if any(
            "end" not in bounds and "indices" not in bounds
            for register in facts["registers"]
            for bounds in register.get("array_parameters", {}).values()
        ):
            candidates = header.get("array_bound_candidates")
            if candidates is None:
                candidates = typed_enum_candidates(original)
            if not isinstance(candidates, dict):
                raise ValueError(f"Firmware 数组枚举候选格式无效：{header['path']}")
            facts["registers"] = apply_typed_enum_bounds(
                facts["registers"],
                candidates,
                facts["register_blocks"],
            )
        active_headers.append(
            {
                "path": header["path"],
                "sha256": header["sha256"],
                "license": header["license"],
                **facts,
            }
        )
    ir = firmware_ir.build_library_ir(
        {
            **library,
            "register_headers": active_headers,
            "instance_array_bounds": tzbmpc_instance_array_bounds(
                root, internal_defines
            ),
        }
    )
    return {
        **variant,
        **{key: value for key, value in ir.items() if key != "series"},
        "device_header": device_header_source,
        "interrupts": interrupts,
        "base_addresses": base_addresses,
        "rcu": rcu,
        "dma": dma,
        "mdma": mdma,
    }


def build_report(
    resources: dict[str, object],
    models: dict[str, object],
    registers: dict[str, object],
    lock: dict[str, object],
    known_source_issues: dict[str, object],
    root: Path,
    compiler: Path,
    cache_dir: Path,
    device_selector_config: dict[str, object] | None = None,
) -> dict[str, object]:
    raw_devices = resources.get("devices")
    if not isinstance(raw_devices, list):
        raise ValueError("Pack 资源报告缺少 devices")
    raw_models = models.get("devices")
    if not isinstance(raw_models, list):
        raise ValueError("规范型号报告缺少 devices")
    raw_libraries = registers.get("libraries")
    if not isinstance(raw_libraries, list):
        raise ValueError("Firmware 寄存器报告缺少 libraries")
    libraries = {str(library["series"]): library for library in raw_libraries}
    if len(libraries) != len(raw_libraries):
        raise ValueError("Firmware 寄存器报告包含重复系列")
    roots = register_extract._library_roots(lock, root)
    compiler_identity = subprocess.run(
        [str(compiler), "--version"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.splitlines()[0]
    spellings = {
        series: selector_spellings(library_root)
        for series, (library_root, _) in roots.items()
        if series in libraries
    }
    device_headers = {
        series: find_device_header(
            roots[series][0], str(library["device_header_sha256"])
        )
        for series, library in libraries.items()
    }
    selectors = {
        series: sorted({value for values in index.values() for value in values})
        for series, index in spellings.items()
    }
    device_selectors = (
        validated_device_selectors(
            device_selector_config, models, set(libraries), selectors, device_headers
        )
        if device_selector_config is not None
        else {}
    )
    variants, missing = collect_variants(
        resources, set(libraries), selectors, models, device_selectors
    )
    output_variants = [
        _variant_ir(
            variant,
            libraries[str(variant["series"])],
            roots[str(variant["series"])][0],
            compiler,
            compiler_identity,
            spellings[str(variant["series"])],
            cache_dir,
            device_header=device_headers[str(variant["series"])],
        )
        for variant in variants
    ]
    covered_devices = [
        str(device)
        for variant in output_variants
        for device in variant["devices"]
    ]
    missing_devices = [row["device"] for row in missing]
    if (
        len(covered_devices) != len(set(covered_devices))
        or len(missing_devices) != len(set(missing_devices))
        or set(covered_devices) & set(missing_devices)
    ):
        raise ValueError("Firmware 型号变体存在重复或同时缺失的设备")
    expected_issues = _known_source_issues(known_source_issues)
    seen_issues = set()
    for variant in output_variants:
        raw_issues = variant["source_issues"]
        assert isinstance(raw_issues, list)
        for issue in raw_issues:
            assert isinstance(issue, dict)
            full_issue = {
                "series": variant["series"],
                "defines": variant["defines"],
                "tree_sha256": variant["tree_sha256"],
                **issue,
            }
            key = _issue_key(full_issue)
            expected = expected_issues.get(key)
            issue["conflict_status"] = classify_source_issue(full_issue, expected)
            if expected is not None:
                seen_issues.add(key)
    stale_issues = set(expected_issues) - seen_issues
    return {
        "schema_version": 1,
        "preprocessor": {
            "mode": "c11-E-dM-and-E-P",
            "cache_version": 3,
            "core_shim_version": CORE_SHIM_VERSION,
        },
        "summary": {
            "pack_devices": len(raw_devices),
            "normalized_devices": len(raw_models),
            "supplemental_devices": sum(
                not model.get("cmsis_devices", []) for model in raw_models
            ),
            "variants": len(output_variants),
            "devices": sum(len(variant["devices"]) for variant in output_variants),
            "missing_devices": len(missing),
            "layouts": sum(len(variant["layouts"]) for variant in output_variants),
            "instances": sum(len(variant["instances"]) for variant in output_variants),
            "interrupts": sum(len(variant["interrupts"]) for variant in output_variants),
            "variants_with_rcu": sum(variant["rcu"] is not None for variant in output_variants),
            "rcu_enable_entries": sum(
                len(variant["rcu"]["enable"])
                for variant in output_variants
                if variant["rcu"] is not None
            ),
            "rcu_reset_entries": sum(
                len(variant["rcu"]["reset"])
                for variant in output_variants
                if variant["rcu"] is not None
            ),
            "variants_with_dma_source": sum(
                variant["dma"] is not None for variant in output_variants
            ),
            "dma_channels": sum(
                len(variant["dma"]["dma_channels"])
                for variant in output_variants
                if variant["dma"] is not None
            ),
            "variants_with_dmamux": sum(
                variant["dma"] is not None
                and variant["dma"]["kind"] == "dmamux"
                for variant in output_variants
            ),
            "dmamux_channels": sum(
                len(variant["dma"]["dmamux_channels"])
                for variant in output_variants
                if variant["dma"] is not None
            ),
            "dmamux_generator_channels": sum(
                len(variant["dma"]["dmamux_generator_channels"])
                for variant in output_variants
                if variant["dma"] is not None
            ),
            "dmamux_requests": sum(
                len(variant["dma"]["requests"])
                for variant in output_variants
                if variant["dma"] is not None
            ),
            "variants_with_mdma": sum(
                variant["mdma"] is not None for variant in output_variants
            ),
            "mdma_channels": sum(
                len(variant["mdma"]["channels"])
                for variant in output_variants
                if variant["mdma"] is not None
            ),
            "mdma_requests": sum(
                len(variant["mdma"]["requests"])
                for variant in output_variants
                if variant["mdma"] is not None
            ),
            "instance_layout_conflicts": sum(
                len(variant["instance_layout_conflicts"])
                for variant in output_variants
            ),
            "source_issues": sum(
                len(variant["source_issues"]) for variant in output_variants
            ),
            "known_blocking_source_issues": sum(
                issue["conflict_status"] == "known-blocking"
                for variant in output_variants
                for issue in variant["source_issues"]
            ),
            "source_resolved_issues": sum(
                issue["conflict_status"] == "source-resolved"
                for variant in output_variants
                for issue in variant["source_issues"]
            ),
            "unexpected_source_issues": sum(
                issue["conflict_status"] == "unexpected"
                for variant in output_variants
                for issue in variant["source_issues"]
            ),
            "stale_known_source_issues": len(stale_issues),
        },
        "stale_known_source_issues": [
            {"series": series, "defines": list(defines), "path": path}
            for series, defines, path in sorted(stale_issues)
        ],
        "missing_devices": missing,
        "variants": output_variants,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resources",
        type=Path,
        default=repo_root / "reports/gigadevice-pack-resources.json",
    )
    parser.add_argument(
        "--models",
        type=Path,
        default=repo_root / "reports/gigadevice-models.json",
    )
    parser.add_argument(
        "--registers",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-registers.json",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=repo_root / "sources/gigadevice/firmware.lock.json",
    )
    parser.add_argument(
        "--known-source-issues",
        type=Path,
        default=repo_root / "sources/gigadevice/firmware-register-conflicts.json",
    )
    parser.add_argument("--device-selectors", type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/firmware-sources-v1",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/firmware-cpp-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-variants.json",
    )
    parser.add_argument("--cpp", default="clang")
    parser.add_argument("--minimum-devices", type=int, default=680)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    compiler_name = shutil.which(args.cpp)
    if compiler_name is None:
        raise ValueError(f"找不到 C 预处理器：{args.cpp}")
    inputs = [
        args.resources,
        args.models,
        args.registers,
        args.lock,
        args.known_source_issues,
    ]
    if args.device_selectors is not None:
        inputs.append(args.device_selectors)
    report = build_report(
        json.loads(args.resources.read_text(encoding="utf-8")),
        json.loads(args.models.read_text(encoding="utf-8")),
        json.loads(args.registers.read_text(encoding="utf-8")),
        json.loads(args.lock.read_text(encoding="utf-8")),
        json.loads(args.known_source_issues.read_text(encoding="utf-8")),
        args.root,
        Path(compiler_name),
        args.cache_dir,
        (
            json.loads(args.device_selectors.read_text(encoding="utf-8"))
            if args.device_selectors is not None
            else None
        ),
    )
    report["provenance"] = {
        path.name: common._sha256(path) for path in sorted(inputs)
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Firmware 型号变体：{args.output}")
    if int(summary["normalized_devices"]) < args.minimum_devices:
        raise ValueError("规范设备闭包低于门限")
    if int(summary["devices"]) + int(summary["missing_devices"]) != int(
        summary["normalized_devices"]
    ):
        raise ValueError("Firmware 型号变体与缺口未闭合全部规范设备")
    if int(summary["instance_layout_conflicts"]) != 0:
        raise ValueError("Firmware 型号变体仍有实例布局冲突")
    for key in ("unexpected_source_issues", "stale_known_source_issues"):
        if int(summary[key]) != 0:
            raise ValueError(f"Firmware 型号变体来源问题未通过门限：{key}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
