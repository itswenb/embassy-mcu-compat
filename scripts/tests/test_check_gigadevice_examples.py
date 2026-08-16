import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "check_gigadevice_examples.py"
SPEC = importlib.util.spec_from_file_location("check_gigadevice_examples", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class GigadeviceExampleCheckTests(unittest.TestCase):
    def test_example型号直接决定编译feature与真实目标(self):
        projection = {
            "chip": "gd32f303cg",
            "profile": "stm32f103rf",
            "rust_target": "thumbv7em-none-eabihf",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "Cargo.toml"
            manifest.write_text(
                '[features]\ndefault = []\ngd32f303cg = ["embassy-stm32/stm32f103rf", "embassy-stm32/memory-x"]\n',
                encoding="utf-8",
            )
            examples = MODULE.load_examples(manifest, [projection])
            spec = MODULE.compile_spec(
                manifest, root / "publication", projection, root / "target", True
            )

        self.assertEqual(examples, [projection])
        self.assertIn("gd32f303cg", spec["command"])
        self.assertIn("thumbv7em-none-eabihf", spec["command"])
        self.assertEqual(
            spec["environment"]["EMBASSY_MCU_COMPAT_CHIP"], "gd32f303cg"
        )


if __name__ == "__main__":
    unittest.main()
