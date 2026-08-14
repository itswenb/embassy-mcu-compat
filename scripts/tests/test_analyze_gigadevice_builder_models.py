import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "analyze_gigadevice_builder_models.py"
SPEC = importlib.util.spec_from_file_location("analyze_gigadevice_builder_models", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuilderModelTests(unittest.TestCase):
    def test_旧系列文件名补齐产品线并匹配device(self):
        pattern = MODULE.builder_pattern(
            Path("GD32F10x/103xxDatasheet.xml")
        )

        self.assertTrue(MODULE.pattern_matches(pattern, "GD32F103C8", part_number=False))
        self.assertFalse(MODULE.pattern_matches(pattern, "GD32F105C8", part_number=False))

    def test_具体与通配文件名匹配完整订货号(self):
        exact = MODULE.builder_pattern(
            Path("GD32F30x/GD32F303/F303CBTDatasheet.xml")
        )
        wildcard = MODULE.builder_pattern(
            Path("GD32C211/C211ExP6TRDatasheet.xml")
        )

        self.assertTrue(MODULE.pattern_matches(exact, "GD32F303CBT6", part_number=True))
        self.assertFalse(MODULE.pattern_matches(exact, "GD32F303CBU6", part_number=True))
        self.assertTrue(MODULE.pattern_matches(wildcard, "GD32C211E4P6TR", part_number=True))

    def test_device层可合并同一裸片前缀的封装专用矩阵(self):
        c = MODULE.builder_pattern(Path("GD32C2x1/GD32C221/C221CxT6Datasheet.xml"))
        f = MODULE.builder_pattern(Path("GD32C2x1/GD32C221/C221FxP6TRDatasheet.xml"))

        self.assertTrue(MODULE.pattern_matches_device_prefix(c, "GD32C221C8"))
        self.assertFalse(MODULE.pattern_matches_device_prefix(f, "GD32C221C8"))

    def test_通用家族矩阵覆盖家族内型号(self):
        pattern = MODULE.builder_pattern(
            Path("GD32H75E/GD32H75E/H75EDatasheet.xml")
        )

        self.assertTrue(MODULE.pattern_matches(pattern, "GD32H75EYMJ6", part_number=True))

    def test_只记录实际出现的封装与引脚数(self):
        xml = """<PinPadMatrix>
          <PinPad Name="PA0"><Package_LQFP48 Number="1"/><Package_QFN32 Number="-"/></PinPad>
          <PinPad Name="PB0"><Package_LQFP48 Number="2"/></PinPad>
        </PinPadMatrix>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.xml"
            path.write_text(xml, encoding="utf-8")
            packages, pins = MODULE.xml_packages(path)

        self.assertEqual(packages, {"LQFP48": 2})
        self.assertEqual(pins, 2)

    def test_不能用另一订货号的专用矩阵冒充通用device矩阵(self):
        models = {
            "devices": [{"id": "GD32F303CB"}],
            "catalog_entries": [
                {"id": "GD32F303CBT6", "kind": "part_number", "device": "GD32F303CB"},
                {"id": "GD32F303CBU6", "kind": "part_number", "device": "GD32F303CB"},
            ],
        }
        matrices = [
            {
                "path": "F303CBT.xml",
                "_pattern": MODULE.builder_pattern(
                    Path("GD32F30x/GD32F303/F303CBTDatasheet.xml")
                ),
            }
        ]

        report = MODULE.build_report(models, matrices)
        parts = {part["id"]: part for part in report["part_numbers"]}

        self.assertEqual(parts["GD32F303CBT6"]["evidence"], "part-pattern")
        self.assertEqual(parts["GD32F303CBU6"]["evidence"], "none")


if __name__ == "__main__":
    unittest.main()
