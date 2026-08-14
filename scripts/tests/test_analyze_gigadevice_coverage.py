import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "analyze_gigadevice_coverage.py"
SPEC = importlib.util.spec_from_file_location("analyze_gigadevice_coverage", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class CoverageTests(unittest.TestCase):
    def test_选型标记映射到Pack设备或聚合系列(self):
        result = MODULE.classify_catalog_tokens(
            {"GD32F103C8", "GD32F103C8T6", "GD32F103", "GD32F10x", "GD32VW553K8U6"},
            {"GD32F103C8", "GD32F103CB"},
        )

        self.assertEqual(result["exact"], ["GD32F103C8"])
        self.assertEqual(result["order_code"][0]["device"], "GD32F103C8")
        self.assertEqual(result["aggregate"], ["GD32F103", "GD32F10x"])
        self.assertEqual(result["unmatched"], ["GD32VW553K8U6"])

    def test_识别逐文件宽松许可证(self):
        self.assertEqual(
            MODULE.source_license("SPDX-License-Identifier: Apache-2.0"), "Apache-2.0"
        )
        self.assertEqual(
            MODULE.source_license(
                "Redistribution and use in source and binary forms, with or without modification. "
                "Neither the name of the copyright holder"
            ),
            "BSD-3-Clause",
        )
        self.assertEqual(MODULE.source_license("Copyright GigaDevice"), "未识别")


if __name__ == "__main__":
    unittest.main()
