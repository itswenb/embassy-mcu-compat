import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "index_gigadevice_builder_firmware.py"
SPEC = importlib.util.spec_from_file_location("index_gigadevice_builder_firmware", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuilderFirmwareIndexTests(unittest.TestCase):
    def test_索引插件器件头和选择器(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins/com.gigadevice.templatefwlib.arm.gd32h77x_78x_1.0.0.1"
            include = plugin / "Firmware/Firmware/CMSIS/GD/GD32H77x_78x/Include"
            include.mkdir(parents=True)
            (include / "gd32h77x_78x.h").write_text(
                "#if defined(GD32H77EXW) || defined(GD32H78RXP)\n#endif\n"
                "#define __FPU_PRESENT 1U\n",
                encoding="utf-8",
            )
            cmsis = plugin / "Firmware/Firmware/CMSIS"
            (cmsis / "core_cm7.h").write_text("", encoding="utf-8")
            (include / "gd32h77x_78x_err_report.h").write_text(
                "#define GD32H77_ERROR 1\n", encoding="utf-8"
            )
            resources = root / "resources"
            (resources / "CodeGenerate/GD32H77x").mkdir(parents=True)
            (resources / "CodeTemplate/GD32H77x").mkdir(parents=True)

            report = MODULE.build_report(root)

        self.assertEqual(report["summary"]["plugins"], 1)
        self.assertEqual(report["summary"]["device_headers"], 1)
        self.assertEqual(report["plugins"][0]["series"], "gd32h77x_78x")
        self.assertEqual(report["plugins"][0]["core"], "Cortex-M7")
        self.assertEqual(report["plugins"][0]["rust_target"], "thumbv7em-none-eabihf")
        self.assertEqual(
            report["plugins"][0]["model_selectors"],
            ["GD32H77EXW", "GD32H78RXP"],
        )
        self.assertEqual(report["code_generate_families"], ["GD32H77x"])

    def test_索引Riscv扁平器件头(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins/com.gigadevice.templatefwlib.riscv.gd32vf103_1.0.0.1"
            firmware = plugin / "Firmware/Firmware/GD32VF103_standard_peripheral"
            firmware.mkdir(parents=True)
            (firmware / "gd32vf103.h").write_text(
                "#ifdef GD32VF103\n#endif\n", encoding="utf-8"
            )
            (root / "resources/CodeGenerate").mkdir(parents=True)
            (root / "resources/CodeTemplate").mkdir(parents=True)

            report = MODULE.build_report(root)

        self.assertEqual(report["summary"]["device_headers"], 1)
        self.assertEqual(report["plugins"][0]["model_selectors"], ["GD32VF103"])


if __name__ == "__main__":
    unittest.main()
