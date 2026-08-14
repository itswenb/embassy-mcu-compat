import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "check_gigadevice_metapac.py"
SPEC = importlib.util.spec_from_file_location("check_gigadevice_metapac", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class MetapacCheckTests(unittest.TestCase):
    def test_仅复用与当前输入和工具链完全一致的成功报告(self):
        summary = {"devices": 3, "failed": 0}
        provenance = {"cargo_toml_sha256": "a" * 64}
        report = {
            "rustc": "release=1.94.0-nightly",
            "summary": dict(summary),
            "provenance": dict(provenance),
        }

        self.assertTrue(
            MODULE.report_is_reusable(
                report, summary, provenance, "release=1.94.0-nightly"
            )
        )
        report["summary"]["failed"] = 1
        self.assertFalse(
            MODULE.report_is_reusable(
                report, summary, provenance, "release=1.94.0-nightly"
            )
        )

    def test_逐feature编译禁用无界增量缓存(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            capture = root / "incremental.txt"
            cargo = bin_dir / "cargo"
            cargo.write_text(
                '#!/bin/sh\nprintf "%s" "${CARGO_INCREMENTAL-unset}" > "$CAPTURE_PATH"\n',
                encoding="utf-8",
            )
            cargo.chmod(0o755)
            environment = {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "CAPTURE_PATH": str(capture),
            }

            with patch.dict(os.environ, environment):
                MODULE._check_exact_features(
                    root,
                    root / "target",
                    [("GD32TEST", "thumbv7m-none-eabi")],
                    False,
                )

            self.assertEqual(capture.read_text(encoding="utf-8"), "0")

    def test_型号按精确Rust目标分为可编译与缺来源(self):
        models = {
            "devices": [
                {
                    "id": "GD32VF103C8",
                    "rust_target": "riscv32imac-unknown-none-elf",
                },
                {
                    "id": "GD32VW553HI",
                    "rust_target": "riscv32imafc-unknown-none-elf",
                },
                {
                    "id": "GD32C103CBxxA",
                    "rust_target": "thumbv7em-none-eabihf",
                },
                {"id": "GD32UNKNOWN", "rust_target": None},
            ]
        }

        self.assertEqual(
            MODULE.exact_targets(
                models,
                [
                    "gd32vf103c8",
                    "gd32vw553hi",
                    "gd32c103cbxxa",
                    "gd32unknown",
                ],
            ),
            (
                [
                    ("GD32VF103C8", "riscv32imac-unknown-none-elf"),
                    ("GD32VW553HI", "riscv32imafc-unknown-none-elf"),
                    ("GD32C103CBXXA", "thumbv7em-none-eabihf"),
                ],
                ["GD32UNKNOWN"],
            ),
        )

    def test_RISCV_PAC拒绝ARM专用中断实现(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = root / "src/chips/gd32riscv"
            chip.mkdir(parents=True)
            (chip / "pac.rs").write_text(
                "unsafe impl cortex_m::interrupt::InterruptNumber for Interrupt {}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "ARM 专用内容"):
                MODULE.validate_riscv_pacs(root, ["GD32RISCV"])

    def test_全部metadata参与检查且相同PAC只编译一次(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/chips/gd32testa").mkdir(parents=True)
            (root / "src/chips/gd32testb").mkdir(parents=True)
            (root / "src/chips/gd32testc").mkdir(parents=True)
            (root / "src/metadata.rs").write_text(
                "pub struct Metadata { pub name: &'static str }\n"
            )
            (root / "src/chips/metadata_0001.rs").write_text("")
            for chip in ("gd32testa", "gd32testb"):
                chip_dir = root / "src/chips" / chip
                (chip_dir / "pac.rs").write_text("pub const VALUE: u32 = 1;\n")
                (chip_dir / "metadata.rs").write_text(
                    'include!("../metadata_0001.rs");\n'
                    f'pub static METADATA: Metadata = Metadata {{ name: "{chip.upper()}" }};\n'
                )
            standalone = root / "src/chips/gd32testc"
            (standalone / "pac.rs").write_text("pub const VALUE: u32 = 2;\n")
            (standalone / "metadata.rs").write_text(
                '// name: "GPIOA"\n'
                'pub static METADATA: Metadata = Metadata { name: "GD32TESTC" };\n',
                encoding="utf-8",
            )

            devices, pac_sources, metadata_sources = MODULE.validation_sources(root)

            self.assertEqual(devices, ["gd32testa", "gd32testb", "gd32testc"])
            self.assertEqual(len(pac_sources), 2)
            self.assertEqual(len(metadata_sources), 2)
            self.assertEqual(pac_sources[0].count("mod pac;"), 1)
            self.assertEqual(metadata_sources[0].count("mod chip_metadata"), 1)


if __name__ == "__main__":
    unittest.main()
