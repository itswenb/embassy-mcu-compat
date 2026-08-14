import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "compare_gigadevice_svd_headers.py"
SPEC = importlib.util.spec_from_file_location(
    "compare_gigadevice_svd_headers", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class SvdHeaderComparisonTests(unittest.TestCase):
    def test_同名基址和中断值一致时没有冲突(self):
        svd = {
            "peripheral_base_addresses": {"USART0": 0x40004400},
            "interrupts": [{"name": "USART0", "value": 37}],
        }
        header = {
            "base_addresses": {"USART0_BASE": 0x40004400},
            "interrupts": [
                {"name": "NonMaskableInt", "value": -14},
                {"name": "USART0", "value": 37},
            ],
        }

        result = MODULE.compare_facts(svd, header)

        self.assertEqual(result["missing_svd_interrupt_values"], [])
        self.assertEqual(result["named_base_conflicts"], [])
        self.assertEqual(result["named_base_matches"], ["USART0"])

    def test_报告缺失中断值和同名基址冲突(self):
        svd = {
            "peripheral_base_addresses": {"USART0": 0x40004400},
            "interrupts": [{"name": "USART0", "value": 37}],
        }
        header = {
            "base_addresses": {"USART0_BASE": 0x50004400},
            "interrupts": [{"name": "USART0", "value": 38}],
        }

        result = MODULE.compare_facts(svd, header)

        self.assertEqual(result["missing_svd_interrupt_values"], [37])
        self.assertEqual(
            result["missing_svd_interrupts"], [{"name": "USART0", "value": 37}]
        )
        self.assertEqual(
            result["named_base_conflicts"],
            [{"name": "USART0", "svd": 0x40004400, "header": 0x50004400}],
        )

    def test_寄存器头文件实例地址参与交叉校验(self):
        svd = {
            "peripheral_base_addresses": {
                "SPI0": 0x40013000,
                "USART0": 0x40004400,
            },
            "interrupts": [],
        }
        header = {"base_addresses": {}, "interrupts": []}

        result = MODULE.compare_facts(
            svd,
            header,
            {
                "I2C0": [0x40005400],
                "SPI0": [0x50013000],
                "USART0": [0x40004400, 0x50004400],
            },
        )

        self.assertEqual(result["named_instance_matches"], ["USART0"])
        self.assertEqual(
            result["named_instance_conflicts"],
            [{"name": "SPI0", "svd": 0x40013000, "firmware": 0x50013000}],
        )
        self.assertEqual(result["shared_instance_address_values"], 1)

    def test_同系列重复寄存器头文件实例按名称和值去重(self):
        self.assertEqual(
            MODULE._register_instances(
                {
                    "register_headers": [
                        {"instances": {"SPI0": 0x40013000, "USART0": 0x40004400}},
                        {"instances": {"USART0": 0x40004400, "USART1": 0x40004800}},
                    ]
                }
            ),
            {
                "SPI0": [0x40013000],
                "USART0": [0x40004400],
                "USART1": [0x40004800],
            },
        )

    def test_仅H7xx使用已审计的固件系列别名(self):
        self.assertEqual(MODULE.firmware_series_for_pack("GD32A10x_DFP"), "GD32A10x")
        self.assertEqual(
            MODULE.firmware_series_for_pack("GD32H7xx_DFP"), "GD32H73x_75x"
        )

    def test_已知冲突必须同时锁定两侧哈希和差异(self):
        comparison = {
            "svd_sha256": "a" * 64,
            "firmware_header_sha256": "b" * 64,
            "missing_svd_interrupts": [{"name": "ADC2", "value": 47}],
            "interrupt_name_conflicts": [],
            "named_base_conflicts": [],
        }
        expected = {
            **comparison,
            "resolution": "block",
            "reason": "官方 SVD 与 Firmware 头文件冲突",
        }

        self.assertEqual(MODULE.classify_conflict(comparison, expected), "known-blocking")
        self.assertEqual(
            MODULE.classify_conflict(
                comparison,
                {**expected, "resolution": "prefer-pack-svd"},
            ),
            "source-resolved",
        )
        self.assertEqual(
            MODULE.classify_conflict(
                {**comparison, "firmware_header_sha256": "c" * 64}, expected
            ),
            "unexpected",
        )


if __name__ == "__main__":
    unittest.main()
