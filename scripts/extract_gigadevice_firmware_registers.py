#!/usr/bin/env python3
"""从宽松许可 GD32 Firmware 头文件提取寄存器偏移和位域事实。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import analyze_gigadevice_coverage as coverage
import gigadevice_sources as common
import index_gigadevice_firmware_headers as header_index


REGISTER_RE = re.compile(
    r"^\s*#\s*define\s+([A-Za-z_]\w*)(?:\(([^)]*)\))?\s+REG(8|16|32|64)\s*\((.*)\)\s*$",
    re.MULTILINE,
)
FIELD_RE = re.compile(
    r"^\s*#\s*define\s+([A-Za-z_]\w*)\s+(BIT|BITS)\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)",
    re.MULTILINE,
)
FUNCTION_DEFINE_RE = re.compile(
    r"^\s*#\s*define\s+([A-Za-z_]\w*)\(([^)]*)\)\s+(.+?)\s*$", re.MULTILINE
)
IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")
RANGE_SUFFIX_RE = re.compile(r"(?<!\d)(\d+)_(\d+)$")
REGISTER_COMMENT_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)\(([^)]*)\)"
    r"[^\r\n]*?(/\*.*?\*/)[ \t]*$",
    re.MULTILINE,
)
COMMENT_OR_REGISTER_RE = re.compile(
    r"(?P<comment>/\*.*?\*/)"
    r"|(?P<register>^[ \t]*#[ \t]*define[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"\((?P<parameters>[^)\r\n]*)\)[^\r\n]*?\bREG(?:8|16|32|64)\s*\()",
    re.DOTALL | re.MULTILINE,
)
COMMENT_RANGE_RE = re.compile(
    r"\b([A-Za-z_]\w*)\b\s*=\s*(\d+(?:\s*(?:,|\.{2,3}|-|to|~)\s*\d+)+)",
    re.IGNORECASE,
)
REGISTER_SECTION_RE = re.compile(r"\bregisters?\s+definitions?\b", re.IGNORECASE)
TYPED_ENUM_RE = re.compile(
    r"typedef\s+enum(?:\s+[A-Za-z_]\w*)?\s*\{(?P<body>[^}]*)\}\s*"
    r"(?P<type>[A-Za-z_]\w*)\s*;"
)
ENUM_MEMBER_RE = re.compile(r"^\s*([A-Za-z_]\w*)(?:\s*=\s*(.+))?\s*$")
STRUCT_ARRAY_RE = re.compile(
    r"\b(?:__IO\s+)?(?:u?int)(8|16|32|64)_t\s+([A-Za-z_]\w*)\s*\[([^]]+)\]"
)
SOURCE_LOOP_RE = re.compile(
    r"\bfor\s*\(\s*(?P<variable>[A-Za-z_]\w*)\s*=\s*(?P<start>\d+)[uUlL]*\s*;"
    r"\s*(?P=variable)\s*<\s*(?P<stop>\d+)[uUlL]*\s*;[^)]*\)\s*\{"
    r"(?P<body>.*?)\n\s*\}",
    re.DOTALL,
)
REGISTER_COUNT_DEFINITIONS = {"OP_BYTE": "OB_WORD_CNT"}
SINGLETON_INDEX_PREFIXES = {"EXMC_BANK0_NORSRAM_REGION"}


def _source(text: str) -> str:
    return header_index.COMMENT_RE.sub("", text.replace("\\\n", ""))


def typed_enum_candidates(text: str) -> dict[str, list[tuple[str, int]]]:
    source = _source(text)
    enum_ends: dict[str, set[int]] = {}
    known_values: dict[str, int] = {}
    for match in TYPED_ENUM_RE.finditer(source):
        values = []
        previous = -1
        valid = True
        for raw_member in match.group("body").split(","):
            member = ENUM_MEMBER_RE.fullmatch(raw_member)
            if member is None:
                if raw_member.strip():
                    valid = False
                continue
            name, expression = member.groups()
            value = (
                previous + 1
                if expression is None
                else header_index._evaluate(expression, known_values)
            )
            if value is None:
                valid = False
                break
            previous = int(value)
            known_values[name] = previous
            if not re.search(r"(?:MAX|INVALID|COUNT|NUMBER|END|ALL)$", name):
                values.append(previous)
        unique = sorted(set(values))
        if valid and len(unique) > 1 and unique == list(range(len(unique))):
            enum_ends.setdefault(match.group("type"), set()).add(unique[-1])
    enum_values = {
        enum_type: next(iter(ends))
        for enum_type, ends in enum_ends.items()
        if len(ends) == 1
    }
    candidates: dict[str, list[tuple[str, int]]] = {}
    for enum_type, end in enum_values.items():
        for match in re.finditer(
            rf"\b{re.escape(enum_type)}\s+([A-Za-z_]\w*)\s*(?=[,)])", source
        ):
            row = (enum_type, end)
            if row not in candidates.setdefault(match.group(1), []):
                candidates[match.group(1)].append(row)
    return {parameter: sorted(rows) for parameter, rows in sorted(candidates.items())}


def indexed_identifier_groups(text: str) -> dict[str, list[int]]:
    source = _source(text)
    names = {name for name, _ in header_index.DEFINE_RE.findall(source)}
    names.update(
        member.group(1)
        for enum_match in TYPED_ENUM_RE.finditer(source)
        for raw_member in enum_match.group("body").split(",")
        if (member := ENUM_MEMBER_RE.fullmatch(raw_member)) is not None
    )
    groups: dict[str, set[int]] = {}
    for name in names:
        match = re.fullmatch(r"(.+?)(\d+)", name)
        if match is not None:
            groups.setdefault(match.group(1), set()).add(int(match.group(2)))
    return {
        prefix: sorted(indices)
        for prefix, indices in sorted(groups.items())
        if len(indices) > 1 or prefix in SINGLETON_INDEX_PREFIXES
    }


def indexed_identifier_value_groups(text: str) -> dict[str, list[int]]:
    values = resolve_integer_definitions(text)
    groups: dict[str, set[int]] = {}
    for name, _ in header_index.DEFINE_RE.findall(_source(text)):
        match = re.fullmatch(r"(.+?)(\d+)", name)
        value = values.get(name)
        if match is not None and value is not None and 0 <= value < 512:
            groups.setdefault(match.group(1), set()).add(value)
    return {
        prefix: sorted(raw_values)
        for prefix, raw_values in sorted(groups.items())
        if len(raw_values) > 1
    }


def register_parameter_ranges(text: str) -> dict[str, dict[str, dict[str, int | str]]]:
    candidates: dict[tuple[str, str], set[tuple[int, int]]] = {}

    def add(name: str, parameter: str, raw: str) -> None:
        if "," not in raw and re.search(r"\.{2,3}|-|to|~", raw, re.IGNORECASE):
            start, end = map(
                int, re.split(r"\s*(?:\.{2,3}|-|to|~)\s*", raw, flags=re.IGNORECASE)
            )
            indices = list(range(start, end + 1))
        else:
            indices = list(map(int, re.split(r"\s*,\s*", raw)))
        unique = sorted(set(indices))
        if unique and unique == list(range(unique[0], unique[-1] + 1)):
            candidates.setdefault((name, parameter), set()).add(
                (unique[0], unique[-1])
            )

    for name, raw_parameters, comment in REGISTER_COMMENT_RE.findall(
        text.replace("\\\n", "")
    ):
        for parameter in map(str.strip, raw_parameters.split(",")):
            if not parameter:
                continue
            match = re.search(
                rf"\b{re.escape(parameter)}\b\s*=\s*"
                r"(\d+(?:\s*(?:,|\.{2,3}|-|to|~)\s*\d+)+)",
                comment,
                re.IGNORECASE,
            )
            if match is None:
                upper = re.search(
                    rf"\b{re.escape(parameter)}\b\s*(<=|<)\s*(\d+)", comment
                )
                if upper is not None:
                    end = int(upper.group(2)) - (upper.group(1) == "<")
                    add(name, parameter, f"0..{end}")
                continue
            add(name, parameter, match.group(1))

    current_ranges: dict[str, str] = {}
    for match in COMMENT_OR_REGISTER_RE.finditer(text.replace("\\\n", "")):
        comment = match.group("comment")
        if comment is not None:
            if REGISTER_SECTION_RE.search(comment):
                current_ranges = dict(COMMENT_RANGE_RE.findall(comment))
            continue
        name = match.group("name")
        raw_parameters = match.group("parameters")
        assert name is not None and raw_parameters is not None
        for parameter in map(str.strip, raw_parameters.split(",")):
            keys = [parameter]
            if parameter and parameter[-1] in {"x", "y"}:
                keys.append(parameter[-1])
            ranges = {current_ranges[key] for key in keys if key in current_ranges}
            if len(ranges) == 1:
                add(name, parameter, next(iter(ranges)))
    result: dict[str, dict[str, dict[str, int | str]]] = {}
    for (name, parameter), ranges in sorted(candidates.items()):
        if len(ranges) == 1:
            start, end = next(iter(ranges))
            result.setdefault(name, {})[parameter] = {
                "start": start,
                "end": end,
                "bound_evidence": "source-comment-range",
            }
    return result


def source_loop_array_bounds(text: str) -> list[dict[str, int | str]]:
    rows = set()
    for match in SOURCE_LOOP_RE.finditer(_source(text)):
        start, stop = int(match.group("start")), int(match.group("stop"))
        if stop <= start or stop - start >= 512:
            continue
        variable = match.group("variable")
        for register in re.findall(
            rf"\b([A-Z][A-Z0-9_]*[A-Za-z0-9_]*)\s*\(\s*{re.escape(variable)}\s*\)",
            match.group("body"),
        ):
            rows.add((register, variable, start, stop - 1))
    return [
        {"register": register, "loop_variable": variable, "start": start, "end": end}
        for register, variable, start, end in sorted(rows)
    ]


def resolve_integer_definitions(
    text: str, initial: dict[str, int] | None = None
) -> dict[str, int]:
    values = dict(initial or {})
    expressions = dict(header_index.DEFINE_RE.findall(_source(text)))
    while expressions:
        resolved = {
            name: value
            for name, expression in expressions.items()
            if (value := header_index._evaluate(expression, values)) is not None
        }
        if not resolved:
            break
        values.update(resolved)
        for name in resolved:
            del expressions[name]
    return values


def resolve_library_integer_definitions(
    texts: list[str], initial: dict[str, int] | None = None
) -> dict[str, int]:
    return resolve_integer_definitions("\n".join(texts), initial)


def _address_owner(expression: str, values: dict[str, int], address: int) -> str | None:
    candidates = {
        name: values[name]
        for name in IDENTIFIER_RE.findall(expression)
        if name in values and 0x08000000 <= values[name] <= address
    }
    if not candidates:
        return None
    return max(candidates, key=lambda name: (candidates[name], len(name), name))


def _instance_addresses(
    source: str, values: dict[str, int], owners: set[str]
) -> dict[str, int]:
    instances = {name: values[name] for name in owners}
    for name, expression in header_index.DEFINE_RE.findall(source):
        upper = name.upper()
        address = values.get(name)
        parts = set(upper.split("_"))
        if (
            address is None
            or not 0x08000000 <= address <= 0xEFFFFFFF
            or name.startswith("__")
            or parts & {"BASE", "ADDR", "BUS", "OB"}
            or upper.startswith(("FLASH", "SRAM"))
        ):
            continue
        references = set(IDENTIFIER_RE.findall(expression)) - {name}
        if any(
            candidate in values
            and 0x08000000 <= values[candidate] <= 0xEFFFFFFF
            for candidate in references
        ):
            instances[name] = address
    return dict(sorted(instances.items()))


def _peripheral_blocks(
    instances: dict[str, int], registers: list[dict[str, object]]
) -> dict[str, object]:
    names = [str(row["name"]) for row in registers]
    roots = {name.split("_", 1)[0] for name in names}
    roots.update(
        token
        for name in names
        for token in name.split("_")[:-1]
        if len(token) >= 3
        and any(instance.startswith(token) or token.startswith(instance) for instance in instances)
    )
    owners = {
        str(row["owner"])
        for row in registers
        if row.get("owner") in instances
    }
    instance_blocks = {}
    unassigned_instances = []
    for name in instances:
        if name in owners:
            instance_blocks[name] = name
            continue
        candidates = [root for root in roots if name.startswith(root)]
        if candidates:
            instance_blocks[name] = max(candidates, key=lambda root: (len(root), root))
        elif any(root.startswith(name) for root in roots):
            instance_blocks[name] = name
        else:
            unassigned_instances.append(name)

    blocks = set(instance_blocks.values())
    generic_blocks = {
        block
        for block in blocks
        if any(
            row.get("owner") is None
            and str(row["name"]).startswith(block)
            and row.get("parameters")
            for row in registers
        )
    }
    generic_parents = {
        block
        for block in generic_blocks
        if all(other.startswith(block) for other in generic_blocks)
    }
    fallback = (
        next(iter(blocks))
        if len(blocks) == 1
        else next(iter(generic_blocks))
        if len(generic_blocks) == 1
        else next(iter(generic_parents))
        if len(generic_parents) == 1
        else None
    )
    if fallback is not None:
        for name in unassigned_instances:
            instance_blocks[name] = fallback
        unassigned_instances = []

    blocks = set(instance_blocks.values())
    register_blocks = {}
    unassigned_registers = []
    for row in registers:
        name = str(row["name"])
        owner = row.get("owner")
        if owner in instance_blocks:
            register_blocks[name] = instance_blocks[str(owner)]
            continue
        tokens = set(name.split("_"))
        candidates = [
            block
            for block in blocks
            if name.startswith(block) or block in tokens
        ]
        if candidates:
            register_blocks[name] = max(candidates, key=lambda block: (len(block), block))
        elif len(blocks) == 1:
            register_blocks[name] = next(iter(blocks))
        else:
            unassigned_registers.append(name)
    return {
        "instance_blocks": dict(sorted(instance_blocks.items())),
        "register_blocks": dict(sorted(register_blocks.items())),
        "unassigned_instances": sorted(unassigned_instances),
        "unassigned_registers": sorted(unassigned_registers),
    }


def _split_arguments(value: str) -> list[str]:
    arguments = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            arguments.append(value[start:index].strip())
            start = index + 1
    arguments.append(value[start:].strip())
    return arguments


def _expand_function_macros(expression: str, source: str) -> str:
    macros = {
        name: ([item.strip() for item in parameters.split(",")], body)
        for name, parameters, body in FUNCTION_DEFINE_RE.findall(source)
        if name not in {"REG8", "REG16", "REG32", "REG64", "BIT", "BITS"}
    }
    if not macros:
        return expression
    matcher = re.compile(r"\b(" + "|".join(map(re.escape, sorted(macros, key=len, reverse=True))) + r")\s*\(")
    for _ in range(32):
        replacement = None
        for match in matcher.finditer(expression):
            opening = expression.find("(", match.start())
            depth = 0
            closing = None
            for index in range(opening, len(expression)):
                if expression[index] == "(":
                    depth += 1
                elif expression[index] == ")":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
            if closing is None:
                continue
            parameters, body = macros[match.group(1)]
            arguments = _split_arguments(expression[opening + 1 : closing])
            if len(arguments) != len(parameters):
                continue
            expanded = body
            for parameter, argument in zip(parameters, arguments, strict=True):
                expanded = re.sub(
                    rf"\b{re.escape(parameter)}\b", f"({argument})", expanded
                )
            replacement = (match.start(), closing + 1, f"({expanded})")
            break
        if replacement is None:
            return expression
        start, end, value = replacement
        expression = expression[:start] + value + expression[end:]
    raise ValueError("函数式地址宏递归展开超过 32 层")


def parse_register_facts(text: str, values: dict[str, int]) -> dict[str, object]:
    parameter_ranges = register_parameter_ranges(text)
    source = _source(text)
    struct_arrays: dict[tuple[int, str], set[int]] = {}
    for raw_width, name, expression in STRUCT_ARRAY_RE.findall(source):
        count = header_index._evaluate(expression, values)
        if count is not None and count > 1:
            struct_arrays.setdefault((int(raw_width), name.casefold()), set()).add(
                int(count)
            )
    registers = []
    unresolved = []
    helpers = []
    owners = set()
    for name, raw_parameters, width, expression in REGISTER_RE.findall(source):
        expression = _expand_function_macros(expression, source)
        parameters = [item.strip() for item in raw_parameters.split(",") if item.strip()]
        zero = dict(values)
        zero.update({parameter: 0 for parameter in parameters})
        address = header_index._evaluate(expression, zero)
        if address is None:
            unknown = set(IDENTIFIER_RE.findall(expression)) - set(zero) - {
                "uint8_t",
                "uint16_t",
                "uint32_t",
                "uint64_t",
                "uintptr_t",
                "volatile",
            }
            (helpers if unknown else unresolved).append(name)
            continue
        parameter_strides = {}
        nonlinear_parameters = []
        for parameter in parameters:
            one = dict(zero)
            one[parameter] = 1
            two = dict(zero)
            two[parameter] = 2
            second = header_index._evaluate(expression, one)
            third = header_index._evaluate(expression, two)
            if second is None or third is None:
                continue
            stride = second - address
            if third - address != 2 * stride:
                nonlinear_parameters.append(parameter)
            elif stride:
                parameter_strides[parameter] = stride

        owner = _address_owner(expression, values, address)
        base_parameters = (
            sorted(
                parameter
                for parameter, stride in parameter_strides.items()
                if stride == 1
            )
            if owner is None
            else []
        )
        array_parameters = {
            parameter: {"start": 0, "stride": stride}
            for parameter, stride in parameter_strides.items()
            if parameter not in base_parameters
        }
        range_match = RANGE_SUFFIX_RE.search(name)
        if range_match is not None and len(array_parameters) == 1:
            start, end = map(int, range_match.groups())
            if end >= start:
                parameter = next(iter(array_parameters))
                array_parameters[parameter].update({"start": start, "end": end})
        for parameter, evidence in parameter_ranges.get(name, {}).items():
            if parameter in array_parameters:
                array_parameters[parameter].update(evidence)
        suffix = name.split("_", 1)[-1].casefold()
        for parameter, bounds in array_parameters.items():
            parameter_name = parameter.casefold()
            member = (
                suffix[: -len(parameter_name)]
                if parameter_name and suffix.endswith(parameter_name)
                else suffix
            )
            counts = struct_arrays.get((int(width), member), set())
            if "end" not in bounds and len(counts) == 1:
                bounds.update(
                    {
                        "end": next(iter(counts)) - 1,
                        "bound_evidence": "source-struct-array",
                    }
                )
        count_name = REGISTER_COUNT_DEFINITIONS.get(name)
        count = values.get(count_name) if count_name is not None else None
        if count is not None and 1 < count < 512:
            for bounds in array_parameters.values():
                if "end" not in bounds:
                    bounds.update(
                        {
                            "end": int(bounds["start"]) + count - 1,
                            "bound_evidence": f"source-count:{count_name}",
                        }
                    )
        origin = dict(zero)
        for parameter, bounds in array_parameters.items():
            origin[parameter] = int(bounds["start"])
        origin_address = header_index._evaluate(expression, origin)
        if origin_address is None:
            unresolved.append(name)
            continue
        if owner is not None:
            owners.add(owner)
        row: dict[str, object] = {
            "name": name,
            "offset": origin_address - values[owner] if owner is not None else origin_address,
            "parameters": parameters,
            "width": int(width),
        }
        if owner is not None:
            row["owner"] = owner
        if base_parameters:
            row["base_parameters"] = base_parameters
        if array_parameters:
            row["array_parameters"] = array_parameters
        if nonlinear_parameters:
            row["nonlinear_parameters"] = sorted(nonlinear_parameters)
        strides = [
            (parameter, int(bounds["stride"]))
            for parameter, bounds in array_parameters.items()
        ]
        unique_strides = {stride for _, stride in strides}
        if len(unique_strides) == 1:
            row["stride"] = unique_strides.pop()
        elif strides:
            row["strides"] = {parameter: stride for parameter, stride in strides}
        registers.append(row)

    unique_registers = {
        json.dumps(row, ensure_ascii=False, sort_keys=True): row for row in registers
    }
    registers = sorted(
        unique_registers.values(),
        key=lambda row: (str(row["name"]), int(row["offset"]), int(row["width"])),
    )
    register_rows = sorted(
        registers, key=lambda row: (-len(str(row["name"])), str(row["name"]))
    )
    fields = []
    unmatched_fields = []
    invalid_fields = []
    for name, kind, first, second in FIELD_RE.findall(source):
        bit_offset = int(first)
        if kind == "BITS" and not second:
            invalid_fields.append(
                {
                    "name": name,
                    "first": bit_offset,
                    "second": None,
                    "reason": "BITS缺少结束位",
                }
            )
            continue
        bit_end = bit_offset if kind == "BIT" else int(second)
        if bit_end < bit_offset:
            invalid_fields.append(
                {"name": name, "first": bit_offset, "second": bit_end}
            )
            continue
        candidates = [
            row
            for row in register_rows
            if name.startswith(str(row["name"]) + "_")
        ]
        if not candidates:
            unmatched_fields.append(name)
            continue
        register_row = next(
            (row for row in candidates if bit_end < int(row["width"])), None
        )
        if register_row is None:
            invalid_fields.append(
                {"name": name, "first": bit_offset, "second": bit_end}
            )
            continue
        fields.append(
            {
                "name": name,
                "register": register_row["name"],
                "bit_offset": bit_offset,
                "bit_size": bit_end - bit_offset + 1,
            }
        )
    unique_fields = {
        json.dumps(row, ensure_ascii=False, sort_keys=True): row for row in fields
    }
    instances = _instance_addresses(source, values, owners)
    blocks = _peripheral_blocks(instances, registers)
    return {
        "instances": instances,
        **blocks,
        "registers": registers,
        "fields": sorted(
            unique_fields.values(),
            key=lambda row: (str(row["name"]), int(row["bit_offset"])),
        ),
        "unresolved_registers": sorted(set(unresolved)),
        "helper_register_macros": sorted(set(helpers)),
        "unmatched_fields": sorted(set(unmatched_fields)),
        "invalid_fields": sorted(invalid_fields, key=lambda row: str(row["name"])),
    }


def _library_roots(
    lock: dict[str, object], root: Path
) -> dict[str, tuple[Path, dict[str, object]]]:
    result = {}
    for raw in lock["firmware"]:
        assert isinstance(raw, dict)
        filename = str(raw["filename"])
        series = coverage._series_from_firmware_filename(filename)
        if series in result:
            raise ValueError(f"Firmware 锁文件包含重复系列：{series}")
        result[series] = (root / filename.removesuffix(".7z"), raw)
    return result


def build_report(
    lock: dict[str, object], header_report: dict[str, object], root: Path
) -> dict[str, object]:
    roots = _library_roots(lock, root)
    libraries = []
    for raw_library in header_report["libraries"]:
        assert isinstance(raw_library, dict)
        series = str(raw_library["series"])
        library_root, lock_item = roots[series]
        if raw_library["archive_sha256"] != lock_item["sha256"]:
            raise ValueError(f"Firmware 头文件报告与锁文件哈希不一致：{series}")
        device_headers = raw_library["device_headers"]
        if not isinstance(device_headers, list) or len(device_headers) != 1:
            raise ValueError(f"Firmware 系列 {series} 必须恰有一个器件头文件")
        device_header = device_headers[0]
        assert isinstance(device_header, dict)
        central_path = library_root.joinpath(*Path(str(device_header["path"])).parts)
        central_text = central_path.read_text(encoding="utf-8", errors="ignore")
        central_values = resolve_integer_definitions(central_text)

        candidates = []
        blocked_headers = []
        for path in sorted(library_root.rglob("*.h")):
            if not any(part.casefold() == "firmware" for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if REGISTER_RE.search(_source(text)) is None:
                continue
            relative = path.relative_to(library_root).as_posix()
            sha256 = common._sha256(path)
            license_name = coverage.source_license(text[:16384])
            if license_name not in header_index.PERMISSIVE_LICENSES:
                blocked_headers.append(
                    {"path": relative, "sha256": sha256, "license": license_name}
                )
                continue
            candidates.append((relative, sha256, license_name, text))

        library_values = resolve_library_integer_definitions(
            [text for _, _, _, text in candidates], central_values
        )
        by_hash = {}
        for relative, sha256, license_name, text in candidates:
            if sha256 in by_hash:
                duplicates = by_hash[sha256]["duplicate_paths"]
                assert isinstance(duplicates, list)
                duplicates.append(relative)
                continue
            values = resolve_integer_definitions(text, library_values)
            facts = parse_register_facts(text, values)
            array_bound_candidates = (
                typed_enum_candidates(text)
                if any(
                    "end" not in bounds
                    for register in facts["registers"]
                    for bounds in register.get("array_parameters", {}).values()
                )
                else {}
            )
            array_index_groups = (
                indexed_identifier_groups(text)
                if any(
                    "end" not in bounds
                    for register in facts["registers"]
                    for bounds in register.get("array_parameters", {}).values()
                )
                else {}
            )
            array_value_groups = (
                indexed_identifier_value_groups(text)
                if array_index_groups
                else {}
            )
            by_hash[sha256] = {
                "path": relative,
                "duplicate_paths": [],
                "sha256": sha256,
                "license": license_name,
                "array_bound_candidates": array_bound_candidates,
                "array_index_groups": array_index_groups,
                "array_value_groups": array_value_groups,
                **facts,
            }
        register_headers = sorted(by_hash.values(), key=lambda row: str(row["path"]))
        register_names = {
            str(register["name"])
            for header in register_headers
            for register in header["registers"]
        }
        source_loop_bounds = []
        for path in sorted(library_root.rglob("*.c")):
            if not any(part.casefold() == "firmware" for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if coverage.source_license(text[:16384]) not in header_index.PERMISSIVE_LICENSES:
                continue
            relative = path.relative_to(library_root).as_posix()
            source = {"path": relative, "sha256": common._sha256(path)}
            source_loop_bounds.extend(
                {**row, "source": source}
                for row in source_loop_array_bounds(text)
                if row["register"] in register_names
            )
        libraries.append(
            {
                "series": series,
                "version": raw_library["version"],
                "archive_sha256": raw_library["archive_sha256"],
                "tree_sha256": raw_library["tree_sha256"],
                "device_header_sha256": device_header["sha256"],
                "register_headers": register_headers,
                "blocked_register_headers": blocked_headers,
                "source_loop_bounds": sorted(
                    source_loop_bounds,
                    key=lambda row: (
                        str(row["register"]),
                        int(row["start"]),
                        int(row["end"]),
                        str(row["source"]["path"]),
                    ),
                ),
            }
        )
    libraries.sort(key=lambda row: str(row["series"]).casefold())
    all_headers = [header for library in libraries for header in library["register_headers"]]
    blocked = [header for library in libraries for header in library["blocked_register_headers"]]
    return {
        "schema_version": 1,
        "summary": {
            "firmware_libraries": len(libraries),
            "register_headers": len(all_headers),
            "blocked_register_headers": len(blocked),
            "libraries_without_register_headers": sum(
                not library["register_headers"] for library in libraries
            ),
            "resolved_registers": sum(len(header["registers"]) for header in all_headers),
            "unresolved_registers": sum(
                len(header["unresolved_registers"]) for header in all_headers
            ),
            "helper_register_macros": sum(
                len(header["helper_register_macros"]) for header in all_headers
            ),
            "headers_with_array_bound_candidates": sum(
                bool(header["array_bound_candidates"]) for header in all_headers
            ),
            "headers_with_array_index_groups": sum(
                bool(header["array_index_groups"]) for header in all_headers
            ),
            "headers_with_array_value_groups": sum(
                bool(header["array_value_groups"]) for header in all_headers
            ),
            "source_loop_bounds": sum(
                len(library["source_loop_bounds"]) for library in libraries
            ),
            "instances": sum(len(header["instances"]) for header in all_headers),
            "peripheral_blocks": sum(
                len(set(header["instance_blocks"].values())) for header in all_headers
            ),
            "unassigned_instances": sum(
                len(header["unassigned_instances"]) for header in all_headers
            ),
            "unassigned_registers": sum(
                len(header["unassigned_registers"]) for header in all_headers
            ),
            "fields": sum(len(header["fields"]) for header in all_headers),
            "unmatched_fields": sum(len(header["unmatched_fields"]) for header in all_headers),
            "invalid_fields": sum(len(header["invalid_fields"]) for header in all_headers),
        },
        "libraries": libraries,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock", type=Path, default=repo_root / "sources/gigadevice/firmware.lock.json"
    )
    parser.add_argument(
        "--headers",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-headers.json",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/firmware-sources-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "reports/gigadevice-firmware-registers.json",
    )
    parser.add_argument("--minimum-firmware-libraries", type=int, default=33)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_report(
        json.loads(args.lock.read_text(encoding="utf-8")),
        json.loads(args.headers.read_text(encoding="utf-8")),
        args.root,
    )
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Firmware 寄存器报告：{args.output}")
    if int(summary["firmware_libraries"]) < args.minimum_firmware_libraries:
        raise ValueError("Firmware 库数量低于寄存器提取门限")
    if int(summary["libraries_without_register_headers"]) != 0:
        raise ValueError("仍有 Firmware 库缺少宽松许可寄存器头文件")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
