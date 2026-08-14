#!/usr/bin/env python3
"""把 Firmware DMA/DMAMUX 事实归一到规范 GD32 型号。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import gigadevice_sources as common
import extract_gigadevice_manual_dma as manual_source


DMA_INSTANCE_RE = re.compile(r"DMA[0-9]*")
DMA_INTERRUPT_RE = re.compile(r"(DMA[0-9]*)_Channel([0-9]+)")
SYSTEM_REQUEST_RE = re.compile(r"M2M|GENERATOR[0-9]+")


def manual_families(name: str) -> list[str]:
    families = re.findall(r"GD32[A-Z][A-Z0-9x]+", name, re.IGNORECASE)
    if re.search(r"(?:^|[ _])F5HC(?:$|[ _])", name, re.IGNORECASE):
        families.append("GD32F5HC")
    return list(dict.fromkeys(families))


def series_matches(family: str, series: str) -> bool:
    family_key = family.casefold()
    series_key = series.casefold()
    return family_key == series_key or series_key.startswith(family_key.rstrip("x"))


def normalize_manual_signals(name: str) -> list[str]:
    spi_i2s = re.fullmatch(r"SPI(\d*)/I2S(\d+)_(RX|TX)", name)
    if spi_i2s is not None:
        spi, i2s, signal = spi_i2s.groups()
        return [f"SPI{spi or i2s}_{signal}"]
    signals = name.split("/")
    normalized = []
    for signal in signals:
        if signal in {"QUANSPI", "QUADSPI"}:
            signal = "QSPI"
        signal = re.sub(r"^I2S(\d+)_?ADD_", r"I2S\1_ADD_", signal)
        normalized.append(signal)
    return normalized


def build_remap(
    variant: dict[str, object], signal: str, footnote: int
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    if footnote not in {1, 2}:
        return None, {"reason": "dma-remap-footnote-invalid", "footnote": footnote}
    matches = []
    for layout in variant.get("layouts", []):
        for field in layout.get("fields", []):
            name = str(field["name"])
            register = str(field["register"])
            if not name.endswith("_DMA_RMP") or int(field["bit_size"]) != 1:
                continue
            prefix = register + "_"
            if not name.startswith(prefix):
                continue
            key = name[len(prefix) : -len("_DMA_RMP")]
            if signal == key or signal.startswith(key + "_"):
                matches.append((len(key), layout, field))
    if not matches:
        return None, {"reason": "dma-remap-field-missing", "signal": signal}
    best_length = max(length for length, _, _ in matches)
    best = [(layout, field) for length, layout, field in matches if length == best_length]
    if len(best) != 1:
        return None, {"reason": "dma-remap-field-ambiguous", "signal": signal}
    layout, field = best[0]
    return (
        {
            "instance": "SYSCFG",
            "register": field["register"],
            "field": field["name"],
            "value": footnote - 1,
            "source": layout.get("sources", []),
        },
        None,
    )


def _manual_for_device(
    manuals: list[dict[str, object]], series: str
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    matches = []
    for manual in manuals:
        families = [
            family
            for family in manual_families(str(manual["name"]))
            if series_matches(family, series)
        ]
        if families:
            score = max(
                (sum(character.casefold() != "x" for character in family), len(family))
                for family in families
            )
            matches.append((score, manual))
    if matches:
        best_score = max(score for score, _ in matches)
        best = [manual for score, manual in matches if score == best_score]
        if len(best) == 1:
            return best[0], None
    else:
        best = []
    return None, {
        "reason": "fixed-manual-missing" if not best else "fixed-manual-ambiguous",
        "candidates": sorted(str(manual["name"]) for manual in best),
    }


def build_fixed_dma(
    variant: dict[str, object],
    device: str,
    peripheral_names: set[str],
    manuals: list[dict[str, object]],
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    manual, manual_issue = _manual_for_device(manuals, str(variant["series"]))
    if manual_issue is not None or manual is None:
        return None, [manual_issue]
    tables = [
        table
        for table in manual.get("tables", [])
        if not table.get("applies_to")
        or any(
            manual_source.family_matches(str(family), device)
            for family in table["applies_to"]
        )
    ]
    if not tables:
        return None, [{"reason": "fixed-manual-table-missing"}]
    instance_names = {str(instance["name"]) for instance in variant["instances"]}
    issues = []
    channels_by_controller = {}
    requests = []
    for table in tables:
        controller = str(table["controller"])
        if controller not in instance_names:
            issues.append(
                {"reason": "fixed-dma-instance-missing", "controller": controller}
            )
            continue
        if not any(
            str(interrupt["name"]).startswith(f"{controller}_Channel")
            for interrupt in variant["interrupts"]
        ):
            continue
        channels = list(map(int, table["channels"]))
        if channels != list(range(len(channels))):
            issues.append(
                {
                    "reason": "fixed-dma-channel-sequence-conflict",
                    "controller": controller,
                    "channels": channels,
                }
            )
        channels_by_controller[controller] = channels
        for source_route in table["routes"]:
            for signal in normalize_manual_signals(str(source_route["signal"])):
                binding, binding_issue = bind_request(peripheral_names, signal)
                if binding is None and binding_issue["reason"] == "peripheral-missing":
                    continue
                if binding_issue is not None:
                    issues.append(
                        {
                            "reason": binding_issue["reason"],
                            "signal": signal,
                            "table": table["number"],
                        }
                    )
                    continue
                route = {
                    "name": signal,
                    "dma": controller,
                    "channel": int(source_route["channel"]),
                    "binding": binding,
                    "source": {
                        "manual": manual["name"],
                        "pdf": manual.get("pdf"),
                        "table": table["number"],
                        "page": source_route["source"]["page"],
                        "raw_signal": source_route["signal"],
                    },
                }
                if "request" in source_route:
                    route["request"] = int(source_route["request"])
                if "footnote" in source_route:
                    remap, remap_issue = build_remap(
                        variant, signal, int(source_route["footnote"])
                    )
                    if remap_issue is not None:
                        issues.append({"table": table["number"], **remap_issue})
                        continue
                    route["remap"] = remap
                requests.append(route)
    unique = {}
    for request in requests:
        key = (
            request["dma"],
            request["channel"],
            request.get("request"),
            request["binding"]["peripheral"],
            request["binding"]["signal"],
            json.dumps(request.get("remap"), sort_keys=True),
        )
        unique.setdefault(key, request)
    requests = sorted(
        unique.values(),
        key=lambda request: (
            str(request["dma"]),
            int(request["channel"]),
            int(request.get("request", -1)),
            str(request["name"]),
        ),
    )
    if not requests:
        issues.append({"reason": "fixed-request-map-empty"})
    return {
        "channels_by_controller": channels_by_controller,
        "requests": requests,
        "manual": manual["name"],
    }, issues


def build_fixed_channels(
    variant: dict[str, object], channels_by_controller: dict[str, list[int]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = []
    issues = []
    for controller, channels in sorted(channels_by_controller.items()):
        for channel in channels:
            candidates = []
            prefix = f"{controller}_Channel"
            for interrupt in variant["interrupts"]:
                name = str(interrupt["name"])
                if not name.startswith(prefix):
                    continue
                if channel in map(int, re.findall(r"\d+", name[len(prefix) :])):
                    candidates.append(interrupt)
            exact = [
                interrupt
                for interrupt in candidates
                if interrupt["name"] == f"{controller}_Channel{channel}"
            ]
            if exact:
                candidates = exact
            if not candidates:
                issues.append(
                    {
                        "reason": "dma-channel-interrupt-missing",
                        "dma": controller,
                        "channel": channel,
                    }
                )
                continue
            numbers = {int(interrupt["value"]) for interrupt in candidates}
            if len(numbers) != 1:
                issues.append(
                    {
                        "reason": "dma-channel-interrupt-conflict",
                        "dma": controller,
                        "channel": channel,
                    }
                )
                continue
            interrupt = sorted(candidates, key=lambda item: str(item["name"]))[0]
            rows.append(
                {
                    "name": f"{controller}_CH{channel}",
                    "dma": controller,
                    "channel": channel,
                    "interrupt": interrupt["name"],
                    "interrupt_number": int(interrupt["value"]),
                }
            )
    return rows, issues


def bind_request(
    peripheral_names: set[str], name: str
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    if SYSTEM_REQUEST_RE.fullmatch(name) is not None:
        return {"kind": "system", "signal": name}, None
    if re.fullmatch(r"SSTAT[0-3]", name) is not None and "MFCOM" in peripheral_names:
        return {
            "kind": "peripheral",
            "peripheral": "MFCOM",
            "signal": name,
        }, None
    if re.fullmatch(r"EVIC(?:[0-9]|1[01])", name) is not None and "EVIC" in peripheral_names:
        return {
            "kind": "peripheral",
            "peripheral": "EVIC",
            "signal": name,
        }, None
    edim = f"EDIM_{name}"
    if edim in peripheral_names:
        return {
            "kind": "peripheral",
            "peripheral": edim,
            "signal": name,
        }, None
    for source, peripheral in (("DAC0", "DAC"), ("SPI0", "SPI")):
        if peripheral in peripheral_names and (
            name == source or name.startswith(source + "_")
        ):
            return {
                "kind": "peripheral",
                "peripheral": peripheral,
                "signal": name[len(source) :].removeprefix("_") or peripheral,
            }, None
    candidates = sorted(
        (
            peripheral
            for peripheral in peripheral_names
            if name == peripheral or name.startswith(peripheral + "_")
        ),
        key=lambda peripheral: (-len(peripheral), peripheral),
    )
    if not candidates:
        return None, {
            "name": name,
            "reason": "peripheral-missing",
            "candidates": [],
        }
    longest = len(candidates[0])
    best = [candidate for candidate in candidates if len(candidate) == longest]
    if len(best) != 1:
        return None, {
            "name": name,
            "reason": "peripheral-ambiguous",
            "candidates": best,
        }
    peripheral = best[0]
    signal = name[len(peripheral) :].removeprefix("_") or peripheral
    return {
        "kind": "peripheral",
        "peripheral": peripheral,
        "signal": signal,
    }, None


def build_channels(
    variant: dict[str, object], dma: dict[str, object]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    issues = []
    controller_counts: dict[str, int] = {}
    for instance in variant["instances"]:
        name = str(instance["name"])
        if DMA_INSTANCE_RE.fullmatch(name) is not None:
            controller_counts[name] = controller_counts.get(name, 0) + 1
    controllers = sorted(controller_counts)
    duplicates = [name for name in controllers if controller_counts[name] != 1]
    if duplicates:
        issues.append(
            {
                "reason": "dma-instance-ambiguous",
                "candidates": duplicates,
            }
        )

    channels_by_key: dict[tuple[str, int], dict[str, object]] = {}
    for interrupt in variant["interrupts"]:
        match = DMA_INTERRUPT_RE.fullmatch(str(interrupt["name"]))
        if match is None:
            continue
        dma_name, raw_channel = match.groups()
        channel = int(raw_channel)
        key = (dma_name, channel)
        row = {
            "name": f"{dma_name}_CH{channel}",
            "dma": dma_name,
            "channel": channel,
            "interrupt": interrupt["name"],
            "interrupt_number": int(interrupt["value"]),
        }
        previous = channels_by_key.get(key)
        if previous is not None and previous != row:
            issues.append(
                {
                    "reason": "dma-channel-interrupt-conflict",
                    "dma": dma_name,
                    "channel": channel,
                }
            )
        channels_by_key[key] = row
    channels = [channels_by_key[key] for key in sorted(channels_by_key)]

    if dma["kind"] != "dmamux":
        return channels, issues
    missing_controllers = [
        controller
        for controller in controllers
        if not any(channel["dma"] == controller for channel in channels)
    ]
    unknown_controllers = sorted(
        {str(channel["dma"]) for channel in channels} - set(controllers)
    )
    if missing_controllers or unknown_controllers:
        issues.append(
            {
                "reason": "dma-channel-instance-mismatch",
                "missing": missing_controllers,
                "unknown": unknown_controllers,
            }
        )
    dmamux_instances = [
        instance for instance in variant["instances"] if instance["name"] == "DMAMUX"
    ]
    if len(dmamux_instances) != 1:
        issues.append(
            {
                "reason": (
                    "dmamux-instance-missing"
                    if not dmamux_instances
                    else "dmamux-instance-ambiguous"
                )
            }
        )
    raw_mux_channels = list(map(int, dma["dmamux_channels"]))
    if raw_mux_channels != list(range(len(raw_mux_channels))):
        issues.append(
            {
                "reason": "dmamux-channel-sequence-conflict",
                "channels": raw_mux_channels,
            }
        )
    if len(channels) != len(raw_mux_channels):
        issues.append(
            {
                "reason": "dmamux-dma-channel-count-conflict",
                "dma_channels": len(channels),
                "dmamux_channels": len(raw_mux_channels),
            }
        )
    if issues:
        return channels, issues
    for channel, dmamux_channel in zip(channels, raw_mux_channels, strict=True):
        channel["dmamux"] = "DMAMUX"
        channel["dmamux_channel"] = dmamux_channel
    return channels, issues


def build_mdma(
    variant: dict[str, object],
    mdma: dict[str, object],
    peripheral_names: set[str],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    issues = []
    instances = [
        instance for instance in variant["instances"] if instance["name"] == "MDMA"
    ]
    interrupts = [
        interrupt
        for interrupt in variant["interrupts"]
        if interrupt["name"] == "MDMA"
    ]
    if len(instances) != 1:
        issues.append(
            {
                "reason": (
                    "mdma-instance-missing" if not instances else "mdma-instance-ambiguous"
                )
            }
        )
    if len(interrupts) != 1:
        issues.append(
            {
                "reason": (
                    "mdma-interrupt-missing"
                    if not interrupts
                    else "mdma-interrupt-ambiguous"
                )
            }
        )
    raw_channels = list(map(int, mdma["channels"]))
    if raw_channels != list(range(len(raw_channels))):
        issues.append(
            {"reason": "mdma-channel-sequence-conflict", "channels": raw_channels}
        )
    channels = []
    if len(interrupts) == 1:
        channels = [
            {
                "name": f"MDMA_CH{channel}",
                "dma": "MDMA",
                "channel": channel,
                "interrupt": "MDMA",
                "interrupt_number": int(interrupts[0]["value"]),
            }
            for channel in raw_channels
        ]
    requests = []
    for source_request in mdma["requests"]:
        request = dict(source_request)
        if request["kind"] == "software":
            binding, issue = {"kind": "system", "signal": str(request["name"])}, None
        else:
            binding, issue = bind_request(peripheral_names, str(request["name"]))
        request["binding"] = binding
        if issue is not None:
            issues.append({"request": int(request["request"]), **issue})
        requests.append(request)
    return channels, requests, issues


def build_outputs(
    variants: dict[str, object],
    rcu: dict[str, object],
    manual_dma: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    raw_variants = variants.get("variants")
    raw_devices = rcu.get("devices")
    source_summary = variants.get("summary")
    raw_manuals = manual_dma.get("manuals")
    if (
        not isinstance(raw_variants, list)
        or not isinstance(raw_devices, list)
        or not isinstance(source_summary, dict)
        or manual_dma.get("schema_version") != 1
        or not isinstance(raw_manuals, list)
    ):
        raise ValueError("Firmware 变体、RCU 设备或 DMA 手册报告格式无效")
    variants_by_id = {str(variant["id"]): variant for variant in raw_variants}
    if len(variants_by_id) != len(raw_variants):
        raise ValueError("Firmware DMA 变体主键重复")

    devices = []
    public_devices = []
    for source_device in sorted(raw_devices, key=lambda item: str(item["id"])):
        device = str(source_device["id"])
        variant_id = source_device.get("variant")
        if variant_id is None:
            public = {
                "id": device,
                "status": "missing",
                "variant": None,
                "kind": None,
                "channels": 0,
                "requests": 0,
                "bound_requests": 0,
                "system_requests": 0,
                "unbound_requests": 0,
                "mdma_channels": 0,
                "mdma_requests": 0,
                "bound_mdma_requests": 0,
                "system_mdma_requests": 0,
                "unbound_mdma_requests": 0,
                "reason": "firmware-source-missing",
            }
            public_devices.append(public)
            devices.append(
                {
                    **public,
                    "dma_channels": [],
                    "dma_requests": [],
                    "mdma_channel_rows": [],
                    "mdma_request_rows": [],
                    "issues": [],
                }
            )
            continue
        variant = variants_by_id.get(str(variant_id))
        if variant is None:
            raise ValueError(f"DMA 设备引用未知 Firmware 变体：{device}: {variant_id}")
        dma = variant.get("dma")
        if not isinstance(dma, dict):
            public = {
                "id": device,
                "status": "missing",
                "variant": variant_id,
                "kind": None,
                "channels": 0,
                "requests": 0,
                "bound_requests": 0,
                "system_requests": 0,
                "unbound_requests": 0,
                "mdma_channels": 0,
                "mdma_requests": 0,
                "bound_mdma_requests": 0,
                "system_mdma_requests": 0,
                "unbound_mdma_requests": 0,
                "reason": "dma-source-missing",
            }
            public_devices.append(public)
            devices.append(
                {
                    **public,
                    "dma_channels": [],
                    "dma_requests": [],
                    "mdma_channel_rows": [],
                    "mdma_request_rows": [],
                    "issues": [],
                }
            )
            continue

        peripheral_names = set(map(str, source_device["peripheral_names"]))
        requests = []
        fixed = None
        if dma["kind"] == "fixed":
            fixed, issues = build_fixed_dma(
                variant, device, peripheral_names, raw_manuals
            )
            if fixed is None:
                channels = []
            else:
                channels, channel_issues = build_fixed_channels(
                    variant, fixed["channels_by_controller"]
                )
                issues.extend(channel_issues)
                requests = fixed["requests"]
        else:
            channels, issues = build_channels(variant, dma)
        mdma = variant.get("mdma")
        mdma_channels = []
        mdma_requests = []
        if isinstance(mdma, dict):
            mdma_channels, mdma_requests, mdma_issues = build_mdma(
                variant, mdma, peripheral_names
            )
            issues.extend(mdma_issues)
        elif any(instance["name"] == "MDMA" for instance in variant["instances"]):
            issues.append({"reason": "mdma-source-missing"})
        if dma["kind"] == "fixed":
            incomplete = fixed is None or any(
                str(issue["reason"]).startswith("fixed-manual")
                or issue["reason"] == "fixed-request-map-empty"
                for issue in issues
            )
            status = "source-incomplete" if incomplete else (
                "conflict" if issues else "normalized"
            )
        else:
            for source_request in dma["requests"]:
                request = dict(source_request)
                binding, issue = bind_request(peripheral_names, str(request["name"]))
                request["binding"] = binding
                if issue is not None:
                    issues.append({"request": int(request["request"]), **issue})
                requests.append(request)
            status = "conflict" if issues else "normalized"
        bound_requests = sum(
            request["binding"] is not None
            and request["binding"]["kind"] == "peripheral"
            for request in requests
        )
        system_requests = sum(
            request["binding"] is not None
            and request["binding"]["kind"] == "system"
            for request in requests
        )
        bound_mdma_requests = sum(
            request["binding"] is not None
            and request["binding"]["kind"] == "peripheral"
            for request in mdma_requests
        )
        system_mdma_requests = sum(
            request["binding"] is not None
            and request["binding"]["kind"] == "system"
            for request in mdma_requests
        )
        public = {
            "id": device,
            "status": status,
            "variant": variant_id,
            "kind": dma["kind"],
            "channels": len(channels),
            "requests": len(requests),
            "bound_requests": bound_requests,
            "system_requests": system_requests,
            "unbound_requests": len(requests) - bound_requests - system_requests,
            "mdma_channels": len(mdma_channels),
            "mdma_requests": len(mdma_requests),
            "bound_mdma_requests": bound_mdma_requests,
            "system_mdma_requests": system_mdma_requests,
            "unbound_mdma_requests": (
                len(mdma_requests) - bound_mdma_requests - system_mdma_requests
            ),
        }
        public_devices.append(public)
        devices.append(
            {
                **public,
                "source": dma["source"],
                "mdma_source": mdma["source"] if isinstance(mdma, dict) else None,
                "dma_channels": channels,
                "dma_requests": requests,
                "mdma_channel_rows": mdma_channels,
                "mdma_request_rows": mdma_requests,
                "issues": issues,
            }
        )

    if len(devices) != int(source_summary["normalized_devices"]):
        raise ValueError("DMA 规范设备闭包与 Firmware 变体报告不一致")
    public_variants = [
        {
            "id": variant["id"],
            "series": variant["series"],
            "devices": variant["devices"],
            "kind": variant["dma"]["kind"] if variant["dma"] is not None else None,
            "channels": (
                len(variant["dma"]["dmamux_channels"])
                if variant["dma"] is not None
                else 0
            ),
            "requests": (
                len(variant["dma"]["requests"])
                if variant["dma"] is not None
                else 0
            ),
            "mdma_channels": (
                len(variant["mdma"]["channels"])
                if isinstance(variant.get("mdma"), dict)
                else 0
            ),
            "mdma_requests": (
                len(variant["mdma"]["requests"])
                if isinstance(variant.get("mdma"), dict)
                else 0
            ),
            "source": variant["dma"]["source"] if variant["dma"] is not None else None,
            "mdma_source": (
                variant["mdma"]["source"]
                if isinstance(variant.get("mdma"), dict)
                else None
            ),
        }
        for variant in sorted(raw_variants, key=lambda item: str(item["id"]))
    ]
    summary = {
        "normalized_devices": len(devices),
        "variants": len(raw_variants),
        "variants_with_dma_source": sum(
            variant["dma"] is not None for variant in raw_variants
        ),
        "variants_with_dmamux": sum(
            variant["dma"] is not None and variant["dma"]["kind"] == "dmamux"
            for variant in raw_variants
        ),
        "variants_with_fixed_dma": sum(
            variant["dma"] is not None and variant["dma"]["kind"] == "fixed"
            for variant in raw_variants
        ),
        "variants_with_mdma": sum(
            isinstance(variant.get("mdma"), dict) for variant in raw_variants
        ),
        "devices_with_normalized_dma": sum(
            device["status"] == "normalized" for device in devices
        ),
        "devices_with_dma_conflict": sum(
            device["status"] == "conflict" for device in devices
        ),
        "devices_with_fixed_request_map_missing": sum(
            device["status"] == "source-incomplete" for device in devices
        ),
        "devices_without_dma_source": sum(
            device["status"] == "missing" for device in devices
        ),
        "dmamux_channels": sum(
            len(variant["dma"]["dmamux_channels"])
            for variant in raw_variants
            if variant["dma"] is not None
        ),
        "dmamux_requests": sum(
            len(variant["dma"]["requests"])
            for variant in raw_variants
            if variant["dma"] is not None
        ),
        "mdma_channels": sum(
            len(variant["mdma"]["channels"])
            for variant in raw_variants
            if isinstance(variant.get("mdma"), dict)
        ),
        "mdma_requests": sum(
            len(variant["mdma"]["requests"])
            for variant in raw_variants
            if isinstance(variant.get("mdma"), dict)
        ),
        "device_dma_channels": sum(len(device["dma_channels"]) for device in devices),
        "device_bound_requests": sum(device["bound_requests"] for device in devices),
        "device_system_requests": sum(device["system_requests"] for device in devices),
        "device_unbound_requests": sum(device["unbound_requests"] for device in devices),
        "device_mdma_channels": sum(device["mdma_channels"] for device in devices),
        "device_bound_mdma_requests": sum(
            device["bound_mdma_requests"] for device in devices
        ),
        "device_system_mdma_requests": sum(
            device["system_mdma_requests"] for device in devices
        ),
        "device_unbound_mdma_requests": sum(
            device["unbound_mdma_requests"] for device in devices
        ),
        "issues": sum(len(device["issues"]) for device in devices),
    }
    full = {"schema_version": 1, "variants": public_variants, "devices": devices}
    report = {
        "schema_version": 1,
        "summary": summary,
        "variants": public_variants,
        "devices": public_devices,
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
        "--rcu",
        type=Path,
        default=repo_root / ".cache/normalized/gigadevice-rcu.json",
    )
    parser.add_argument(
        "--manual-dma",
        type=Path,
        default=repo_root / "reports/gigadevice-manual-dma.json",
    )
    parser.add_argument(
        "--normalized-output",
        type=Path,
        default=repo_root / ".cache/normalized/gigadevice-dma.json",
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-dma.json"
    )
    parser.add_argument("--maximum-conflicts", type=int, default=0)
    parser.add_argument("--maximum-fixed-missing", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    full, report = build_outputs(
        json.loads(args.variants.read_text(encoding="utf-8")),
        json.loads(args.rcu.read_text(encoding="utf-8")),
        json.loads(args.manual_dma.read_text(encoding="utf-8")),
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
        "rcu": {"path": args.rcu.name, "sha256": common._sha256(args.rcu)},
        "manual_dma": {
            "path": args.manual_dma.name,
            "sha256": common._sha256(args.manual_dma),
        },
    }
    common._write_text_atomic(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = report["summary"]
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"DMA 归一报告：{args.output}")
    if int(summary["devices_with_dma_conflict"]) > args.maximum_conflicts:
        raise ValueError("DMA 冲突设备超过门限")
    if (
        int(summary["devices_with_fixed_request_map_missing"])
        > args.maximum_fixed_missing
    ):
        raise ValueError("固定 DMA 映射缺失设备超过门限")
    status_total = sum(
        int(summary[key])
        for key in (
            "devices_with_normalized_dma",
            "devices_with_dma_conflict",
            "devices_with_fixed_request_map_missing",
            "devices_without_dma_source",
        )
    )
    if status_total != int(summary["normalized_devices"]):
        raise ValueError("DMA 设备状态未闭合全部规范型号")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
