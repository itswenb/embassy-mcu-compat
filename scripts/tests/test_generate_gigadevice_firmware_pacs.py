import importlib.util
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "generate_gigadevice_firmware_pacs.py"
SPEC = importlib.util.spec_from_file_location(
    "generate_gigadevice_firmware_pacs", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FirmwarePacTests(unittest.TestCase):
    def test_条件Firmware_IR转换为确定性SVD(self):
        variant = {
            "id": "gd32test-a",
            "series": "GD32TEST",
            "devices": ["GD32TESTA", "GD32TESTB"],
            "layouts": [
                {
                    "id": "rcu-test",
                    "block": "RCU",
                    "registers": [
                        {
                            "name": "RCU_CTL",
                            "offset": 0,
                            "width": 32,
                            "parameters": [],
                        },
                        {
                            "name": "RCU_DATA10_41",
                            "offset": 0x40,
                            "width": 32,
                            "parameters": ["number"],
                            "array_parameters": {
                                "number": {"start": 10, "end": 41, "stride": 4}
                            },
                        },
                    ],
                    "fields": [
                        {
                            "name": "RCU_CTL_IRC8MEN",
                            "register": "RCU_CTL",
                            "bit_offset": 0,
                            "bit_size": 1,
                        }
                    ],
                }
            ],
            "instances": [
                {
                    "name": "RCU",
                    "address": 0x40021000,
                    "layout": "rcu-test",
                }
            ],
            "interrupts": [{"name": "RCU_CTC", "value": 5}],
        }

        first = MODULE.variant_svd_bytes(variant)
        second = MODULE.variant_svd_bytes(variant)
        root = ET.fromstring(first)

        self.assertEqual(first, second)
        self.assertEqual(root.findtext("name"), "GD32TESTA")
        self.assertEqual(root.findtext("./peripherals/peripheral/name"), "RCU")
        self.assertEqual(
            root.findtext("./peripherals/peripheral/baseAddress"), "0x40021000"
        )
        self.assertEqual(
            root.findtext("./peripherals/peripheral/registers/register/name"),
            "RCU_CTL",
        )
        self.assertEqual(
            root.findtext(
                "./peripherals/peripheral/registers/register/fields/field/name"
            ),
            "RCU_CTL_IRC8MEN",
        )
        self.assertEqual(
            root.findtext("./peripherals/peripheral/interrupt/name"), "RCU_CTC"
        )
        array_register = root.findall(
            "./peripherals/peripheral/registers/register"
        )[1]
        self.assertEqual(array_register.findtext("dim"), "32")
        self.assertEqual(array_register.findtext("dimIncrement"), "0x4")
        self.assertEqual(array_register.findtext("dimIndex"), "10-41")
        self.assertEqual(
            MODULE.register_parameter_stats(variant),
            {
                "base_parameter_registers": 0,
                "array_registers": 1,
                "bounded_array_registers": 1,
                "unbounded_array_registers": 0,
            },
        )
        self.assertEqual(MODULE.unbounded_array_parameters(variant), [])

    def test_实例引用未知布局时拒绝生成(self):
        with self.assertRaisesRegex(ValueError, "未知布局"):
            MODULE.variant_svd_bytes(
                {
                    "id": "broken",
                    "devices": ["GD32BROKEN"],
                    "layouts": [],
                    "instances": [
                        {"name": "RCU", "address": 0x40021000, "layout": "missing"}
                    ],
                    "interrupts": [],
                }
            )

    def test_未闭合数组输出可审计明细(self):
        variant = {
            "layouts": [
                {
                    "id": "dma-test",
                    "block": "DMA",
                    "registers": [
                        {
                            "name": "DMA_CHCTL",
                            "array_parameters": {
                                "channel": {"start": 0, "stride": 20}
                            },
                        }
                    ],
                }
            ]
        }

        self.assertEqual(
            MODULE.unbounded_array_parameters(variant),
            [
                {
                    "layout": "dma-test",
                    "block": "DMA",
                    "register": "DMA_CHCTL",
                    "parameter": "channel",
                }
            ],
        )

    def test_稀疏数组展开为保持真实偏移的标量寄存器(self):
        variant = {
            "id": "gd32test-sparse",
            "series": "GD32TEST",
            "devices": ["GD32TEST"],
            "layouts": [
                {
                    "id": "syscfg-test",
                    "block": "SYSCFG",
                    "registers": [
                        {
                            "name": "SYSCFG_TIMERCFG0",
                            "offset": 0x100,
                            "width": 32,
                            "array_parameters": {
                                "syscfg_timerx": {
                                    "start": 0,
                                    "indices": [0, 1, 2, 11],
                                    "stride": 12,
                                }
                            },
                        }
                    ],
                    "fields": [],
                }
            ],
            "instances": [
                {"name": "SYSCFG", "address": 0x40010000, "layout": "syscfg-test"}
            ],
            "interrupts": [],
        }

        root = ET.fromstring(MODULE.variant_svd_bytes(variant))
        registers = root.findall("./peripherals/peripheral/registers/register")

        self.assertEqual(
            [(row.findtext("name"), row.findtext("addressOffset")) for row in registers],
            [
                ("SYSCFG_TIMERCFG0_0", "0x100"),
                ("SYSCFG_TIMERCFG0_1", "0x10C"),
                ("SYSCFG_TIMERCFG0_2", "0x118"),
                ("SYSCFG_TIMERCFG0_11", "0x184"),
            ],
        )
        self.assertEqual(MODULE.register_parameter_stats(variant)["unbounded_array_registers"], 0)

    def test_二维数组展开为确定偏移的标量寄存器(self):
        variant = {
            "id": "gd32test-matrix",
            "series": "GD32TEST",
            "devices": ["GD32TEST"],
            "layouts": [
                {
                    "id": "edim-test",
                    "block": "EDIM_AFMT",
                    "registers": [
                        {
                            "name": "EDIM_AFMT_ENCRDATA",
                            "offset": 0,
                            "width": 32,
                            "array_parameters": {
                                "m": {"start": 0, "end": 2, "stride": 4},
                                "n": {"start": 0, "end": 1, "stride": 16},
                            },
                        }
                    ],
                    "fields": [],
                }
            ],
            "instances": [
                {"name": "EDIM_AFMT", "address": 0x40000000, "layout": "edim-test"}
            ],
            "interrupts": [],
        }

        root = ET.fromstring(MODULE.variant_svd_bytes(variant))
        registers = root.findall("./peripherals/peripheral/registers/register")

        self.assertEqual(
            [(row.findtext("name"), row.findtext("addressOffset")) for row in registers],
            [
                ("EDIM_AFMT_ENCRDATA_0_0", "0x0"),
                ("EDIM_AFMT_ENCRDATA_0_1", "0x10"),
                ("EDIM_AFMT_ENCRDATA_1_0", "0x4"),
                ("EDIM_AFMT_ENCRDATA_1_1", "0x14"),
                ("EDIM_AFMT_ENCRDATA_2_0", "0x8"),
                ("EDIM_AFMT_ENCRDATA_2_1", "0x18"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
