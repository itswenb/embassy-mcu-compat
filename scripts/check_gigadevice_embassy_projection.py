#!/usr/bin/env python3
"""使用真实型号与规范 profile 编译全部 GD32 Embassy 投影。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import gigadevice_sources as common


EMBASSY_VERSION = "0.6.0"
EMBASSY_STM32_DATA_REVISION = "c05b9691035f8978090af617e20869b7302b069b"
EMBASSY_STM32_DATA_GENERATED_REVISION = "0ff67d4ac661d8817efbb196ab089e70ea7fb01d"
REGRESSION_CHIP = "gd32f303cb"
REGRESSION_FEATURES = (
    "time-driver-tim5",
    "exti",
    "rt",
    "unstable-pac",
    "unchecked-overclocking",
)


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def validation_cargo_toml(
    projections: list[dict[str, object]], publication: Path, version: str
) -> str:
    profiles = sorted({str(row["profile"]) for row in projections})
    features = [
        *[
            f'{feature} = ["embassy-stm32/{feature}"]'
            for feature in REGRESSION_FEATURES
        ],
        *[
            f'{profile} = ["embassy-stm32/{profile}"]'
            for profile in profiles
        ],
    ]
    return (
        "[package]\n"
        'name = "gigadevice-embassy-projection-check"\n'
        'version = "0.0.0"\n'
        'edition = "2024"\n'
        "publish = false\n\n"
        "[dependencies]\n"
        f'embassy-stm32 = {{ version = "={version}" }}\n\n'
        "[features]\n"
        "default = []\n"
        + "\n".join(features)
        + "\n\n[patch.crates-io]\n"
        f"stm32-metapac = {{ path = {_toml_string(publication.resolve())} }}\n"
    )


def validate_requested_profile(
    projection: dict[str, object], requested_profile: str
) -> None:
    expected = str(projection["profile"])
    if requested_profile != expected:
        raise ValueError(
            f"{projection['chip']} 必须使用 profile {expected}，不能使用 {requested_profile}"
        )


def validate_embassy_baseline(generation: dict[str, object]) -> None:
    upstream = generation.get("upstream")
    expected = {
        "stm32_data": EMBASSY_STM32_DATA_REVISION,
        "stm32_data_generated": EMBASSY_STM32_DATA_GENERATED_REVISION,
    }
    if not isinstance(upstream, dict) or any(
        upstream.get(key) != value for key, value in expected.items()
    ):
        raise ValueError(
            f"发布投影不是 embassy-stm32 {EMBASSY_VERSION} 的精确 STM32 数据基线"
        )


def compile_spec(
    projection: dict[str, object],
    work: Path,
    target_dir: Path,
    offline: bool,
    extra_features: tuple[str, ...] = (),
) -> dict[str, object]:
    profile = str(projection["profile"])
    validate_requested_profile(projection, profile)
    command = ["cargo", "check", "--quiet"]
    if offline:
        command.append("--offline")
    command.extend(
        [
            "--manifest-path",
            str(work / "Cargo.toml"),
            "--target",
            str(projection["rust_target"]),
            "--no-default-features",
            "--features",
            ",".join((profile, *extra_features)),
        ]
    )
    environment = os.environ.copy()
    environment["EMBASSY_MCU_COMPAT_CHIP"] = str(projection["chip"])
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    environment["CARGO_INCREMENTAL"] = "0"
    return {"command": command, "environment": environment}


def _load_projections(publication: Path) -> list[dict[str, object]]:
    generation = publication / "generation.json"
    if not generation.is_file():
        raise ValueError(f"发布目录缺少 generation.json：{publication}")
    document = json.loads(generation.read_text(encoding="utf-8"))
    validate_embassy_baseline(document)
    rows = document.get("chips")
    if not isinstance(rows, list) or not rows:
        raise ValueError("发布清单缺少兼容投影")
    required = {"chip", "profile", "rust_target", "projection_sha256"}
    projections = []
    for row in rows:
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError("发布清单包含无效投影")
        projection = {key: str(row[key]).lower() for key in required}
        chip = projection["chip"]
        if not (publication / "src/chips" / chip).is_dir():
            raise ValueError(f"发布目录缺少投影芯片：{chip}")
        projections.append(projection)
    projections.sort(key=lambda row: str(row["chip"]))
    chips = [str(row["chip"]) for row in projections]
    if len(chips) != len(set(chips)):
        raise ValueError("发布清单包含重复真实型号")
    compat = (publication / "src/compat.rs").read_text(encoding="utf-8")
    pairs = re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', compat)
    expected = [(str(row["chip"]), str(row["profile"])) for row in projections]
    if pairs != expected:
        raise ValueError("COMPATIBLE_CHIPS 与发布投影清单不一致")
    return projections


def _write_validation_crate(
    work: Path, publication: Path, projections: list[dict[str, object]]
) -> None:
    cargo_toml = validation_cargo_toml(projections, publication, EMBASSY_VERSION)
    if (
        (work / "generation.json").is_file()
        and (work / "Cargo.toml").is_file()
        and (work / "Cargo.toml").read_text(encoding="utf-8") == cargo_toml
    ):
        return
    work.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".embassy-check-", dir=work.parent) as directory:
        temporary = Path(directory)
        (temporary / "src").mkdir()
        (temporary / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")
        (temporary / "src/lib.rs").write_text("#![no_std]\n", encoding="utf-8")
        (temporary / "generation.json").write_text(
            '{"schema_version":1}\n', encoding="utf-8"
        )
        if work.exists():
            if not (work / "generation.json").is_file():
                raise ValueError(f"拒绝覆盖非本脚本生成目录：{work}")
            shutil.rmtree(work)
        temporary.rename(work)


def _tool_version(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _provenance(publication: Path) -> dict[str, str]:
    return {
        "generation_sha256": common._sha256(publication / "generation.json"),
        "cargo_toml_sha256": common._sha256(publication / "Cargo.toml"),
        "compat_sha256": common._sha256(publication / "src/compat.rs"),
        "checker_sha256": common._sha256(Path(__file__)),
    }


def _report_is_reusable(
    report: dict[str, object],
    projections: list[dict[str, object]],
    provenance: dict[str, str],
    rustc: str,
    cargo: str,
) -> bool:
    summary = report.get("summary")
    return (
        report.get("rustc") == rustc
        and report.get("cargo") == cargo
        and report.get("provenance") == provenance
        and isinstance(summary, dict)
        and summary.get("projections") == len(projections)
        and summary.get("compiled") == len(projections)
        and summary.get("failed") == 0
    )


def _args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publication",
        type=Path,
        default=root / ".cache/generated/mcu-metapac-publication-v1",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=root / ".cache/generated/gigadevice-embassy-check-v1",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=root / ".cache/tools/gigadevice-embassy-check-target",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "reports/gigadevice-embassy-compile.json",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    projections = _load_projections(args.publication)
    regression = next(
        (row for row in projections if row["chip"] == REGRESSION_CHIP), None
    )
    if regression is None:
        raise ValueError(f"发布投影缺少回归型号：{REGRESSION_CHIP}")
    ordered = [regression, *[row for row in projections if row is not regression]]
    _write_validation_crate(args.work_dir, args.publication, projections)
    rustc = _tool_version(["rustc", "-Vv"])
    cargo = _tool_version(["cargo", "-V"])
    provenance = _provenance(args.publication)
    if args.output.is_file() and not args.force:
        try:
            existing = json.loads(args.output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if isinstance(existing, dict) and _report_is_reusable(
            existing, projections, provenance, rustc, cargo
        ):
            print(f"复用已验证的 {len(projections)} 个 Embassy 投影：{args.output}")
            return 0

    checks = []
    for index, projection in enumerate(ordered, 1):
        extra = REGRESSION_FEATURES if projection is regression else ()
        spec = compile_spec(
            projection, args.work_dir, args.target_dir, args.offline, extra
        )
        result = subprocess.run(
            spec["command"],
            env=spec["environment"],
            capture_output=True,
            text=True,
        )
        check = {
            "chip": projection["chip"],
            "profile": projection["profile"],
            "rust_target": projection["rust_target"],
            "features": [projection["profile"], *extra],
            "status": "passed" if result.returncode == 0 else "failed",
        }
        if result.returncode != 0:
            check["error"] = (
                result.stderr
                if len(result.stderr) <= 8000
                else result.stderr[:4000] + "\n...\n" + result.stderr[-4000:]
            )
        checks.append(check)
        if index % 10 == 0 or index == len(ordered) or result.returncode != 0:
            print(
                f"已编译 {index}/{len(ordered)}：{projection['chip']} "
                f"{check['status']}",
                flush=True,
            )
        if result.returncode != 0:
            break

    failed = [check for check in checks if check["status"] == "failed"]
    report = {
        "schema_version": 1,
        "embassy_stm32": EMBASSY_VERSION,
        "rustc": rustc,
        "cargo": cargo,
        "provenance": provenance,
        "summary": {
            "projections": len(projections),
            "compiled": len(checks) - len(failed),
            "failed": len(failed),
            "real_project_regressions": 1,
        },
        "checks": checks,
    }
    common._write_text_atomic(
        args.output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if failed:
        names = ", ".join(str(check["chip"]) for check in failed[:10])
        raise ValueError(f"Embassy 投影编译失败 {len(failed)} 个：{names}")
    print(f"projections={len(projections)} compiled={len(checks)} failed=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
