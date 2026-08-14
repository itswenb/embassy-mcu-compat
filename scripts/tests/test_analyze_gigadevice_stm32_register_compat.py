import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "analyze_gigadevice_stm32_register_compat.py"
SPEC = importlib.util.spec_from_file_location(
    "analyze_gigadevice_stm32_register_compat", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RegisterCompatTests(unittest.TestCase):
    def test_寄存器与位域位置相同才视为结构兼容(self):
        layout = {
            "id": "rcu-test",
            "block": "RCU",
            "registers": [
                {"name": "RCU_CTL", "offset": 0, "width": 32},
                {"name": "RCU_CFG", "offset": 4, "width": 32},
            ],
            "fields": [
                {
                    "name": "RCU_CTL_IRC8MEN",
                    "register": "RCU_CTL",
                    "bit_offset": 0,
                    "bit_size": 1,
                },
                {
                    "name": "RCU_CFG_SCS",
                    "register": "RCU_CFG",
                    "bit_offset": 0,
                    "bit_size": 2,
                },
            ],
        }
        compatible = {
            "block/RCC": {
                "items": [
                    {"name": "CR", "byte_offset": 0, "fieldset": "CR"},
                    {"name": "CFGR", "byte_offset": 4, "fieldset": "CFGR"},
                ]
            },
            "fieldset/CR": {
                "fields": [{"name": "HSION", "bit_offset": 0, "bit_size": 1}]
            },
            "fieldset/CFGR": {
                "fields": [{"name": "SW", "bit_offset": 0, "bit_size": 2}]
            },
        }
        incompatible = {
            **compatible,
            "fieldset/CFGR": {
                "fields": [{"name": "SW", "bit_offset": 1, "bit_size": 2}]
            },
        }

        signature = MODULE.gd_layout_signature(layout)

        self.assertEqual(signature, MODULE.st_block_signature(compatible, "RCC"))
        self.assertNotEqual(signature, MODULE.st_block_signature(incompatible, "RCC"))

    def test_稠密数组按实际地址展开后比较(self):
        layout = {
            "id": "dma-test",
            "block": "DMA",
            "registers": [
                {
                    "name": "DMA_CHCTL",
                    "offset": 8,
                    "width": 32,
                    "array_parameters": {
                        "channel": {"start": 0, "end": 2, "stride": 20}
                    },
                }
            ],
            "fields": [],
        }
        registers = {
            "block/DMA": {
                "items": [
                    {
                        "name": "CCR",
                        "array": {"len": 3, "stride": 20},
                        "byte_offset": 8,
                    }
                ]
            }
        }

        self.assertEqual(
            MODULE.gd_layout_signature(layout),
            MODULE.st_block_signature(registers, "DMA"),
        )

    def test_STM32子块数组递归展开后比较(self):
        registers = {
            "block/DMA": {
                "items": [
                    {
                        "name": "CH",
                        "array": {"len": 2, "stride": 20},
                        "byte_offset": 8,
                        "block": "CH",
                    }
                ]
            },
            "block/CH": {
                "items": [
                    {"name": "CR", "byte_offset": 0, "fieldset": "CR"},
                    {"name": "COUNT", "byte_offset": 4},
                ]
            },
            "fieldset/CR": {
                "fields": [{"name": "EN", "bit_offset": 0, "bit_size": 1}]
            },
        }

        self.assertEqual(
            MODULE.st_block_signature(registers, "DMA"),
            (
                (8, 32, (((0, 0),),)),
                (12, 32, ()),
                (28, 32, (((0, 0),),)),
                (32, 32, ()),
            ),
        )

    def test_STM32子块继承保留父块寄存器(self):
        registers = {
            "block/TIM_BASE": {
                "items": [
                    {"name": "CR", "byte_offset": 0, "fieldset": "CR"}
                ]
            },
            "block/TIM_GP": {
                "extends": "TIM_BASE",
                "items": [{"name": "COUNT", "byte_offset": 4}],
            },
            "fieldset/CR": {
                "fields": [{"name": "EN", "bit_offset": 0, "bit_size": 1}]
            },
        }

        self.assertEqual(
            MODULE.st_block_signature(registers, "TIM_GP"),
            ((0, 32, (((0, 0),),)), (4, 32, ())),
        )

    def test_STM32继承按名称覆盖寄存器并合并位域(self):
        registers = {
            "block/TIM_BASE": {
                "items": [
                    {"name": "COUNT", "byte_offset": 0, "bit_size": 16},
                    {"name": "CTL", "byte_offset": 4, "fieldset": "CTL_BASE"},
                ]
            },
            "block/TIM_GP": {
                "extends": "TIM_BASE",
                "items": [
                    {"name": "COUNT", "byte_offset": 0},
                    {"name": "CTL", "byte_offset": 4, "fieldset": "CTL_GP"},
                ],
            },
            "fieldset/CTL_BASE": {
                "fields": [{"name": "EN", "bit_offset": 0, "bit_size": 1}]
            },
            "fieldset/CTL_GP": {
                "extends": "CTL_BASE",
                "fields": [{"name": "MODE", "bit_offset": 4, "bit_size": 2}],
            },
        }

        self.assertEqual(
            MODULE.st_block_signature(registers, "TIM_GP"),
            (
                (0, 32, ()),
                (4, 32, (((0, 0),), ((4, 5),))),
            ),
        )

    def test_STM32非连续位域保持所有位段(self):
        registers = {
            "block/TIM": {
                "items": [{"name": "SMCR", "byte_offset": 0, "fieldset": "SMCR"}]
            },
            "fieldset/SMCR": {
                "fields": [
                    {
                        "name": "SMS",
                        "bit_offset": [
                            {"start": 0, "end": 2},
                            {"start": 16, "end": 16},
                        ],
                        "bit_size": 4,
                    }
                ]
            },
        }

        signature = MODULE.st_block_signature(registers, "TIM")

        self.assertEqual(signature[0][2], (((0, 2), (16, 16)),))

    def test_同一结构布局允许保留不同来源记录(self):
        first = {
            "id": "rcu-test",
            "block": "RCU",
            "registers": [{"name": "CTL", "offset": 0, "width": 32}],
            "fields": [],
            "sources": [{"path": "a.h"}],
        }
        second = {**first, "sources": [{"path": "b.h"}]}
        variants = {
            "variants": [
                {"layouts": [first]},
                {"layouts": [second]},
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            report = MODULE.analyze(variants, Path(directory))

        self.assertEqual(report["summary"]["layouts"], 1)

    def test_STM32所需结构是GD32子集时可作为API候选(self):
        layout = {
            "id": "rcu-test",
            "block": "RCU",
            "registers": [
                {"name": "CTL", "offset": 0, "width": 32},
                {"name": "EXTRA", "offset": 4, "width": 32},
            ],
            "fields": [
                {
                    "name": "CTL_EN",
                    "register": "CTL",
                    "bit_offset": 0,
                    "bit_size": 1,
                },
                {
                    "name": "CTL_EXTRA",
                    "register": "CTL",
                    "bit_offset": 8,
                    "bit_size": 1,
                },
            ],
        }
        registers = {
            "block/RCC": {
                "items": [{"name": "CR", "byte_offset": 0, "fieldset": "CR"}]
            },
            "fieldset/CR": {
                "fields": [{"name": "EN", "bit_offset": 0, "bit_size": 1}]
            },
        }
        incompatible = {
            **registers,
            "fieldset/CR": {
                "fields": [{"name": "EN", "bit_offset": 1, "bit_size": 1}]
            },
        }

        self.assertTrue(MODULE.st_block_is_subset(layout, registers, "RCC"))
        self.assertFalse(MODULE.st_block_is_subset(layout, incompatible, "RCC"))


if __name__ == "__main__":
    unittest.main()
