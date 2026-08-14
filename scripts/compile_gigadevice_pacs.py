#!/usr/bin/env python3
"""用 rustc 对所有 chiptool GD32 PAC 输出执行可缓存的类型检查。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import gigadevice_sources as common


STUB_VERSION = 1
STUB = """

// 仅用于独立类型检查；正式 PAC 由目标架构依赖提供该 trait。
pub mod cortex_m {
    pub mod interrupt {
        pub unsafe trait InterruptNumber {
            fn number(self) -> u16;
        }
    }
}
""".encode("utf-8")


def compile_source(source: bytes) -> bytes:
    return source + STUB


def parse_rustc_version(output: str) -> str:
    values = dict(
        line.split(": ", 1) for line in output.splitlines() if ": " in line
    )
    try:
        return f"release={values['release']};commit-hash={values['commit-hash']}"
    except KeyError as error:
        raise ValueError("rustc --version --verbose 缺少 release/commit-hash") from error


def _rustc_version() -> str:
    output = subprocess.run(
        ["rustc", "--version", "--verbose"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout
    return parse_rustc_version(output)


def _find_generated(cache: Path, source: dict[str, object]) -> tuple[Path, dict[str, object]]:
    sha256 = str(source["sha256"])
    expected_marker = source["generated"]
    assert isinstance(expected_marker, dict)
    matches = []
    for marker in cache.glob(f"n*-{sha256[:16]}-*/source.json"):
        data = json.loads(marker.read_text(encoding="utf-8"))
        if (
            data.get("svd_sha256") == sha256
            and data.get("normalized_svd_sha256")
            == expected_marker.get("normalized_svd_sha256")
            and data.get("normalization_version")
            == expected_marker.get("normalization_version")
            and data.get("chiptool_revision") == expected_marker.get("chiptool_revision")
        ):
            matches.append((marker.parent, data))
    if len(matches) != 1:
        raise ValueError(f"SVD 必须唯一对应 chiptool 输出：{sha256}，实际 {len(matches)} 个")
    root, marker = matches[0]
    lib = root / "lib.rs"
    expected = marker["outputs"]["lib.rs"]["sha256"]
    if not lib.is_file() or common._sha256(lib) != expected:
        raise ValueError(f"chiptool lib.rs 校验失败：{lib}")
    return lib, marker


def _compile(lib: Path, version: str, cache: Path) -> tuple[str, dict[str, object]]:
    source = compile_source(lib.read_bytes())
    key = hashlib.sha256(
        source + b"\0" + version.encode("utf-8") + f"\0stub={STUB_VERSION}".encode()
    ).hexdigest()
    marker = cache / f"{key}.json"
    if marker.is_file():
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("success") is True and data.get("key") == key:
            return "cached", data
        raise ValueError(f"PAC 类型检查缓存损坏：{marker}")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pac-check-", dir=cache) as directory:
        root = Path(directory)
        source_path = root / "lib.rs"
        with source_path.open("wb") as file:
            file.write(source)
            file.flush()
            os.fsync(file.fileno())
        result = subprocess.run(
            [
                "rustc",
                "--edition=2024",
                "--crate-name",
                "gd32_pac_check",
                "--crate-type=lib",
                "--emit=metadata",
                "--cap-lints=allow",
                "-o",
                str(root / "lib.rmeta"),
                str(source_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    data = {
        "schema_version": 1,
        "key": key,
        "success": result.returncode == 0,
        "source_sha256": common._sha256(lib),
        "rustc": version,
        "stub_version": STUB_VERSION,
    }
    if result.returncode != 0:
        data["error"] = (result.stderr or result.stdout).strip()[-8000:]
        return "failed", data
    common._write_text_atomic(marker, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return "compiled", data


def check(audit: dict[str, object], generated_cache: Path, compile_cache: Path) -> dict[str, object]:
    version = _rustc_version()
    raw_svds = audit["svds"]
    assert isinstance(raw_svds, list)
    results = []
    for svd in raw_svds:
        assert isinstance(svd, dict)
        lib, marker = _find_generated(generated_cache, svd)
        status, details = _compile(lib, version, compile_cache)
        results.append(
            {
                "source_pack_name": svd["source_pack_name"],
                "path": svd["path"],
                "svd_sha256": svd["sha256"],
                "chiptool_revision": marker["chiptool_revision"],
                "status": status,
                "details": details,
            }
        )
    failed = sum(result["status"] == "failed" for result in results)
    return {
        "schema_version": 1,
        "summary": {
            "pac_outputs": len(results),
            "compiled_or_cached": len(results) - failed,
            "failed": failed,
        },
        "rustc": version,
        "pacs": results,
    }


def _parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit", type=Path, default=repo_root / "reports/gigadevice-svd-audit.json"
    )
    parser.add_argument(
        "--generated-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/chiptool-svd-v1",
    )
    parser.add_argument(
        "--compile-cache",
        type=Path,
        default=repo_root / ".cache/research/gigadevice/pac-compile-v1",
    )
    parser.add_argument(
        "--output", type=Path, default=repo_root / "reports/gigadevice-pac-compile.json"
    )
    parser.add_argument("--minimum-pac-outputs", type=int, default=43)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = check(
        json.loads(args.audit.read_text(encoding="utf-8")),
        args.generated_cache,
        args.compile_cache,
    )
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"PAC 编译报告：{args.output}")
    if int(summary["pac_outputs"]) < args.minimum_pac_outputs or int(summary["failed"]) != 0:
        raise ValueError("PAC 类型检查覆盖未通过门限")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
