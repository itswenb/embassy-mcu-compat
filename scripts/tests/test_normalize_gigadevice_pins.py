import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "normalize_gigadevice_pins.py"
SPEC = importlib.util.spec_from_file_location("normalize_gigadevice_pins", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class NormalizePinTests(unittest.TestCase):
    def _inputs(self, datasheet_position="10", builder_positions=None):
        builder_positions = builder_positions or ["10"]
        models = {"devices": [{"id": "GD32A103CB"}, {"id": "GD32A711AZ"}]}
        builder = {
            "matrices": [
                {
                    "path": "A103.xml",
                    "sha256": "a" * 64,
                    "pins": [
                        {
                            "name": "PA0",
                            "types": ["I/O"],
                            "packages": {"LQFP48": builder_positions},
                            "functions": [
                                {"source": "alternate", "af": 1, "name": "USART0_TX"}
                            ],
                        }
                    ],
                }
            ],
            "devices": [
                {"id": "GD32A103CB", "status": "normalized", "matrix_paths": ["A103.xml"], "afio_paths": []},
                {"id": "GD32A711AZ", "status": "missing", "matrix_paths": [], "afio_paths": []},
            ],
        }
        datasheets = {
            "datasheets": [
                {
                    "name": "GD32A103xx Datasheet",
                    "pdf": {"sha256": "b" * 64},
                    "pin_tables": [
                        {
                            "device_pattern": "GD32A103Cx",
                            "package": "LQFP48",
                            "table": "2-1",
                            "page": 10,
                            "page_end": 11,
                            "pins": [
                                {
                                    "name": "PA0",
                                    "position": datasheet_position,
                                    "type": "I/O",
                                    "functions": [
                                        {"source": "alternate", "name": "USART0_TX"},
                                        {"source": "default", "name": "PA0"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        return models, builder, datasheets

    def test_通配模式匹配具体密度并合并两个来源(self):
        full, report = MODULE.build_outputs(*self._inputs())

        device = full["devices"][0]
        self.assertEqual(device["id"], "GD32A103CB")
        self.assertEqual(device["status"], "normalized")
        self.assertEqual(device["pins"][0]["packages"], {"LQFP48": ["10"]})
        self.assertEqual(report["summary"]["devices_with_normalized_pins"], 1)
        self.assertEqual(report["summary"]["devices_without_pin_source"], 1)
        self.assertEqual(report["summary"]["devices_with_pin_conflict"], 0)

    def test_同封装同引脚位置不一致时拒绝宣布归一(self):
        full, report = MODULE.build_outputs(*self._inputs(datasheet_position="11"))

        self.assertEqual(full["devices"][0]["status"], "conflict")
        self.assertEqual(report["summary"]["devices_with_pin_conflict"], 1)

    def test_当前数据手册覆盖Builder中的旧多余位置(self):
        full, report = MODULE.build_outputs(
            *self._inputs(builder_positions=["10", "12"])
        )

        device = full["devices"][0]
        self.assertEqual(device["status"], "normalized")
        self.assertEqual(device["pins"][0]["packages"], {"LQFP48": ["10"]})
        self.assertEqual(len(device["position_resolutions"]), 1)
        self.assertEqual(report["summary"]["resolved_position_differences"], 1)

    def test_AFIO路由在无封装矩阵时仍生成引脚功能(self):
        models = {"devices": [{"id": "GD32H779II"}]}
        builder = {
            "matrices": [],
            "devices": [
                {
                    "id": "GD32H779II",
                    "matrix_paths": [],
                    "afio_paths": ["GD32H77x/GD32H779.xml"],
                    "routes": [
                        {
                            "function": "USART0_TX",
                            "group": "USART0_TX",
                            "pin": "PA9",
                            "remap": "AF7",
                            "value": 7,
                        }
                    ],
                }
            ],
        }

        full, _ = MODULE.build_outputs(models, builder, {"datasheets": []})

        self.assertEqual(full["devices"][0]["status"], "normalized")
        self.assertEqual(
            full["devices"][0]["pins"],
            [
                {
                    "name": "PA9",
                    "types": [],
                    "packages": {},
                    "functions": [
                        {
                            "source": "afio",
                            "af": 7,
                            "name": "USART0_TX",
                            "group": "USART0_TX",
                            "remap": "AF7",
                        }
                    ],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
