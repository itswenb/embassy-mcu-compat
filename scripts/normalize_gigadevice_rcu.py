#!/usr/bin/env python3
"""把 Firmware RCU 枚举反查为可验证的寄存器门控事实。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import gigadevice_sources as common


INSTANCE_ALIASES = {
    "AF": ("AFIO",),
    "AFMT": ("EDIM_AFMT",),
    "BISS": ("EDIM_BISS",),
    "BKPI": ("BKP",),
    "CFG": ("SYSCFG",),
    "CFGCMP": ("CMP", "SYSCFG"),
    "DBGMCU": ("DBG",),
    "ENDAT": ("EDIM_ENDAT",),
    "HDSL": ("EDIM_HDSL",),
    "RF": ("WIFI_RF",),
    "TFMT": ("EDIM_TFMT",),
    "ULPI": ("USBHS",),
}
AUXILIARY_SUFFIXES = ("ULPI", "HOLD", "PTP", "RUN", "TX", "RX")
SYSTEM_MEMORY_RE = re.compile(r"(?:BKP|TCM)?SRAM[0-9]*|BKP|CPU")
WIRELESS_SUBSYSTEM_NAMES = {"BLE", "RFI"}
BASE_INSTANCE_RE = re.compile(r"^([A-Z][A-Z0-9_]*?)_BASE(?:_(?:NS|S))?$")
NON_PERIPHERAL_BASE_TOKENS = {
    "ADDRESS",
    "BANK",
    "BB",
    "BUS",
    "CHANNEL",
    "FLASH",
    "OB",
    "OPTION",
    "PERIPH",
    "RAM",
}


def base_instance_names(base_addresses: dict[str, object]) -> list[str]:
    names = set()
    for raw_name in base_addresses:
        match = BASE_INSTANCE_RE.fullmatch(raw_name)
        if match is None:
            continue
        name = match.group(1)
        tokens = set(name.split("_"))
        if (
            tokens & NON_PERIPHERAL_BASE_TOKENS
            or "FLASH" in name
            or "SRAM" in name
        ):
            continue
        names.add(name)
    return sorted(names)


def resolve_gate(
    layout: dict[str, object], entry: dict[str, object]
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    offset = int(entry["register_offset"])
    bit = int(entry["bit"])
    registers = [
        register
        for register in layout["registers"]
        if int(register["offset"]) == offset and not register.get("parameters", [])
    ]
    issue = {"name": entry["name"], "register_offset": offset, "bit": bit}
    if len(registers) != 1:
        return None, {
            **issue,
            "reason": "register-missing" if not registers else "register-ambiguous",
            "candidates": sorted(str(register["name"]) for register in registers),
        }
    register_name = str(registers[0]["name"])
    fields = [
        field
        for field in layout["fields"]
        if field["register"] == register_name
        and int(field["bit_offset"]) == bit
        and int(field["bit_size"]) == 1
    ]
    if len(fields) != 1:
        if not fields:
            return {
                "name": entry["name"],
                "register": register_name,
                "field": None,
                "register_offset": offset,
                "bit": bit,
                "resolution": "firmware-enum",
            }, None
        return None, {
            **issue,
            "reason": "field-ambiguous",
            "register": register_name,
            "candidates": sorted(str(field["name"]) for field in fields),
        }
    return {
        "name": entry["name"],
        "register": register_name,
        "field": fields[0]["name"],
        "register_offset": offset,
        "bit": bit,
    }, None


def resolve_svd_gate(
    gates: list[dict[str, object]], kind: str, entry: dict[str, object]
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    candidates = [
        gate
        for gate in gates
        if gate.get("kind") == kind and gate.get("name") == entry.get("name")
    ]
    facts = {
        (int(gate["register_offset"]), int(gate["bit"])) for gate in candidates
    }
    if len(facts) != 1:
        return None, {
            "name": entry["name"],
            "reason": "svd-gate-missing" if not facts else "svd-gate-conflict",
            "kind": kind,
            "firmware_entry": entry,
            "candidates": sorted([list(fact) for fact in facts]),
        }
    register_offset, bit = next(iter(facts))
    selected = min(
        (
            gate
            for gate in candidates
            if int(gate["register_offset"]) == register_offset
            and int(gate["bit"]) == bit
        ),
        key=lambda gate: (str(gate["register"]), str(gate["field"])),
    )
    return {
        "name": entry["name"],
        "register": selected["register"],
        "field": selected["field"],
        "register_offset": register_offset,
        "bit": bit,
        "resolution": "svd",
        "svd_sha256s": sorted(
            {
                str(gate["svd_sha256"])
                for gate in candidates
                if gate.get("svd_sha256") is not None
            }
        ),
        "firmware_entry": entry,
    }, None


def resolve_firmware_field_gate(
    layout: dict[str, object], kind: str, entry: dict[str, object]
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    suffix = "EN" if kind == "enable" else "RST"
    field_suffix = f"_{entry['name']}{suffix}"
    registers = {str(row["name"]): row for row in layout["registers"]}
    candidates = [
        field
        for field in layout["fields"]
        if str(field["name"]).endswith(field_suffix)
        and int(field["bit_size"]) == 1
        and str(field["register"]) in registers
    ]
    facts = {
        (
            str(field["register"]),
            int(registers[str(field["register"])]["offset"]),
            int(field["bit_offset"]),
        )
        for field in candidates
    }
    if len(facts) != 1:
        return None, {
            "name": entry["name"],
            "reason": (
                "firmware-field-missing" if not facts else "firmware-field-conflict"
            ),
            "kind": kind,
            "firmware_entry": entry,
            "candidates": sorted([list(fact) for fact in facts]),
        }
    register, register_offset, bit = next(iter(facts))
    field = next(
        item
        for item in candidates
        if str(item["register"]) == register and int(item["bit_offset"]) == bit
    )
    return {
        "name": entry["name"],
        "register": register,
        "field": field["name"],
        "register_offset": register_offset,
        "bit": bit,
        "resolution": "firmware-field",
        "firmware_entry": entry,
    }, None


def bind_gate(
    instances_by_name: dict[str, list[dict[str, object]]], name: str
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    direct = [("exact", (name,))]
    if re.fullmatch(r"P[A-Z]", name):
        direct.append(("gpio-port", (f"GPIO{name[1:]}",)))
    if name in INSTANCE_ALIASES:
        direct.append(("alias", INSTANCE_ALIASES[name]))
    for rule, candidates in direct:
        if not all(candidate in instances_by_name for candidate in candidates):
            continue
        duplicates = [
            candidate
            for candidate in candidates
            if len(instances_by_name[candidate]) != 1
        ]
        if duplicates:
            return None, {
                "name": name,
                "reason": "peripheral-ambiguous",
                "candidates": duplicates,
            }
        return {
            "kind": "peripheral",
            "rule": rule,
            "peripherals": sorted(candidates),
        }, None

    casefolded = sorted(
        candidate for candidate in instances_by_name if candidate.casefold() == name.casefold()
    )
    if len(casefolded) == 1 and len(instances_by_name[casefolded[0]]) == 1:
        return {
            "kind": "peripheral",
            "rule": "case-insensitive",
            "peripherals": casefolded,
        }, None

    attempts = [("indexed", name)]
    attempts.extend(
        ("auxiliary", name.removesuffix(suffix))
        for suffix in AUXILIARY_SUFFIXES
        if name.endswith(suffix) and name != suffix
    )
    for rule, base in attempts:
        numbered = sorted(
            candidate
            for candidate in instances_by_name
            if re.fullmatch(re.escape(base) + r"[0-9]+", candidate)
        )
        grouped = sorted(
            candidate
            for candidate in instances_by_name
            if candidate.startswith(base + "_")
        )
        candidates = [base] if base in instances_by_name else numbered or grouped
        if not candidates:
            continue
        duplicates = [
            candidate
            for candidate in candidates
            if len(instances_by_name[candidate]) != 1
        ]
        if duplicates:
            return None, {
                "name": name,
                "reason": "peripheral-ambiguous",
                "candidates": duplicates,
            }
        return {
            "kind": "peripheral",
            "rule": (
                rule
                if rule == "auxiliary"
                else "indexed"
                if numbered
                else "grouped"
            ),
            "peripherals": candidates,
        }, None
    if name == "TZPCU":
        candidates = sorted(
            candidate
            for candidate in instances_by_name
            if re.fullmatch(r"TZBMPC[0-9]+|TZIAC|TZSPC", candidate)
        )
        if candidates:
            duplicates = [
                candidate
                for candidate in candidates
                if len(instances_by_name[candidate]) != 1
            ]
            if duplicates:
                return None, {
                    "name": name,
                    "reason": "peripheral-ambiguous",
                    "candidates": duplicates,
                }
            return {
                "kind": "peripheral",
                "rule": "trustzone-group",
                "peripherals": candidates,
            }, None
    if name in WIRELESS_SUBSYSTEM_NAMES:
        return {
            "kind": "system",
            "rule": "wireless-subsystem",
            "peripherals": [],
        }, None
    if re.fullmatch(r"USBHS[01](?:ULPI)?", name) is not None and "USBHS" in instances_by_name:
        return {
            "kind": "system",
            "rule": "unmodeled-usbhs-instance",
            "peripherals": [],
        }, None
    if SYSTEM_MEMORY_RE.fullmatch(name) is not None:
        return {
            "kind": "system",
            "rule": "system-memory",
            "peripherals": [],
        }, None
    return None, {"name": name, "reason": "peripheral-missing", "candidates": []}


def build_outputs(
    variants: dict[str, object],
    models: dict[str, object],
    resources: dict[str, object] | None = None,
    svds: dict[str, object] | None = None,
    iar: dict[str, object] | None = None,
    iar_svds: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    raw_variants = variants.get("variants")
    raw_missing = variants.get("missing_devices")
    if not isinstance(raw_variants, list) or not isinstance(raw_missing, list):
        raise ValueError("Firmware 变体报告缺少 variants 或 missing_devices")

    seen_variants: set[str] = set()
    seen_devices: set[str] = set()
    normalized_variants = []
    public_variants = []
    variants_by_device: dict[str, dict[str, object]] = {}
    instances_by_variant: dict[str, dict[str, list[dict[str, object]]]] = {}
    for variant in sorted(raw_variants, key=lambda item: str(item["id"])):
        variant_id = str(variant["id"])
        if variant_id in seen_variants:
            raise ValueError(f"Firmware RCU 变体主键重复：{variant_id}")
        seen_variants.add(variant_id)
        variant_devices = sorted(str(device) for device in variant["devices"])
        if len(variant_devices) != len(set(variant_devices)):
            raise ValueError(f"Firmware RCU 变体内设备重复：{variant_id}")
        duplicate_devices = seen_devices & set(variant_devices)
        if duplicate_devices:
            raise ValueError(
                f"Firmware RCU 设备重复归属：{', '.join(sorted(duplicate_devices))}"
            )
        seen_devices.update(variant_devices)

        rcu = variant.get("rcu")
        gate_issues: list[dict[str, object]] = []
        binding_issues: list[dict[str, object]] = []
        enable: list[dict[str, object]] = []
        reset: list[dict[str, object]] = []
        instance = None
        layout = None
        instances_by_name: dict[str, list[dict[str, object]]] = {}
        for item in variant["instances"]:
            instances_by_name.setdefault(str(item["name"]), []).append(item)
        base_addresses = variant.get("base_addresses", {})
        if not isinstance(base_addresses, dict):
            raise ValueError(f"Firmware RCU 变体基址表无效：{variant_id}")
        for name in base_instance_names(base_addresses):
            instances_by_name.setdefault(
                name, [{"name": name, "source": "device-header-base"}]
            )
        instances_by_variant[variant_id] = instances_by_name
        if not isinstance(rcu, dict):
            gate_issues.append({"reason": "rcu-source-missing"})
        else:
            instances = [
                item for item in variant["instances"] if item.get("name") == "RCU"
            ]
            if len(instances) != 1:
                gate_issues.append(
                    {
                        "reason": (
                            "rcu-instance-missing"
                            if not instances
                            else "rcu-instance-ambiguous"
                        ),
                        "candidates": [str(item.get("layout")) for item in instances],
                    }
                )
            else:
                instance = instances[0]
                layouts = [
                    item
                    for item in variant["layouts"]
                    if item.get("id") == instance.get("layout")
                ]
                if len(layouts) != 1:
                    gate_issues.append(
                        {
                            "reason": (
                                "rcu-layout-missing"
                                if not layouts
                                else "rcu-layout-ambiguous"
                            ),
                            "layout": instance.get("layout"),
                        }
                    )
                else:
                    layout = layouts[0]
                    for kind, output in (("enable", enable), ("reset", reset)):
                        for entry in rcu[kind]:
                            gate, issue = resolve_gate(layout, entry)
                            if issue is not None:
                                gate_issues.append({"kind": kind, **issue})
                            else:
                                assert gate is not None
                                binding, binding_issue = bind_gate(
                                    instances_by_name, str(gate["name"])
                                )
                                gate["binding"] = binding
                                if binding_issue is not None:
                                    binding_issues.append({"kind": kind, **binding_issue})
                                output.append(gate)

        gate_status = (
            "missing"
            if not isinstance(rcu, dict)
            else "conflict"
            if gate_issues
            else "normalized"
        )
        binding_status = (
            "missing"
            if not isinstance(rcu, dict)
            else "conflict"
            if gate_issues or binding_issues
            else "normalized"
        )
        status = binding_status
        issues = [*gate_issues, *binding_issues]
        row = {
            "id": variant_id,
            "series": variant["series"],
            "devices": variant_devices,
            "status": status,
            "gate_status": gate_status,
            "binding_status": binding_status,
            "rcu_address": instance.get("address") if instance is not None else None,
            "layout": layout.get("id") if layout is not None else None,
            "source": rcu.get("source") if isinstance(rcu, dict) else None,
            "enable": enable,
            "reset": reset,
            "raw_enable": list(rcu["enable"]) if isinstance(rcu, dict) else [],
            "raw_reset": list(rcu["reset"]) if isinstance(rcu, dict) else [],
            "registers": list(layout["registers"]) if layout is not None else [],
            "fields": list(layout["fields"]) if layout is not None else [],
            "issues": issues,
        }
        normalized_variants.append(row)
        variants_by_device.update({device: row for device in variant_devices})
        public_variants.append(
            {
                **{
                    key: row[key]
                    for key in (
                        "id",
                        "series",
                        "devices",
                        "status",
                        "gate_status",
                        "binding_status",
                    )
                },
                "layout": row["layout"],
                "source": row["source"],
                "enable_gates": len(enable),
                "reset_gates": len(reset),
                "issues": issues,
            }
        )

    missing_by_device: dict[str, dict[str, object]] = {}
    for missing in sorted(raw_missing, key=lambda item: str(item["device"])):
        device = str(missing["device"])
        if device in seen_devices:
            raise ValueError(f"Firmware RCU 设备同时存在和缺失：{device}")
        seen_devices.add(device)
        missing_by_device[device] = missing

    source_summary = variants.get("summary")
    if not isinstance(source_summary, dict):
        raise ValueError("Firmware 变体报告缺少 summary")
    if len(seen_devices) != int(source_summary["normalized_devices"]):
        raise ValueError("Firmware RCU 来源设备闭包与变体报告不一致")
    if sum(variant.get("rcu") is not None for variant in raw_variants) != int(
        source_summary["variants_with_rcu"]
    ):
        raise ValueError("Firmware RCU 来源计数与变体报告不一致")

    raw_models = models.get("devices")
    if not isinstance(raw_models, list):
        raise ValueError("规范型号报告缺少 devices")
    if (resources is None) != (svds is None):
        raise ValueError("Pack 资源和 SVD 审计必须同时提供")
    if (iar is None) != (iar_svds is None):
        raise ValueError("IAR 设备和 SVD 审计必须同时提供")
    resources_by_device: dict[str, dict[str, object]] = {}
    svds_by_sha256: dict[str, dict[str, object]] = {}
    if resources is not None and svds is not None:
        raw_resources = resources.get("devices")
        raw_svds = svds.get("svds")
        if not isinstance(raw_resources, list) or not isinstance(raw_svds, list):
            raise ValueError("Pack 资源或 SVD 审计缺少设备列表")
        resources_by_device = {str(item["device"]): item for item in raw_resources}
        svds_by_sha256 = {str(item["sha256"]): item for item in raw_svds}
        if len(resources_by_device) != len(raw_resources):
            raise ValueError("Pack 资源设备主键重复")
        if len(svds_by_sha256) != len(raw_svds):
            raise ValueError("SVD 审计 SHA-256 重复")
    iar_devices: dict[str, dict[str, object]] = {}
    iar_files: dict[str, dict[str, object]] = {}
    iar_svds_by_sha256: dict[str, dict[str, object]] = {}
    if iar is not None and iar_svds is not None:
        raw_iar_devices = iar.get("devices")
        raw_iar_files = iar.get("svd_files")
        raw_iar_svds = iar_svds.get("svds")
        if not all(isinstance(rows, list) for rows in (raw_iar_devices, raw_iar_files, raw_iar_svds)):
            raise ValueError("IAR 设备或 SVD 审计缺少列表")
        iar_devices = {str(row["id"]): row for row in raw_iar_devices}
        iar_files = {Path(str(row["path"])).name: row for row in raw_iar_files}
        iar_svds_by_sha256 = {str(row["sha256"]): row for row in raw_iar_svds}
        if len(iar_devices) != len(raw_iar_devices) or len(iar_files) != len(raw_iar_files):
            raise ValueError("IAR 设备或 SVD 文件主键重复")
    devices = []
    detailed_devices = []
    model_ids: set[str] = set()
    for model in sorted(raw_models, key=lambda item: str(item["id"])):
        device = str(model["id"])
        if device in model_ids:
            raise ValueError(f"RCU 规范型号重复：{device}")
        model_ids.add(device)
        source_devices = {device, *(str(item) for item in model["cmsis_devices"])}
        svd_sha256s: set[str] = set()
        svd_peripherals: set[str] = set()
        svd_rcu_gates: list[dict[str, object]] = []
        if resources is not None:
            for cmsis_device in model["cmsis_devices"]:
                resource = resources_by_device.get(str(cmsis_device))
                if resource is None:
                    raise ValueError(f"RCU 规范型号引用未知 Pack 设备：{cmsis_device}")
                for debug in resource.get("debug", []):
                    file = debug.get("file")
                    if not isinstance(file, dict):
                        continue
                    sha256 = str(file["sha256"])
                    svd = svds_by_sha256.get(sha256)
                    if svd is None:
                        raise ValueError(f"RCU 规范型号引用未审计 SVD：{sha256}")
                    peripheral_names = svd.get("peripheral_names")
                    if not isinstance(peripheral_names, list):
                        raise ValueError(f"SVD 审计缺少外设名称：{sha256}")
                    svd_sha256s.add(sha256)
                    svd_peripherals.update(map(str, peripheral_names))
                    raw_rcu_gates = svd.get("rcu_gates", [])
                    if not isinstance(raw_rcu_gates, list):
                        raise ValueError(f"SVD 审计 RCU 门控无效：{sha256}")
                    svd_rcu_gates.extend(
                        {**gate, "svd_sha256": sha256}
                        for gate in raw_rcu_gates
                        if isinstance(gate, dict)
                    )
        if iar_device := iar_devices.get(device):
            iar_file = iar_files.get(str(iar_device["svd"]))
            if iar_file is None:
                raise ValueError(f"RCU IAR 型号引用未知 SVD：{device}")
            sha256 = str(iar_file["sha256"])
            svd = iar_svds_by_sha256.get(sha256)
            if svd is None:
                raise ValueError(f"RCU IAR 型号引用未审计 SVD：{device}")
            peripheral_names = svd.get("peripheral_names")
            raw_rcu_gates = svd.get("rcu_gates")
            if not isinstance(peripheral_names, list) or not isinstance(raw_rcu_gates, list):
                raise ValueError(f"RCU IAR SVD 审计结构无效：{device}")
            svd_sha256s.add(sha256)
            svd_peripherals.update(map(str, peripheral_names))
            svd_rcu_gates.extend(
                {**gate, "svd_sha256": sha256}
                for gate in raw_rcu_gates
                if isinstance(gate, dict)
            )
        selected = {
            str(row["id"]): row
            for source_device in source_devices
            if (row := variants_by_device.get(source_device)) is not None
        }
        missing = [
            missing_by_device[source_device]
            for source_device in source_devices
            if source_device in missing_by_device
        ]
        if len(selected) > 1:
            raise ValueError(
                f"RCU 规范型号同时归属多个 Firmware 变体：{device}:"
                + ",".join(sorted(selected))
            )
        if selected and missing:
            raise ValueError(f"RCU 规范型号同时包含已有和缺失来源：{device}")
        if selected:
            selected_variant = next(iter(selected.values()))
            variant_id = str(selected_variant["id"])
            canonical_instances = {
                name: list(items)
                for name, items in instances_by_variant[variant_id].items()
            }
            for name in svd_peripherals:
                canonical_instances.setdefault(name, [{"name": name, "source": "svd"}])
            device_gate_issues = []
            device_binding_issues = []
            omitted_gates = []
            device_gates: dict[str, list[dict[str, object]]] = {
                "enable": [],
                "reset": [],
            }
            for kind in ("enable", "reset"):
                resolved_by_entry = {
                    (
                        str(gate["name"]),
                        int(gate["register_offset"]),
                        int(gate["bit"]),
                    ): gate
                    for gate in selected_variant[kind]
                }
                for raw_entry in selected_variant[f"raw_{kind}"]:
                    entry = dict(raw_entry)
                    key = (
                        str(entry["name"]),
                        int(entry["register_offset"]),
                        int(entry["bit"]),
                    )
                    source_gate = resolved_by_entry.get(key)
                    if source_gate is None or source_gate.get("resolution") == "firmware-enum":
                        corroborated, _ = resolve_firmware_field_gate(
                            selected_variant, kind, entry
                        )
                        if corroborated is None:
                            corroborated, _ = resolve_svd_gate(svd_rcu_gates, kind, entry)
                        if corroborated is not None:
                            source_gate = corroborated
                    if source_gate is None:
                        _, issue = resolve_svd_gate(svd_rcu_gates, kind, entry)
                        if issue is not None:
                            device_gate_issues.append(issue)
                            continue
                    gate = dict(source_gate)
                    binding, issue = bind_gate(
                        canonical_instances, str(gate["name"])
                    )
                    gate["binding"] = binding
                    if issue is not None:
                        svd_binding, svd_issue = bind_gate(
                            {
                                name: [{"name": name, "source": "svd"}]
                                for name in svd_peripherals
                            },
                            str(gate["name"]),
                        )
                        has_svd_gate = any(
                            row.get("kind") == kind
                            and row.get("name") == gate["name"]
                            for row in svd_rcu_gates
                        )
                        if (
                            svd_sha256s
                            and svd_binding is None
                            and svd_issue is not None
                            and not has_svd_gate
                        ):
                            omitted_gates.append(
                                {
                                    "kind": kind,
                                    "name": gate["name"],
                                    "firmware_gate": gate,
                                    "resolution": "current-pack-supersedes-firmware",
                                    "svd_sha256s": sorted(svd_sha256s),
                                }
                            )
                            continue
                        device_binding_issues.append({"kind": kind, **issue})
                    device_gates[kind].append(gate)
            gate_status = "conflict" if device_gate_issues else "normalized"
            binding_status = (
                "conflict"
                if gate_status == "conflict" or device_binding_issues
                else "normalized"
            )
            device_issues = [*device_gate_issues, *device_binding_issues]
            public_device = {
                "id": device,
                "status": binding_status,
                "gate_status": gate_status,
                "binding_status": binding_status,
                "variant": variant_id,
                "svd_sha256s": sorted(svd_sha256s),
                "peripheral_instances": len(canonical_instances),
                "enable_gates": len(device_gates["enable"]),
                "reset_gates": len(device_gates["reset"]),
                "unbound_gates": len(device_issues),
            }
            devices.append(public_device)
            detailed_devices.append(
                {
                    **public_device,
                    "peripheral_names": sorted(canonical_instances),
                    "enable": device_gates["enable"],
                    "reset": device_gates["reset"],
                    "omitted_gates": omitted_gates,
                    "issues": device_issues,
                }
            )
            continue
        direct_svd_gates = [
            gate
            for gate in svd_rcu_gates
            if str(gate["register"]).endswith("EN" if gate["kind"] == "enable" else "RST")
        ]
        if direct_svd_gates:
            instances = {name: [{"name": name, "source": "svd"}] for name in svd_peripherals}
            device_gates = {"enable": [], "reset": []}
            issues = []
            for source_gate in direct_svd_gates:
                gate = dict(source_gate)
                binding, issue = bind_gate(instances, str(gate["name"]))
                gate["binding"] = binding
                if issue is not None:
                    issues.append({"kind": gate["kind"], **issue})
                device_gates[str(gate["kind"])].append(gate)
            status = "conflict" if issues else "normalized"
            public_device = {
                "id": device,
                "status": status,
                "gate_status": "normalized",
                "binding_status": status,
                "variant": None,
                "svd_sha256s": sorted(svd_sha256s),
                "peripheral_instances": len(svd_peripherals),
                "enable_gates": len(device_gates["enable"]),
                "reset_gates": len(device_gates["reset"]),
                "unbound_gates": len(issues),
                "reason": "svd",
            }
            devices.append(public_device)
            detailed_devices.append(
                {
                    **public_device,
                    "peripheral_names": sorted(svd_peripherals),
                    "enable": device_gates["enable"],
                    "reset": device_gates["reset"],
                    "issues": issues,
                }
            )
            continue
        reasons = {str(item["reason"]) for item in missing}
        if len(reasons) != 1:
            raise ValueError(f"RCU 规范型号缺少唯一 Firmware 缺口来源：{device}")
        public_device = {
            "id": device,
            "status": "missing",
            "gate_status": "missing",
            "binding_status": "missing",
            "variant": None,
            "svd_sha256s": sorted(svd_sha256s),
            "peripheral_instances": len(svd_peripherals),
            "enable_gates": 0,
            "reset_gates": 0,
            "unbound_gates": 0,
            "reason": next(iter(reasons)),
        }
        devices.append(public_device)
        detailed_devices.append(
            {
                **public_device,
                "peripheral_names": sorted(svd_peripherals),
                "enable": [],
                "reset": [],
                "issues": [],
            }
        )

    summary = {
        "normalized_devices": len(devices),
        "variants": len(normalized_variants),
        "variants_with_normalized_gate_table": sum(
            variant["gate_status"] == "normalized"
            for variant in normalized_variants
        ),
        "variants_with_gate_table_conflict": sum(
            variant["gate_status"] == "conflict" for variant in normalized_variants
        ),
        "variants_with_normalized_rcu": sum(
            variant["status"] == "normalized" for variant in normalized_variants
        ),
        "variants_with_rcu_conflict": sum(
            variant["status"] == "conflict" for variant in normalized_variants
        ),
        "devices_with_normalized_gate_table": sum(
            device["gate_status"] == "normalized" for device in devices
        ),
        "devices_with_gate_table_conflict": sum(
            device["gate_status"] == "conflict" for device in devices
        ),
        "devices_with_normalized_rcu": sum(
            device["status"] == "normalized" for device in devices
        ),
        "devices_with_rcu_conflict": sum(
            device["status"] == "conflict" for device in devices
        ),
        "devices_without_rcu_source": sum(
            device["status"] == "missing" for device in devices
        ),
        "devices_with_svd_only_rcu": sum(
            device.get("reason") == "svd" for device in devices
        ),
        "enable_gates": sum(len(variant["enable"]) for variant in normalized_variants),
        "reset_gates": sum(len(variant["reset"]) for variant in normalized_variants),
        "bound_enable_gates": sum(
            gate["binding"] is not None
            and gate["binding"]["kind"] == "peripheral"
            for variant in normalized_variants
            for gate in variant["enable"]
        ),
        "bound_reset_gates": sum(
            gate["binding"] is not None
            and gate["binding"]["kind"] == "peripheral"
            for variant in normalized_variants
            for gate in variant["reset"]
        ),
        "system_gates": sum(
            gate["binding"] is not None and gate["binding"]["kind"] == "system"
            for variant in normalized_variants
            for gate in [*variant["enable"], *variant["reset"]]
        ),
        "unbound_gates": sum(
            gate["binding"] is None
            for variant in normalized_variants
            for gate in [*variant["enable"], *variant["reset"]]
        ),
        "issues": sum(len(variant["issues"]) for variant in normalized_variants),
    }
    full = {
        "schema_version": 1,
        "variants": normalized_variants,
        "devices": detailed_devices,
    }
    report = {
        "schema_version": 1,
        "summary": summary,
        "variants": public_variants,
        "devices": devices,
    }
    return full, report


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variants",
        type=Path,
        default=repo_root / "reports/gigadevice-merged-firmware-variants.json",
    )
    parser.add_argument(
        "--models",
        type=Path,
        default=repo_root / "reports/gigadevice-models.json",
    )
    parser.add_argument(
        "--resources",
        type=Path,
        default=repo_root / "reports/gigadevice-pack-resources.json",
    )
    parser.add_argument(
        "--svds",
        type=Path,
        default=repo_root / "reports/gigadevice-svd-audit.json",
    )
    parser.add_argument(
        "--iar",
        type=Path,
        default=repo_root / "reports/gigadevice-iar-a7.json",
    )
    parser.add_argument(
        "--iar-svds",
        type=Path,
        default=repo_root / "reports/gigadevice-iar-svd-audit.json",
    )
    parser.add_argument(
        "--normalized-output",
        type=Path,
        default=repo_root / ".cache/normalized/gigadevice-rcu.json",
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-rcu.json"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    full, report = build_outputs(
        json.loads(args.variants.read_text(encoding="utf-8")),
        json.loads(args.models.read_text(encoding="utf-8")),
        json.loads(args.resources.read_text(encoding="utf-8")),
        json.loads(args.svds.read_text(encoding="utf-8")),
        json.loads(args.iar.read_text(encoding="utf-8")),
        json.loads(args.iar_svds.read_text(encoding="utf-8")),
    )
    common._write_text_atomic(
        args.normalized_output,
        json.dumps(full, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    report["provenance"] = {
        "firmware_variants": {
            "path": args.variants.name,
            "sha256": common._sha256(args.variants),
        },
        "models": {
            "path": args.models.name,
            "sha256": common._sha256(args.models),
        },
        "resources": {
            "path": args.resources.name,
            "sha256": common._sha256(args.resources),
        },
        "svds": {
            "path": args.svds.name,
            "sha256": common._sha256(args.svds),
        },
        "iar": {"path": args.iar.name, "sha256": common._sha256(args.iar)},
        "iar_svds": {
            "path": args.iar_svds.name,
            "sha256": common._sha256(args.iar_svds),
        },
        "normalized_data": {
            "path": args.normalized_output.name,
            "sha256": common._sha256(args.normalized_output),
        },
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = report["summary"]
    if int(summary["normalized_devices"]) < 657 or int(summary["variants"]) < 59:
        raise ValueError("RCU 归一覆盖低于门限")
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"RCU 归一报告：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
