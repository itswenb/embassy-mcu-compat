import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "audit_gigadevice_model_universe.py"
SPEC = importlib.util.spec_from_file_location("audit_gigadevice_model_universe", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ModelUniverseTests(unittest.TestCase):
    def test_五层来源全部闭包到规范device(self):
        report = MODULE.build_report(
            models={
                "devices": [
                    {"id": "GD32F103C8", "cmsis_devices": ["GD32F103C8"]},
                    {"id": "GD32H77DIX", "cmsis_devices": []},
                    {"id": "GD32A711AR", "cmsis_devices": []},
                ],
                "catalog_entries": [],
            },
            pack={"devices": [{"device": "GD32F103C8"}]},
            builder_models={
                "summary": {"builder_xml_files": 1},
                "devices": [
                    {"id": "GD32F103C8", "evidence": "none"},
                    {"id": "GD32H77DIX", "evidence": "builder-model"},
                    {"id": "GD32A711AR", "evidence": "none"},
                ],
                "unmatched_matrices": [],
            },
            firmware_registers={"libraries": [{"series": "GD32F10x"}]},
            builder_firmware={
                "plugins": [{"id": "h77", "series": "gd32h77x_78x"}]
            },
            iar={"devices": [{"id": "GD32A711AR", "svd": "GD32A71x.svd"}]},
        )

        self.assertEqual(report["summary"]["unaccounted_devices"], 0)
        self.assertEqual(report["summary"]["orphan_builder_plugins"], 0)
        self.assertEqual(report["devices"][2]["register_sources"], ["builder-firmware:h77"])
        self.assertEqual(report["devices"][0]["register_sources"], ["iar-svd:GD32A71x.svd"])


if __name__ == "__main__":
    unittest.main()
