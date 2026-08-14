import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "normalize_gigadevice_embassy_names.py"
SPEC = importlib.util.spec_from_file_location(
    "normalize_gigadevice_embassy_names", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class EmbassyNamesTests(unittest.TestCase):
    def test_编号和语义名称转换为Embassy命名(self):
        expected = {
            "TIMER0": ("TIM1", "indexed"),
            "USART0": ("USART1", "indexed"),
            "UART3": ("UART4", "indexed"),
            "DMA0": ("DMA1", "indexed"),
            "DMA": ("DMA1", "semantic"),
            "ADC": ("ADC1", "semantic"),
            "DAC": ("DAC1", "semantic"),
            "CAN0": ("CAN1", "indexed"),
            "DAC0": ("DAC1", "indexed"),
            "I2C0": ("I2C1", "indexed"),
            "RCU": ("RCC", "semantic"),
            "FMC": ("FLASH", "semantic"),
            "EXMC": ("FMC", "semantic"),
            "DMAMUX": ("DMAMUX1", "semantic"),
            "GPIOA": ("GPIOA", "identity"),
        }

        for native, result in expected.items():
            with self.subTest(native=native):
                self.assertEqual(MODULE.embassy_instance_name(native), result)

        self.assertIsNone(MODULE.embassy_instance_name("EDIM_AFMT"))
        for family_specific in ("OSPI0", "SDIO0"):
            with self.subTest(family_specific=family_specific):
                self.assertIsNone(MODULE.embassy_instance_name(family_specific))

    def test_变体内映射冲突会阻止发布(self):
        variants = {
            "variants": [
                {
                    "id": "test",
                    "devices": ["GD32TEST"],
                    "instances": [
                        {"name": "FMC", "address": 0x40022000},
                        {"name": "FLASH", "address": 0x40023000},
                    ],
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "Embassy 外设名称冲突"):
            MODULE.build_report(variants)


if __name__ == "__main__":
    unittest.main()
