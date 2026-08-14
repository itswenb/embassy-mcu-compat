#!/usr/bin/env python3
"""把 cmsis-rust-target-db 输出收敛为每个 Pack 的最新版本。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import gigadevice_sources as common


def _version(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"Pack 版本不是三段语义版本：{value!r}")
    return tuple(map(int, parts))  # type: ignore[return-value]


def latest_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    latest: dict[tuple[str, str], tuple[int, int, int]] = {}
    for record in records:
        key = (str(record["source_pack_vendor"]), str(record["source_pack_name"]))
        version = _version(str(record["source_pack_version"]))
        latest[key] = max(latest.get(key, version), version)
    return [
        record
        for record in records
        if _version(str(record["source_pack_version"]))
        == latest[(str(record["source_pack_vendor"]), str(record["source_pack_name"]))]
    ]


def canonical_repository(value: str) -> str:
    match = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", value)
    if match is None:
        match = re.fullmatch(r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?", value)
    if match is not None:
        return f"https://github.com/{match.group(1)}"
    return value.rstrip("/").removesuffix(".git")


def filter_file(
    input_path: Path,
    output_dir: Path,
    *,
    generator: dict[str, str] | None = None,
    manifest: Path | None = None,
) -> None:
    if generator is not None:
        generator = {
            "repository": canonical_repository(generator.get("repository", "")),
            "revision": generator.get("revision", ""),
        }
    if generator is not None and (
        not generator.get("repository")
        or re.fullmatch(r"[0-9a-f]{40}", generator.get("revision", "")) is None
    ):
        raise ValueError("生成器来源必须包含仓库地址和 40 位 Git revision")
    if manifest is not None and generator is None:
        raise ValueError("写入 target DB 锁文件时必须提供生成器来源")
    records = [
        json.loads(line)
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    latest = latest_records(records)
    if not latest:
        raise ValueError("最新 Pack 数据为空")
    packs = sorted(
        {
            (
                str(record["source_pack_vendor"]),
                str(record["source_pack_name"]),
                str(record["source_pack_version"]),
            )
            for record in latest
        }
    )
    devices_text = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in latest)
    metadata = {
        "schema_version": 1,
        "source_record_count": len(records),
        "record_count": len(latest),
        "unique_device_count": len({str(record["device"]) for record in latest}),
        "pack_count": len(packs),
        "generator": generator,
        "packs": [
            {"vendor": vendor, "name": name, "version": version}
            for vendor, name, version in packs
        ],
    }
    common._write_text_atomic(output_dir / "devices.jsonl", devices_text)
    common._write_text_atomic(
        output_dir / "metadata.json",
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    if manifest is not None:
        lock = {
            "schema_version": 1,
            "generator": generator,
            "input": {
                "filename": input_path.name,
                "sha256": common._sha256(input_path),
            },
            "outputs": {
                "devices_sha256": common._sha256(output_dir / "devices.jsonl"),
                "metadata_sha256": common._sha256(output_dir / "metadata.json"),
            },
            "summary": {
                "pack_count": len(packs),
                "record_count": len(latest),
                "unique_device_count": metadata["unique_device_count"],
            },
        }
        common._write_text_atomic(
            manifest, json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--generator-repository")
    parser.add_argument("--generator-revision")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if bool(args.generator_repository) != bool(args.generator_revision):
        raise ValueError("生成器仓库和 revision 必须同时提供")
    generator = (
        {"repository": args.generator_repository, "revision": args.generator_revision}
        if args.generator_repository
        else None
    )
    filter_file(args.input, args.output_dir, generator=generator, manifest=args.manifest)
    metadata = json.loads((args.output_dir / "metadata.json").read_text(encoding="utf-8"))
    print(
        f"已保留最新 Pack：packs={metadata['pack_count']} records={metadata['record_count']} "
        f"devices={metadata['unique_device_count']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
