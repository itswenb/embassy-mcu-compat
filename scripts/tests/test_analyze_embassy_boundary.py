import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "analyze_embassy_boundary.py"
SPEC = importlib.util.spec_from_file_location("analyze_embassy_boundary", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BoundaryTests(unittest.TestCase):
    def test_统计架构与STM32家族绑定(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/lib.rs").write_text(
                '#[cfg(stm32f1)]\nfn f() { cortex_m::asm::nop(); }\n', encoding="utf-8"
            )
            (root / "build.rs").write_text(
                'if chip_name.starts_with("stm32h7") {}\n', encoding="utf-8"
            )
            (root / "Cargo.toml").write_text('cortex-m = "0.7"\n', encoding="utf-8")

            result = MODULE.scan_embassy_stm32(root)

        self.assertEqual(result["stm32_cfg_files"], 1)
        self.assertEqual(result["cortex_m_source_files"], 1)
        self.assertEqual(result["stm32_prefix_branches"], 1)
        self.assertTrue(result["unconditional_cortex_m_dependency"])


if __name__ == "__main__":
    unittest.main()
