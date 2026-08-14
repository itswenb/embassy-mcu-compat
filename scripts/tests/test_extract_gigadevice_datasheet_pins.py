import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "extract_gigadevice_datasheet_pins.py"
SPEC = importlib.util.spec_from_file_location(
    "extract_gigadevice_datasheet_pins", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class DatasheetPinTests(unittest.TestCase):
    def test_识别管脚定义和复用功能表(self):
        text = """Table 2-8. GD32F103Cx LQFP48 pin definitions
Number  Pin name  Type  Default  Alternate  Additional
1       VBAT      S     VBAT
Table 2-10. Alternate function mapping
Pin Name   AF0   AF1   AF2   AF3
PA0        WKUP  TIMER1_CH0 USART1_CTS
\fTable 2-11. Alternate function mapping (continued)
Pin Name   AF0   AF1   AF2   AF3
PB0        -     TIMER2_CH2 -
"""

        candidates = MODULE.find_candidates(text)

        self.assertEqual(
            [(row["kind"], row["page"]) for row in candidates],
            [
                ("pin-definitions", 1),
                ("alternate-functions", 1),
                ("alternate-functions", 2),
            ],
        )
        self.assertEqual(candidates[0]["device_pattern"], "GD32F103Cx")
        self.assertEqual(candidates[0]["package"], "LQFP48")

    def test_目录中的目录项和修订记录不算实际表(self):
        text = """Table 2-8. GD32F103Cx LQFP48 pin definitions ........ 42
2.6.4. GD32F103Cx LQFP48 pin definitions ........ 42
1. Add missing pin definitions for GD32F103Cx.
"""

        self.assertEqual(MODULE.find_candidates(text), [])

    def test_列出未识别版式的管脚相关行(self):
        text = "Table 3-1. Module pin assignment\nConnector pin description\fNo pins here\n"

        self.assertEqual(
            MODULE.find_pin_mentions(text),
            [
                {"page": 1, "line": 1, "text": "Table 3-1. Module pin assignment"},
                {"page": 1, "line": 2, "text": "Connector pin description"},
            ],
        )

    def test_识别空格表号脚注和无线模块通用管脚表(self):
        text = """Table 2 3. GD32H75Exx BGA240 pin definitions(7)
Pin Name Pins Functions description
\fTable 4-1. Pin definitions
Pin No. Pin Name Function Description
Table 4-2. Pin Definitions of default Usage
"""

        candidates = MODULE.find_candidates(text)

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["table"], "2-3")
        self.assertEqual(candidates[0]["package"], "BGA240")
        self.assertIsNone(candidates[1]["device_pattern"])
        self.assertIsNone(candidates[1]["package"])

    def test_解析普通芯片和模块管脚行及功能分类(self):
        standard = """Table 2-8. GD32F103Cx LQFP48 pin definitions
 Pin Name Pins Type Level Functions description
                                          Default: PB6
    PB6        58      I/O      5VT       Alternate: I2C0_SCL, TIMER3_CH0(6)
                                          Remap: USART0_TX
    PB7        59      I/O      5VT       Default: PB7
"""
        module = """Table 4-1. Pin definitions
 NO. Name Type Function Description
  2  PA0  I/O Default: PA0
             Alternate: USART0_TX, TIMER1_CH0
             Additional: ADC_IN0, WAKEUP0
"""

        standard_pins = MODULE.parse_pin_rows(standard, "name-first")
        module_pins = MODULE.parse_pin_rows(module, "position-first")

        self.assertEqual(standard_pins[0]["name"], "PB6")
        self.assertEqual(standard_pins[0]["position"], "58")
        self.assertEqual(
            standard_pins[0]["functions"],
            [
                {"source": "alternate", "name": "I2C0_SCL"},
                {"source": "alternate", "name": "TIMER3_CH0", "footnote": 6},
                {"source": "default", "name": "PB6"},
                {"source": "remap", "name": "USART0_TX"},
            ],
        )
        self.assertEqual(module_pins[0]["name"], "PA0")
        self.assertEqual(module_pins[0]["position"], "2")
        self.assertEqual(len(module_pins[0]["functions"]), 5)

    def test_按下一张表边界解析全部管脚表(self):
        text = """Table 2-1. GD32F103Cx LQFP48 pin definitions
PA0 10 I/O Default: PA0
           Alternate: USART0_TX
Notes:
PA0 99 I/O Default: PA0
\fTable 2-2. GD32F103Tx QFN36 pin definitions
PB0 20 I/O Default: PB0
Table 2-2. GD32F103Tx QFN36 pin definitions (continued)
PB1 21 I/O Default: PB1
Table 2-3. Alternate function mapping
Pin Name AF0 AF1
"""
        candidates = MODULE.find_candidates(text)

        tables = MODULE.parse_pin_tables(text, candidates)

        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0]["pins"][0]["name"], "PA0")
        self.assertEqual(len(tables[0]["pins"]), 1)
        self.assertEqual(tables[0]["page_end"], 1)
        self.assertEqual(tables[1]["pins"][0]["name"], "PB0")
        self.assertEqual(tables[1]["pins"][1]["name"], "PB1")
        self.assertEqual(tables[1]["page_end"], 2)


if __name__ == "__main__":
    unittest.main()
