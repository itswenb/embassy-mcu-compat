import importlib.util
import os
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "check_gigadevice_embassy_projection.py"
SPEC = importlib.util.spec_from_file_location(
    "check_gigadevice_embassy_projection", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class EmbassyProjectionCheckTests(unittest.TestCase):
    def setUp(self):
        self.projection = {
            "chip": "gd32f303cb",
            "profile": "stm32f103rf",
            "rust_target": "thumbv7em-none-eabihf",
            "projection_sha256": "a" * 64,
        }

    def test_GD32F303CB使用真实型号规范profile和TIM5(self):
        publication = Path("/tmp/publication")
        work = Path("/tmp/work")
        target = Path("/tmp/target")

        cargo_toml = MODULE.validation_cargo_toml(
            [self.projection], publication, "0.6.0"
        )
        spec = MODULE.compile_spec(
            self.projection,
            work,
            target,
            offline=True,
            extra_features=("time-driver-tim5",),
        )

        self.assertIn('embassy-stm32 = { version = "=0.6.0"', cargo_toml)
        self.assertIn('[patch.crates-io]', cargo_toml)
        self.assertIn(
            f'stm32-metapac = {{ path = "{publication.resolve()}" }}', cargo_toml
        )
        self.assertIn(
            'stm32f103rf = ["embassy-stm32/stm32f103rf"]', cargo_toml
        )
        self.assertEqual(spec["environment"]["EMBASSY_MCU_COMPAT_CHIP"], "gd32f303cb")
        self.assertEqual(spec["environment"]["CARGO_TARGET_DIR"], str(target))
        self.assertEqual(
            spec["command"],
            [
                "cargo",
                "check",
                "--quiet",
                "--offline",
                "--manifest-path",
                str(work / "Cargo.toml"),
                "--target",
                "thumbv7em-none-eabihf",
                "--no-default-features",
                "--features",
                "stm32f103rf,time-driver-tim5",
            ],
        )

    def test_错误profile在Cargo执行前被拒绝(self):
        with self.assertRaisesRegex(ValueError, "stm32f103rf"):
            MODULE.validate_requested_profile(self.projection, "stm32f427vg")

    def test_错误STM32数据基线在Cargo执行前被拒绝(self):
        generation = {
            "upstream": {
                "stm32_data": "87c539515764df442bc50b6235bad891950ba3c4",
                "stm32_data_generated": "12ec4cd38c7825c1ff8592de1bdefaae445bb3a6",
            }
        }

        with self.assertRaisesRegex(ValueError, "embassy-stm32 0.6.0"):
            MODULE.validate_embassy_baseline(generation)

    def test_编译环境禁用无界增量缓存(self):
        spec = MODULE.compile_spec(
            self.projection,
            Path("/tmp/work"),
            Path("/tmp/target"),
            offline=False,
        )

        self.assertEqual(spec["environment"]["CARGO_INCREMENTAL"], "0")
        self.assertEqual(
            spec["environment"]["PATH"], os.environ.get("PATH", "")
        )


if __name__ == "__main__":
    unittest.main()
