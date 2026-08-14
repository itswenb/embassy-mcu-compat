import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "generate_gigadevice_stm32_data.py"
SPEC = importlib.util.spec_from_file_location(
    "generate_gigadevice_stm32_data", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class Stm32DataTests(unittest.TestCase):
    def test_RISCV核心名称保留ISA而不是降级为unknown(self):
        variant = {
            "id": "gd32riscv-v1",
            "series": "GD32RISCV",
            "instances": [],
            "interrupts": [],
        }
        for core, expected in (
            ("RV32IMAC", "riscv32imac"),
            ("RV32IMAFC", "riscv32imafc"),
        ):
            with self.subTest(core=core):
                chip = MODULE._chip(
                    {"id": "GD32RISCV", "core": core}, variant, {}, []
                )
                self.assertEqual(chip["cores"][0]["name"], expected)

    def test_真实变体生成共享register_IR和每型号Chip(self):
        models = {
            "devices": [
                {
                    "id": "GD32TESTA",
                    "core": "Cortex-M4",
                    "cmsis_devices": ["GD32TESTA"],
                },
                {
                    "id": "GD32TESTB",
                    "core": "Cortex-M4",
                    "cmsis_devices": ["GD32TESTB"],
                },
            ]
        }
        variants = {
            "variants": [
                {
                    "id": "gd32test-v1",
                    "series": "GD32TEST",
                    "devices": ["GD32TESTA", "GD32TESTB"],
                    "layouts": [
                        {
                            "id": "gpio-0123456789abcdef",
                            "block": "GPIO",
                            "registers": [
                                {
                                    "name": "GPIO_CTL",
                                    "offset": 0,
                                    "width": 32,
                                    "array_parameters": {
                                        "port": {"start": 0, "end": 1, "stride": 4}
                                    },
                                }
                            ],
                            "fields": [
                                {
                                    "name": "GPIO_CTL_MODE",
                                    "register": "GPIO_CTL",
                                    "bit_offset": 0,
                                    "bit_size": 2,
                                }
                            ],
                        }
                    ],
                    "instances": [
                        {
                            "name": "GPIOA",
                            "address": 0x40010800,
                            "layout": "gpio-0123456789abcdef",
                        }
                    ],
                    "interrupts": [{"name": "EXTI0", "value": 6}],
                }
            ],
            "missing": [],
        }

        memory = {
            "devices": [
                {
                    "id": "GD32TESTA",
                    "memory_status": "normalized",
                    "profiles": ["GD32TESTA"],
                },
                {
                    "id": "GD32TESTB",
                    "memory_status": "normalized",
                    "profiles": ["GD32TESTB1", "GD32TESTB2"],
                },
            ],
            "profiles": [
                {
                    "device": "GD32TESTA",
                    "memory": [
                        {
                            "name": "IROM1",
                            "kind": "flash",
                            "address": 0x08000000,
                            "size": 0x10000,
                        },
                        {
                            "name": "IRAM1",
                            "kind": "ram",
                            "address": 0x20000000,
                            "size": 0x5000,
                        },
                    ],
                },
                {
                    "device": "GD32TESTB1",
                    "memory": [
                        {"name": "IRAM1", "kind": "ram", "address": 0x20000000, "size": 1}
                    ],
                },
                {
                    "device": "GD32TESTB2",
                    "memory": [
                        {"name": "IRAM1", "kind": "ram", "address": 0x20000000, "size": 2}
                    ],
                },
            ],
        }

        staging = MODULE.build_staging(models, variants, memory)

        self.assertEqual(sorted(staging["chips"]), ["GD32TESTA", "GD32TESTB"])
        self.assertEqual(len(staging["registers"]), 1)
        chip = staging["chips"]["GD32TESTA"]
        self.assertEqual(chip["cores"][0]["name"], "cm4")
        self.assertEqual(
            chip["memory"],
            [[
                {
                    "name": "IROM1",
                    "kind": "flash",
                    "address": 0x08000000,
                    "size": 0x10000,
                },
                {
                    "name": "IRAM1",
                    "kind": "ram",
                    "address": 0x20000000,
                    "size": 0x5000,
                },
            ]],
        )
        self.assertEqual(staging["chips"]["GD32TESTB"]["memory"], [])
        self.assertEqual(chip["cores"][0]["interrupts"], [{"name": "EXTI0", "number": 6}])
        registers = chip["cores"][0]["peripherals"][0]["registers"]
        register_ir = staging["registers"][f"{registers['kind']}_{registers['version']}"]
        block = register_ir[f"block/{registers['block']}"]
        self.assertEqual(
            [(item["name"], item["byte_offset"]) for item in block["items"]],
            [("GPIO_CTL_0", 0), ("GPIO_CTL_1", 4)],
        )
        self.assertEqual(
            register_ir["fieldset/GPIO_CTL"]["fields"][0],
            {"name": "GPIO_CTL_MODE", "bit_offset": 0, "bit_size": 2},
        )


if __name__ == "__main__":
    unittest.main()
