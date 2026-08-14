import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "extract_gigadevice_manual_dma.py"
SPEC = importlib.util.spec_from_file_location(
    "extract_gigadevice_manual_dma", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ManualDmaTests(unittest.TestCase):
    def test_解析固定通道表并保留重映射脚注(self):
        text = """Table 9-3. DMA0 requests for each channel
Peripheral    Channel 0       Channel 1       Channel 2
 TIMER0          ●          TIMER0_CH0      TIMER0_CH1
                            TIMER0_UP
 USART        USART0_RX       USART0_TX             ●
 ADC           ADC(1)          ADC(2)               ●
Figure 9-5. DMA1 request mapping
"""

        tables, issues = MODULE.parse_main_tables(text, "GD32TEST User Manual")

        self.assertEqual(issues, [])
        self.assertEqual(tables[0]["controller"], "DMA0")
        self.assertEqual(tables[0]["kind"], "fixed")
        self.assertEqual(
            [
                (route["channel"], route["signal"], route.get("footnote"))
                for route in tables[0]["routes"]
            ],
            [
                (0, "ADC", 1),
                (0, "USART0_RX", None),
                (1, "ADC", 2),
                (1, "TIMER0_CH0", None),
                (1, "TIMER0_UP", None),
                (1, "USART0_TX", None),
                (2, "TIMER0_CH1", None),
            ],
        )

    def test_解析通道内请求选择表与换行信号(self):
        text = """Table 14-2. Peripheral requests to DMA0 (Only for GD32F5HCxx)
Channel              Channel 0       Channel 1       Channel 2
              000      SPI2_RX              ●          SPI2_RX
              001    TIMER4_CH              I2C0_RX             ●
                         2
 PERIEN[2:0]
              010          ●               ●             ●
              011          ●               ●             ●
              100          ●               ●             ●
              101          ●               ●             ●
              110          ●               ●             ●
              111          ●               ●             ●
10.4.2. Data process
"""

        tables, issues = MODULE.parse_main_tables(text, "GD32F5xx User Manual")

        self.assertEqual(issues, [])
        self.assertEqual(tables[0]["kind"], "selected")
        self.assertEqual(tables[0]["applies_to"], ["GD32F5HCxx"])
        self.assertEqual(
            [
                (route["channel"], route["request"], route["signal"])
                for route in tables[0]["routes"]
            ],
            [
                (0, 0, "SPI2_RX"),
                (0, 1, "TIMER4_CH2"),
                (1, 1, "I2C0_RX"),
                (2, 0, "SPI2_RX"),
            ],
        )

    def test_手册通配符家族覆盖具体型号(self):
        self.assertTrue(MODULE.family_matches("GD32F5xx", "GD32F527ZM"))
        self.assertTrue(MODULE.family_matches("GD32W51x", "GD32W515PIQ6"))
        self.assertFalse(MODULE.family_matches("GD32F50x", "GD32F527ZM"))

    def test_按pdf坐标重建选择码上下的换行信号(self):
        def word(text, left, top, width=12, page=10):
            return {
                "text": text,
                "left": float(left),
                "top": float(top),
                "width": float(width),
                "height": 8.0,
                "page": page,
            }

        words = [
            word("Table", 100, 20),
            word("1-2.", 115, 20),
            word("Peripheral", 140, 20),
            word("requests", 180, 20),
            word("to", 220, 20),
            word("DMA0", 240, 20),
            word("Channel", 80, 40, 30),
            word("Channel", 180, 40, 30),
            word("0", 212, 40, 5),
            word("Channel", 260, 40, 30),
            word("1", 292, 40, 5),
            word("100", 120, 100, 15),
            word("USART0_", 184, 90, 40),
            word("RX", 198, 110, 12),
            word("USART0_", 264, 90, 40),
            word("TX", 278, 110, 12),
            word("101", 120, 125, 15),
            word("●", 198, 125, 5),
            word("●", 278, 125, 5),
        ]
        table = {
            "number": "1-2",
            "title": "Table 1-2. Peripheral requests to DMA0",
            "kind": "selected",
            "controller": "DMA0",
            "channels": [0, 1],
            "page_start": 10,
            "page_end": 10,
            "end_marker": None,
        }

        routes, issues = MODULE.parse_tsv_table_routes(words, table)

        self.assertEqual(issues, [])
        self.assertEqual(
            [
                (route["channel"], route["request"], route["signal"])
                for route in routes
            ],
            [(0, 4, "USART0_RX"), (1, 4, "USART0_TX")],
        )

    def test_按页发现中英文dma映射表(self):
        with tempfile.TemporaryDirectory() as directory:
            text_dir = Path(directory)
            english = text_dir / "f10.txt"
            chinese = text_dir / "e23.txt"
            english.write_text(
                "9.4.9 DMA request mapping\n\f"
                "Table 9-3. DMA0 requests for each channel\n",
                encoding="utf-8",
            )
            chinese.write_text(
                "10.4.8 DMA 请求映射\n\f表 10-3 DMA0 各通道的请求\n",
                encoding="utf-8",
            )
            report = {
                "schema_version": 1,
                "manuals": [
                    {
                        "name": "GD32F10x User Manual",
                        "text_cache": english.name,
                        "text_sha256": MODULE.common._sha256(english),
                    },
                    {
                        "name": "GD32E23x用户手册",
                        "text_cache": chinese.name,
                        "text_sha256": MODULE.common._sha256(chinese),
                    },
                ],
            }

            inventory = MODULE.build_inventory(report, text_dir)

        self.assertEqual(inventory["summary"]["manuals"], 2)
        self.assertEqual(inventory["summary"]["manuals_with_dma_mapping"], 2)
        self.assertEqual(inventory["summary"]["table_candidates"], 2)
        self.assertEqual(
            inventory["manuals"][0]["table_candidates"][0]["page"], 2
        )
        self.assertEqual(
            inventory["manuals"][1]["table_candidates"][0]["text"],
            "表 10-3 DMA0 各通道的请求",
        )

    def test_拒绝文本哈希不匹配(self):
        with tempfile.TemporaryDirectory() as directory:
            text_dir = Path(directory)
            source = text_dir / "manual.txt"
            source.write_text("DMA request mapping\n", encoding="utf-8")
            report = {
                "schema_version": 1,
                "manuals": [
                    {
                        "name": "GD32",
                        "text_cache": source.name,
                        "text_sha256": "0" * 64,
                    }
                ],
            }

            with self.assertRaisesRegex(ValueError, "哈希"):
                MODULE.build_inventory(report, text_dir)


if __name__ == "__main__":
    unittest.main()
