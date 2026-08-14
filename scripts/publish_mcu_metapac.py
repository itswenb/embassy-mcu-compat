#!/usr/bin/env python3
"""把兼容 patch 与全量原生 PAC 合并为可发布的生成仓库。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import gigadevice_sources as common


IGNORED = shutil.ignore_patterns(".git", "target", "Cargo.lock")
MINIMUM_GD32_CHIPS = 680

NATIVE_README = """# mcu-metapac

这是由 `embassy-mcu-compat` 确定性生成的厂商无关 MCU 外设访问包。

当前版本包含 {chips} 个原生 MCU feature，GigaDevice 是第一个接入厂商。每个 feature
使用真实厂商寄存器、中断和内存事实生成；支持状态与来源缺口以源仓库中的机器可读报告为准。

本包提供 PAC 与 metadata，不代表所有型号已经通过 `embassy-stm32` 兼容门或硬件验证。
"""

ROOT_NOTICE = """## 原生 GD32 PAC

本仓库还发布 workspace 包 [`mcu-metapac`](mcu-metapac/README.md)，当前包含 {chips}
个由真实厂商数据生成并通过编译门的 GD32 feature。依赖时选择一个真实型号：

```toml
[dependencies]
mcu-metapac = {{ git = "https://github.com/itswenb/embassy-mcu-compat-generated", rev = "<固定提交>", features = ["gd32f103c8", "pac", "metadata"] }}
```

这条原生 PAC 路径不等于所有型号已经通过 `embassy-stm32` 或实机验证。
"""


def _replace_package_name(manifest: Path) -> None:
    text = manifest.read_text(encoding="utf-8")
    source = 'name = "stm32-metapac"'
    if text.count(source) != 1:
        raise ValueError("原生 metapac 包名不符合固定生成格式")
    text = text.replace(source, 'name = "mcu-metapac"')
    text = text.replace(
        'repository = "https://github.com/embassy-rs/stm32-data"',
        'repository = "https://github.com/itswenb/embassy-mcu-compat-generated"',
    ).replace(
        'description = "Peripheral Access Crate (PAC) for all STM32 chips, including metadata."',
        'description = "厂商无关 MCU 的原生 PAC 与 metadata。"',
    ).replace(
        'features = ["stm32h755zi-cm7", "pac", "metadata"]',
        'features = ["gd32f103c8", "pac", "metadata"]',
    ).replace(
        'default-target = "thumbv7em-none-eabihf"',
        'default-target = "thumbv7m-none-eabi"',
    )
    manifest.write_text(text, encoding="utf-8")


def _add_workspace(manifest: Path) -> None:
    text = manifest.read_text(encoding="utf-8")
    if "[workspace]" in text:
        raise ValueError("兼容 patch 已包含 workspace，拒绝模糊合并")
    manifest.write_text(
        text.rstrip() + '\n\n[workspace]\nmembers = ["mcu-metapac"]\n',
        encoding="utf-8",
    )


def build_publication(
    patch: Path,
    native: Path,
    output: Path,
    *,
    compile_report: Path,
    replace: bool = False,
) -> dict[str, object]:
    patch_marker = patch / "generation.json"
    native_marker = native / ".m32-metapac-generation.json"
    if not patch_marker.is_file() or not native_marker.is_file():
        raise ValueError("输入缺少生成标记")
    if output.exists() and not replace:
        raise ValueError(f"发布输出已存在：{output}")
    if output.exists() and not all(
        (output / marker).is_file()
        for marker in ("generation.json", "mcu-metapac-generation.json")
    ):
        raise ValueError(f"旧输出缺少完整发布标记，拒绝替换：{output}")
    native_generation = json.loads(native_marker.read_text(encoding="utf-8"))
    chips = int(native_generation["chips"])
    manifest_data = tomllib.loads((native / "Cargo.toml").read_text(encoding="utf-8"))
    feature_count = sum(
        name.startswith("gd32") for name in manifest_data.get("features", {})
    )
    if feature_count != chips:
        raise ValueError(
            f"GD32 feature 数量与生成标记不一致：{feature_count} != {chips}"
        )
    if chips < MINIMUM_GD32_CHIPS:
        raise ValueError(
            f"GD32 feature 少于全量基线：{chips} < {MINIMUM_GD32_CHIPS}"
        )
    compile_data = json.loads(compile_report.read_text(encoding="utf-8"))
    summary = compile_data.get("summary", {})
    if (
        summary.get("devices") != chips
        or summary.get("features_validated") != chips
        or summary.get("features_compiled_for_exact_target") != chips
        or summary.get("features_missing_exact_target") != 0
        or summary.get("failed") != 0
    ):
        raise ValueError("全量精确目标编译门禁未通过")
    provenance = compile_data.get("provenance", {})
    expected_provenance = {
        "cargo_toml_sha256": common._sha256(native / "Cargo.toml"),
        "generation_marker_sha256": common._sha256(native_marker),
    }
    if any(provenance.get(key) != value for key, value in expected_provenance.items()):
        raise ValueError("编译门禁报告与待发布生成树不匹配")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mcu-metapac-", dir=output.parent) as directory:
        temporary = Path(directory) / "publication"
        shutil.copytree(patch, temporary, ignore=IGNORED)
        native_output = temporary / "mcu-metapac"
        shutil.copytree(native, native_output, ignore=IGNORED)
        _add_workspace(temporary / "Cargo.toml")
        _replace_package_name(native_output / "Cargo.toml")
        root_readme = temporary / "README.md"
        root_readme.write_text(
            root_readme.read_text(encoding="utf-8").rstrip()
            + "\n\n"
            + ROOT_NOTICE.format(chips=chips),
            encoding="utf-8",
        )
        (native_output / "README.md").write_text(
            NATIVE_README.format(chips=chips), encoding="utf-8"
        )
        release = temporary / "release"
        release.mkdir()
        shutil.copy2(compile_report, release / "gigadevice-metapac-compile.json")
        report = {
            "schema_version": 1,
            "native_chips": chips,
            "patch_tree_sha256": common.tree_sha256(patch),
            "native_tree_sha256": common.tree_sha256(native),
            "compile_report_sha256": common._sha256(compile_report),
        }
        common._write_text_atomic(
            temporary / "mcu-metapac-generation.json",
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        if output.exists():
            shutil.rmtree(output)
        temporary.rename(output)
    return report


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument(
        "--compile-report",
        type=Path,
        default=root / "reports/gigadevice-metapac-compile.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--replace", action="store_true", help="替换具备完整发布标记的旧输出"
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=root / "reports/mcu-metapac-publication.json",
    )
    args = parser.parse_args()
    report = build_publication(
        args.patch,
        args.native,
        args.output,
        compile_report=args.compile_report,
        replace=args.replace,
    )
    common._write_text_atomic(
        args.report,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(f"native_chips={report['native_chips']} output={args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
