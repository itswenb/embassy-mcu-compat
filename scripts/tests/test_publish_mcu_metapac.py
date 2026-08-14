import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "publish_mcu_metapac.py"
SPEC = importlib.util.spec_from_file_location("publish_mcu_metapac", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def native_manifest(chips: int) -> str:
    features = "\n".join(f"gd32test{index:03d} = []" for index in range(chips))
    return (
        '[package]\nname = "stm32-metapac"\nversion = "1.0.0"\n'
        f"\n[features]\n{features}\n"
    )


def write_compile_report(native: Path, path: Path, *, failed: int = 0) -> None:
    path.write_text(
        json.dumps(
            {
                "provenance": {
                    "cargo_toml_sha256": MODULE.common._sha256(native / "Cargo.toml"),
                    "generation_marker_sha256": MODULE.common._sha256(
                        native / ".m32-metapac-generation.json"
                    ),
                },
                "summary": {
                    "devices": 680,
                    "failed": failed,
                    "features_compiled_for_exact_target": 680,
                    "features_missing_exact_target": 0,
                    "features_validated": 680,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


class PublishMcuMetapacTests(unittest.TestCase):
    def test_兼容patch与原生PAC合并为同一生成仓库(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = root / "patch"
            native = root / "native"
            output = root / "output"
            (patch / "src").mkdir(parents=True)
            (native / "src").mkdir(parents=True)
            (patch / "Cargo.toml").write_text(
                '[package]\nname = "stm32-metapac"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            (patch / "generation.json").write_text("{}\n", encoding="utf-8")
            (patch / "README.md").write_text("# stm32-metapac\n", encoding="utf-8")
            (patch / "src/lib.rs").write_text("#![no_std]\n", encoding="utf-8")
            (native / "Cargo.toml").write_text(native_manifest(680), encoding="utf-8")
            (native / ".m32-metapac-generation.json").write_text(
                '{"chips":680}\n', encoding="utf-8"
            )
            (native / "src/lib.rs").write_text("#![no_std]\n", encoding="utf-8")
            (native / "target").mkdir()
            (native / "target/ignored").write_text("x", encoding="utf-8")
            compile_report = root / "compile.json"
            write_compile_report(native, compile_report)

            report = MODULE.build_publication(
                patch, native, output, compile_report=compile_report
            )

            root_manifest = (output / "Cargo.toml").read_text(encoding="utf-8")
            native_manifest_text = (output / "mcu-metapac/Cargo.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn('members = ["mcu-metapac"]', root_manifest)
            self.assertIn(
                "mcu-metapac",
                (output / "README.md").read_text(encoding="utf-8"),
            )
            self.assertIn('name = "mcu-metapac"', native_manifest_text)
            self.assertFalse((output / "mcu-metapac/target").exists())
            self.assertIn(
                "680 个原生 MCU",
                (output / "mcu-metapac/README.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(report["native_chips"], 680)
            self.assertTrue((output / "release/gigadevice-metapac-compile.json").is_file())

    def test_缺少生成标记时拒绝发布(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = root / "patch"
            native = root / "native"
            patch.mkdir()
            native.mkdir()

            with self.assertRaisesRegex(ValueError, "生成标记"):
                MODULE.build_publication(
                    patch,
                    native,
                    root / "output",
                    compile_report=root / "compile.json",
                )

    def test_只替换带完整发布标记的旧输出(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = root / "patch"
            native = root / "native"
            output = root / "output"
            patch.mkdir()
            native.mkdir()
            output.mkdir()
            (patch / "Cargo.toml").write_text(
                '[package]\nname = "stm32-metapac"\n', encoding="utf-8"
            )
            (patch / "generation.json").write_text("{}\n", encoding="utf-8")
            (patch / "README.md").write_text("# stm32-metapac\n", encoding="utf-8")
            (native / "Cargo.toml").write_text(native_manifest(680), encoding="utf-8")
            (native / ".m32-metapac-generation.json").write_text(
                '{"chips":680}\n', encoding="utf-8"
            )
            compile_report = root / "compile.json"
            write_compile_report(native, compile_report)

            with self.assertRaisesRegex(ValueError, "完整发布标记"):
                MODULE.build_publication(
                    patch,
                    native,
                    output,
                    compile_report=compile_report,
                    replace=True,
                )

            (output / "generation.json").write_text("{}\n", encoding="utf-8")
            (output / "mcu-metapac-generation.json").write_text(
                "{}\n", encoding="utf-8"
            )
            MODULE.build_publication(
                patch,
                native,
                output,
                compile_report=compile_report,
                replace=True,
            )

            self.assertTrue((output / "mcu-metapac/README.md").is_file())

    def test_拒绝发布少于680个或清单不一致的GD32(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = root / "patch"
            native = root / "native"
            patch.mkdir()
            native.mkdir()
            (patch / "Cargo.toml").write_text(
                '[package]\nname = "stm32-metapac"\n', encoding="utf-8"
            )
            (patch / "generation.json").write_text("{}\n", encoding="utf-8")
            (patch / "README.md").write_text("# stm32-metapac\n", encoding="utf-8")
            (native / "Cargo.toml").write_text(native_manifest(679), encoding="utf-8")
            (native / ".m32-metapac-generation.json").write_text(
                '{"chips":680}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "feature 数量"):
                MODULE.build_publication(
                    patch,
                    native,
                    root / "output",
                    compile_report=root / "compile.json",
                )

    def test_拒绝未通过或不匹配生成树的编译报告(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = root / "patch"
            native = root / "native"
            patch.mkdir()
            native.mkdir()
            (patch / "Cargo.toml").write_text(
                '[package]\nname = "stm32-metapac"\n', encoding="utf-8"
            )
            (patch / "generation.json").write_text("{}\n", encoding="utf-8")
            (patch / "README.md").write_text("# stm32-metapac\n", encoding="utf-8")
            (native / "Cargo.toml").write_text(native_manifest(680), encoding="utf-8")
            (native / ".m32-metapac-generation.json").write_text(
                '{"chips":680}\n', encoding="utf-8"
            )
            compile_report = root / "compile.json"
            write_compile_report(native, compile_report, failed=1)

            with self.assertRaisesRegex(ValueError, "编译门禁"):
                MODULE.build_publication(
                    patch, native, root / "output", compile_report=compile_report
                )


if __name__ == "__main__":
    unittest.main()
