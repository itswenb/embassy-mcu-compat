import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "merge_gigadevice_firmware_variants.py"
SPEC = importlib.util.spec_from_file_location("merge_gigadevice_firmware_variants", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class MergeFirmwareVariantsTests(unittest.TestCase):
    def test_Builder新增型号优先Builder其余优先独立Firmware(self):
        models = {
            "devices": [
                {"id": "GD32F103C8", "source": "cmsis-pack"},
                {"id": "GD32H77DIX", "source": "embedded-builder"},
                {"id": "GD32A711AR", "source": "selection-guide"},
            ]
        }
        official = {"variants": [{"id": "f10", "devices": ["GD32F103C8"]}]}
        builder = {
            "variants": [
                {"id": "f10b", "devices": ["GD32F103C8"]},
                {"id": "h77", "devices": ["GD32H77DIX"]},
            ]
        }

        report = MODULE.merge_reports(models, official, builder, {"devices": []})

        selected = {device: variant["source_kind"] for variant in report["variants"] for device in variant["devices"]}
        self.assertEqual(selected, {"GD32F103C8": "official", "GD32H77DIX": "builder"})
        self.assertEqual(report["missing_devices"], [{"device": "GD32A711AR", "reason": "firmware-source-missing"}])

    def test_变体中的CMSIS完整订货号归一回device(self):
        models = {
            "devices": [
                {
                    "id": "GD32W515PI",
                    "source": "cmsis-pack",
                    "cmsis_devices": ["GD32W515PIQ6"],
                }
            ]
        }
        official = {"variants": [{"id": "w51", "devices": ["GD32W515PIQ6"]}]}

        report = MODULE.merge_reports(models, official, {"variants": []}, {"devices": []})

        self.assertEqual(report["variants"][0]["devices"], ["GD32W515PI"])
        self.assertEqual(report["missing_devices"], [])

    def test_CMSIS_Pack同头文件SVD和宏唯一推断新型号变体(self):
        models = {
            "devices": [
                {"id": "GD32H759IM", "source": "cmsis-pack"},
                {"id": "GD32H767IM", "source": "cmsis-pack"},
            ]
        }
        official = {
            "variants": [
                {
                    "id": "h7-i",
                    "devices": ["GD32H759IM"],
                    "defines": ["USE_STDPERIPH_DRIVER", "GD32H7XX", "GD32H7XXI"],
                }
            ]
        }
        signature = {
            "compile": [
                {
                    "define": "USE_STDPERIPH_DRIVER GD32H7XX GD32H7XXI",
                    "file": {"sha256": "header"},
                }
            ],
            "debug": [{"file": {"sha256": "svd"}}],
        }
        resources = {
            "devices": [
                {"device": "GD32H759IM", **signature},
                {"device": "GD32H767IM", **signature},
            ]
        }

        report = MODULE.merge_reports(models, official, {"variants": []}, resources)

        self.assertEqual(report["missing_devices"], [])
        self.assertEqual(report["summary"]["inferred_devices"], 1)
        self.assertEqual(report["variants"][0]["devices"], ["GD32H759IM", "GD32H767IM"])
        self.assertEqual(report["variants"][0]["inferred_devices"], ["GD32H767IM"])

    def test_CMSIS_Pack同时命中两个来源时沿用独立Firmware优先级(self):
        models = {
            "devices": [
                {"id": "GD32H759IM", "source": "cmsis-pack"},
                {"id": "GD32H767IM", "source": "cmsis-pack"},
            ]
        }
        defines = ["USE_STDPERIPH_DRIVER", "GD32H7XX", "GD32H7XXI"]
        official = {
            "variants": [
                {"id": "official-h7-i", "devices": ["GD32H759IM"], "defines": defines}
            ]
        }
        builder = {
            "variants": [
                {"id": "builder-h7-i", "devices": ["GD32H759IM"], "defines": defines}
            ]
        }
        signature = {
            "compile": [
                {
                    "define": "USE_STDPERIPH_DRIVER GD32H7XX GD32H7XXI",
                    "file": {"sha256": "header"},
                }
            ],
            "debug": [{"file": {"sha256": "svd"}}],
        }
        resources = {
            "devices": [
                {"device": "GD32H759IM", **signature},
                {"device": "GD32H767IM", **signature},
            ]
        }

        report = MODULE.merge_reports(models, official, builder, resources)

        self.assertEqual(report["missing_devices"], [])
        self.assertEqual(report["variants"][0]["source_kind"], "official")
        self.assertEqual(report["variants"][0]["inferred_devices"], ["GD32H767IM"])


if __name__ == "__main__":
    unittest.main()
