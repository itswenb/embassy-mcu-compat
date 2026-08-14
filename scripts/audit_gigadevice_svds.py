#!/usr/bin/env python3
"""用固定 chiptool 解析并生成所有最新 GD32 SVD 的 PAC 缓存。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

import gigadevice_sources as common


NORMALIZATION_VERSION = 4
GENERATION_VERSION = 2


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    child = next((item for item in element if _tag(item) == name), None)
    return child.text.strip() if child is not None and child.text else None


def normalized_svd_bytes(path: Path) -> tuple[bytes, list[str]]:
    data = path.read_bytes()
    transformations = []
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
        transformations.append("remove-utf8-bom")
    stripped = data.lstrip(b" \t\r\n")
    if stripped != data:
        data = stripped
        transformations.append("remove-leading-whitespace")
    for source, target in ((b"read", b"read-only"), (b"write", b"write-only")):
        pattern = rb"<access>\s*" + source + rb"\s*</access>"
        data, count = re.subn(pattern, b"<access>" + target + b"</access>", data)
        if count:
            transformations.append(f"access-{source.decode()}-alias:{count}")
    identifier_count = 0

    def normalize_identifier(match: re.Match[bytes]) -> bytes:
        nonlocal identifier_count
        compact = re.sub(rb"\s+", b"", match.group(2))
        identifier_count += compact != match.group(2)
        return match.group(1) + compact + match.group(3)

    data = re.sub(rb"(<name>)([^<]*)(</name>)", normalize_identifier, data)
    if identifier_count:
        transformations.append(f"identifier-whitespace:{identifier_count}")

    root = ET.fromstring(data)
    peripheral_parents = {}
    for peripheral in (item for item in root.iter() if _tag(item) == "peripheral"):
        peripheral_name = _child_text(peripheral, "name")
        parent = peripheral.attrib.get("derivedFrom")
        if peripheral_name and parent:
            peripheral_parents[peripheral_name] = parent

    def root_peripheral(name: str) -> str:
        seen = set()
        while name in peripheral_parents:
            if name in seen:
                raise ValueError(f"SVD 外设 derivedFrom 形成循环：{name}")
            seen.add(name)
            name = peripheral_parents[name]
        return name

    flattened_count = 0

    def flatten_peripheral(match: re.Match[bytes]) -> bytes:
        nonlocal flattened_count
        parent = match.group(2).decode("utf-8")
        root_name = root_peripheral(parent)
        flattened_count += root_name != parent
        return match.group(1) + root_name.encode("utf-8") + match.group(3)

    data = re.sub(
        rb'(<peripheral\b[^>]*\bderivedFrom=")([^"]+)(")', flatten_peripheral, data
    )
    if flattened_count:
        transformations.append(f"flatten-peripheral-derived-from:{flattened_count}")

    register_offsets = {}
    for peripheral in (item for item in root.iter() if _tag(item) == "peripheral"):
        peripheral_name = _child_text(peripheral, "name")
        if not peripheral_name:
            continue
        for register in (item for item in peripheral.iter() if _tag(item) == "register"):
            register_name = _child_text(register, "name")
            offset = _child_text(register, "addressOffset")
            if register_name and offset:
                register_offsets[f"{peripheral_name}.{register_name}"] = offset
    derived = re.compile(
        rb'(<register\b[^>]*\bderivedFrom="([^"]*\.([A-Za-z_][A-Za-z0-9_]*))"[^>]*>)\s*</register>'
    )

    def complete_derived_register(match: re.Match[bytes]) -> bytes:
        reference = match.group(2).decode("utf-8")
        if reference not in register_offsets:
            raise ValueError(f"SVD 派生寄存器引用不存在：{reference}")
        name = match.group(3)
        offset = register_offsets[reference].encode("utf-8")
        return (
            match.group(1)
            + b"<name>"
            + name
            + b"</name><addressOffset>"
            + offset
            + b"</addressOffset></register>"
        )

    data, count = derived.subn(
        complete_derived_register,
        data,
    )
    if count:
        transformations.append(f"derived-register-name:{count}")
    return data, transformations


def svd_stats(path: Path) -> dict[str, object]:
    data, _ = normalized_svd_bytes(path)
    root = ET.fromstring(data)
    name = _child_text(root, "name")
    peripherals = [element for element in root.iter() if _tag(element) == "peripheral"]
    if not name or not peripherals:
        raise ValueError(f"SVD 缺少 device name 或 peripheral：{path}")
    peripheral_names = []
    peripheral_register_roots = []
    rcu_gates = []
    for peripheral in peripherals:
        peripheral_name = _child_text(peripheral, "name")
        address = _child_text(peripheral, "baseAddress")
        if not peripheral_name or address is None:
            raise ValueError(f"SVD peripheral 缺少 name/baseAddress：{path}")
        try:
            numeric_address = int(address, 0)
        except ValueError as error:
            raise ValueError(f"SVD baseAddress 非法：{path} {address!r}") from error
        peripheral_names.append(peripheral_name)
        peripheral_register_roots.append(
            {
                "address": numeric_address,
                "name": peripheral_name,
                "register_root": peripheral.attrib.get("derivedFrom", peripheral_name),
            }
        )
        if peripheral_name != "RCU" and _child_text(peripheral, "groupName") != "RCU":
            continue
        for register in (item for item in peripheral.iter() if _tag(item) == "register"):
            register_name = _child_text(register, "name")
            raw_offset = _child_text(register, "addressOffset")
            if not register_name or raw_offset is None:
                continue
            register_offset = int(raw_offset, 0)
            for field in (item for item in register.iter() if _tag(item) == "field"):
                field_name = _child_text(field, "name")
                raw_bit = _child_text(field, "bitOffset") or _child_text(field, "lsb")
                raw_width = _child_text(field, "bitWidth")
                if not field_name or raw_bit is None or raw_width != "1":
                    continue
                kind = "enable" if field_name.endswith("EN") else "reset" if field_name.endswith("RST") else None
                if kind is None:
                    continue
                suffix = "EN" if kind == "enable" else "RST"
                rcu_gates.append(
                    {
                        "bit": int(raw_bit, 0),
                        "field": field_name,
                        "kind": kind,
                        "name": field_name[: -len(suffix)],
                        "register": register_name,
                        "register_offset": register_offset,
                    }
                )
    if len(peripheral_names) != len(set(peripheral_names)):
        raise ValueError(f"SVD peripheral 名称重复：{path}")

    interrupts = [element for element in root.iter() if _tag(element) == "interrupt"]
    interrupt_values = []
    interrupt_vectors = {}
    for interrupt in interrupts:
        interrupt_name = _child_text(interrupt, "name")
        value = _child_text(interrupt, "value")
        if interrupt_name is None or value is None:
            raise ValueError(f"SVD interrupt 缺少 name/value：{path}")
        try:
            numeric_value = int(value, 0)
        except ValueError as error:
            raise ValueError(f"SVD interrupt value 非法：{path} {value!r}") from error
        interrupt_values.append(numeric_value)
        previous = interrupt_vectors.setdefault(interrupt_name, numeric_value)
        if previous != numeric_value:
            raise ValueError(f"SVD interrupt 名称对应多个编号：{path} {interrupt_name}")
    return {
        "device_name": name,
        "peripherals": len(peripherals),
        "peripheral_names": sorted(peripheral_names),
        "peripheral_register_roots": sorted(
            peripheral_register_roots, key=lambda row: str(row["name"])
        ),
        "interrupts": len(interrupts),
        "interrupt_vectors": [
            {"name": name, "value": value}
            for name, value in sorted(interrupt_vectors.items(), key=lambda row: (row[1], row[0]))
        ],
        "unique_interrupt_values": len(set(interrupt_values)),
        "registers": sum(_tag(element) == "register" for element in root.iter()),
        "fields": sum(_tag(element) == "field" for element in root.iter()),
        "rcu_gates": sorted(
            rcu_gates,
            key=lambda row: (
                str(row["kind"]),
                int(row["register_offset"]),
                int(row["bit"]),
                str(row["field"]),
            ),
        ),
    }


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _build_chiptool(root: Path, target_dir: Path) -> tuple[Path, str]:
    revision = _git(root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("chiptool checkout 不是完整 Git revision")
    if _git(root, "status", "--porcelain"):
        raise ValueError("chiptool checkout 不干净")
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--release",
            "--manifest-path",
            str(root / "Cargo.toml"),
            "--target-dir",
            str(target_dir),
        ],
        check=True,
    )
    binary = target_dir / "release/chiptool"
    if not binary.is_file():
        raise ValueError(f"chiptool 构建后缺少二进制：{binary}")
    return binary, revision


def _verify_cache(
    path: Path, svd_sha256: str, normalized_sha256: str, revision: str
) -> dict[str, object] | None:
    marker = path / "source.json"
    if not marker.is_file():
        return None
    data = json.loads(marker.read_text(encoding="utf-8"))
    if (
        data.get("schema_version") != GENERATION_VERSION
        or data.get("cache_key") != path.name
        or data.get("svd_sha256") != svd_sha256
        or data.get("normalized_svd_sha256") != normalized_sha256
        or data.get("chiptool_revision") != revision
        or data.get("normalization_version") != NORMALIZATION_VERSION
    ):
        return None
    for name in ("lib.rs", "device.x"):
        output = path / name
        expected = data.get("outputs", {}).get(name, {}).get("sha256")
        if not output.is_file() or common._sha256(output) != expected:
            return None
    return data


def _generate(
    binary: Path,
    svd: Path,
    svd_sha256: str,
    normalized_sha256: str,
    revision: str,
    cache_dir: Path,
) -> tuple[str, dict[str, object]]:
    output = cache_dir / (
        f"g{GENERATION_VERSION}-n{NORMALIZATION_VERSION}-"
        f"{svd_sha256[:16]}-{revision[:12]}"
    )
    cached = _verify_cache(output, svd_sha256, normalized_sha256, revision)
    if cached is not None:
        return "cached", cached
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".chiptool-", dir=cache_dir) as directory:
        workspace = Path(directory)
        temporary = workspace / "candidate"
        temporary.mkdir()
        result = subprocess.run(
            [str(binary), "generate", "--svd", str(svd.resolve()), "--no-defmt"],
            cwd=temporary,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            return "failed", {
                "error": (result.stderr or result.stdout).strip()[-4000:],
                "exit_code": result.returncode,
            }
        outputs = {}
        for name in ("lib.rs", "device.x"):
            path = temporary / name
            if not path.is_file():
                raise ValueError(f"chiptool 未生成 {name}：{svd}")
            outputs[name] = {
                "sha256": common._sha256(path),
                "size": path.stat().st_size,
            }
        marker = {
            "schema_version": GENERATION_VERSION,
            "cache_key": output.name,
            "svd_sha256": svd_sha256,
            "normalized_svd_sha256": normalized_sha256,
            "normalization_version": NORMALIZATION_VERSION,
            "chiptool_revision": revision,
            "outputs": outputs,
        }
        common._write_text_atomic(
            temporary / "source.json",
            json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        stale = workspace / "stale"
        if output.exists() or output.is_symlink():
            if output.is_symlink() or not output.is_dir():
                raise ValueError(f"chiptool 缓存不是安全目录：{output}")
            output.rename(stale)
        try:
            temporary.rename(output)
        except BaseException:
            if stale.exists() and not output.exists():
                stale.rename(output)
            raise
    return "generated", marker


def _prepare_svd(path: Path, source_sha256: str, cache_dir: Path) -> tuple[Path, str, list[str]]:
    data, transformations = normalized_svd_bytes(path)
    normalized_sha256 = hashlib.sha256(data).hexdigest()
    if not transformations:
        return path, normalized_sha256, transformations
    output = cache_dir / f"{source_sha256}.svd"
    if output.is_file():
        if common._sha256(output) != normalized_sha256:
            raise ValueError(f"规范化 SVD 缓存校验失败：{output}")
        return output, normalized_sha256, transformations
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=cache_dir, prefix=".svd-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(output)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return output, normalized_sha256, transformations


def _source_path(pdsc_root: Path, entry: dict[str, object]) -> Path:
    relative = PurePosixPath(str(entry["path"]))
    if relative.is_absolute() or ".." in relative.parts or str(relative) != str(entry["path"]):
        raise ValueError(f"SVD 来源路径不安全：{entry['path']!r}")
    if "source_pdsc_path" in entry:
        pdsc = pdsc_root.joinpath(*PurePosixPath(str(entry["source_pdsc_path"])).parts)
        path = pdsc.parent.joinpath(*relative.parts)
    else:
        path = pdsc_root.joinpath(*relative.parts)
    if not path.is_file():
        raise ValueError(f"SVD 来源不存在：{path}")
    if common._sha256(path) != entry["sha256"] or path.stat().st_size != entry["size"]:
        raise ValueError(f"SVD 来源与资源报告不一致：{path}")
    return path


def audit(args: argparse.Namespace) -> dict[str, object]:
    resources = json.loads(args.resources.read_text(encoding="utf-8"))
    entries = resources["svd_files"]
    if not isinstance(entries, list):
        raise ValueError("Pack 资源报告缺少 svd_files")
    if args.chiptool is None:
        binary, revision = _build_chiptool(args.chiptool_root, args.target_dir)
    else:
        binary = args.chiptool
        if not binary.is_file():
            raise ValueError(f"指定的 chiptool 不存在：{binary}")
        revision = _git(args.chiptool_root, "rev-parse", "HEAD")
    results = []
    for entry in entries:
        assert isinstance(entry, dict)
        source = _source_path(args.pdsc_root, entry)
        svd, normalized_sha256, transformations = _prepare_svd(
            source, str(entry["sha256"]), args.normalized_dir
        )
        stats = svd_stats(svd)
        status, generated = _generate(
            binary,
            svd,
            str(entry["sha256"]),
            normalized_sha256,
            revision,
            args.cache_dir,
        )
        results.append(
            {
                **entry,
                **stats,
                "normalized_sha256": normalized_sha256,
                "normalizations": transformations,
                "status": status,
                "generated": generated,
            }
        )
    failed = sum(result["status"] == "failed" for result in results)
    return {
        "schema_version": 1,
        "chiptool": {
            "repository": "https://github.com/embassy-rs/chiptool",
            "revision": revision,
        },
        "summary": {
            "svd_files": len(results),
            "generated_or_cached": len(results) - failed,
            "failed": failed,
            "peripherals": sum(int(result["peripherals"]) for result in results),
            "interrupts": sum(int(result["interrupts"]) for result in results),
            "registers": sum(int(result["registers"]) for result in results),
            "fields": sum(int(result["fields"]) for result in results),
        },
        "svds": results,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resources", type=Path, default=repo_root / "reports/gigadevice-pack-resources.json"
    )
    parser.add_argument(
        "--pdsc-root",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/addon-packs-v1",
    )
    parser.add_argument(
        "--chiptool-root", type=Path, default=repo_root / ".cache/research/repos/chiptool"
    )
    parser.add_argument("--chiptool", type=Path)
    parser.add_argument(
        "--target-dir", type=Path, default=repo_root / ".cache/tools/chiptool-target"
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=repo_root / ".cache/research/gigadevice/chiptool-svd-v1"
    )
    parser.add_argument(
        "--normalized-dir",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/normalized-svd-v4",
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-svd-audit.json"
    )
    parser.add_argument("--minimum-svd-files", type=int, default=43)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = audit(args)
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"SVD 审计报告：{args.output}")
    if int(summary["svd_files"]) < args.minimum_svd_files or int(summary["failed"]) != 0:
        raise ValueError("SVD/chiptool 生成覆盖未通过门限")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, ET.ParseError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
