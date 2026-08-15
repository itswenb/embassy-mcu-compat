import importlib.util
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "generate_gigadevice_embassy_projection.py"
SPEC = importlib.util.spec_from_file_location(
    "generate_gigadevice_embassy_projection", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def candidate(
    name: str, peripherals: list[str], core: str = "cm4"
) -> dict[str, object]:
    return {
        "name": name,
        "cores": [
            {
                "name": core,
                "peripherals": [
                    {"name": peripheral, "address": index * 1024}
                    for index, peripheral in enumerate(peripherals, 1)
                ],
                "interrupts": [],
            }
        ],
    }


def variant(devices: list[str] | None = None) -> dict[str, object]:
    names = ["RCU", "GPIOA", "FMC", "EXTI", "TIMER0", "TIMER4"]
    return {
        "id": "official-gd32f30x-test",
        "devices": devices or ["GD32F303CB"],
        "instances": [
            {"name": name, "address": index * 4096, "layout": f"layout-{name}"}
            for index, name in enumerate(names, 1)
        ],
        "layouts": [],
        "interrupts": [],
    }


def native_chip(peripherals: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": "GD32F303CB",
        "cores": [
            {
                "name": "cm4",
                "peripherals": peripherals,
                "interrupts": [],
                "dma_channels": [],
                "pins": [],
            }
        ],
    }


def native_chip_from_variant(hardware: dict[str, object]) -> dict[str, object]:
    return native_chip(
        [
            {
                "name": instance["name"],
                "address": instance["address"],
                "registers": {
                    "kind": f"gd{str(instance['name']).lower()}",
                    "version": "v1",
                    "block": str(instance["name"]),
                },
            }
            for instance in hardware["instances"]
        ]
    )


def apply_merge_patch(value: object, patch: object) -> object:
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(value) if isinstance(value, dict) else {}
    for key, item in patch.items():
        if item is None:
            result.pop(key, None)
        else:
            result[key] = apply_merge_patch(result.get(key), item)
    return result


class GenerateGigadeviceEmbassyProjectionTests(unittest.TestCase):
    def test_DMAMUX兼容视图保留真实通道数和请求位宽(self):
        native = {
            "block/DMAMUX::DMAMUX": {
                "items": [
                    {
                        "name": f"RM_CH{index}CFG",
                        "byte_offset": index * 4,
                        "fieldset": f"DMAMUX::regs::RM_CH{index}CFG",
                    }
                    for index in range(2)
                ]
                + [{"name": "RM_INTF", "byte_offset": 128}],
            },
            **{
                f"fieldset/DMAMUX::regs::RM_CH{index}CFG": {
                    "fields": [
                        {"name": "MUXID", "bit_offset": 0, "bit_size": 6},
                        {"name": "EVGEN", "bit_offset": 9, "bit_size": 1},
                        {"name": "NBR", "bit_offset": 19, "bit_size": 5},
                    ]
                }
                for index in range(2)
            },
        }

        reference, registers = MODULE._project_dmamux_registers(
            native, "DMAMUX::DMAMUX"
        )

        self.assertEqual(reference["kind"], "dmamux")
        self.assertEqual(reference["block"], "DMAMUX")
        block = registers["block/DMAMUX"]
        self.assertEqual(block["items"][0]["array"], {"len": 2, "stride": 4})
        self.assertEqual(block["items"][1]["name"], "RM_INTF")
        fields = registers["fieldset/CCR"]["fields"]
        self.assertEqual(
            [(field["name"], field["bit_size"]) for field in fields],
            [("DMAREQ_ID", 6), ("EGE", 1), ("NBREQ", 5)],
        )

    def test_外设拓扑优先选择包含TIM5的profile(self):
        model = {
            "id": "GD32F303CB",
            "core": "Cortex-M4",
            "rust_target": "thumbv7em-none-eabihf",
        }
        candidates = [
            candidate("STM32F103C8", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1"]),
            candidate(
                "STM32F103RG", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]
            ),
        ]

        result = MODULE.select_profile(model, variant(), candidates, {})

        self.assertEqual(result["profile"], "stm32f103rg")
        self.assertEqual(result["status"], "projected")

    def test_profile核心架构必须与真实芯片一致(self):
        model = {
            "id": "GD32L233C8",
            "core": "Cortex-M23",
            "rust_target": "thumbv8m.base-none-eabi",
        }
        candidates = [
            candidate("STM32L151QE", ["RCC", "GPIOA", "FLASH", "EXTI"], "cm3"),
            candidate("STM32U031C8", ["RCC", "GPIOA", "FLASH", "EXTI"], "cm0p"),
        ]

        result = MODULE.select_profile(model, variant(), candidates, {})

        self.assertEqual(result["profile"], "stm32u031c8")

    def test_profile路由外设必须匹配AFIO或SYSCFG(self):
        model = {
            "id": "GD32A503CB",
            "core": "Cortex-M33",
            "rust_target": "thumbv8m.main-none-eabihf",
        }
        hardware = variant(["GD32A503CB"])
        hardware["instances"].append(
            {"name": "SYSCFG", "address": 0x40010000, "layout": "layout-SYSCFG"}
        )
        f1 = candidate(
                "STM32F101C8",
                ["RCC", "GPIOA", "FLASH", "EXTI", "AFIO"],
            )
        next(
            peripheral
            for peripheral in f1["cores"][0]["peripherals"]
            if peripheral["name"] == "RCC"
        )["registers"] = {"kind": "rcc", "version": "f1", "block": "RCC"}
        candidates = [
            f1,
            candidate(
                "STM32G031K8",
                ["RCC", "GPIOA", "FLASH", "EXTI", "SYSCFG"],
                core="cm0p",
            ),
        ]

        result = MODULE.select_profile(
            model,
            hardware,
            candidates,
            {
                "layouts": [
                    {
                        "id": "layout-RCU",
                        "exact_candidates": [
                            {"kind": "rcc", "version": "f1", "block": "RCC"}
                        ],
                        "subset_candidates": [],
                    }
                ]
            },
        )

        self.assertEqual(result["profile"], "stm32g031k8")

    def test_RISCV型号明确阻塞(self):
        model = {
            "id": "GD32VF103CB",
            "core": "RV32IMAC",
            "rust_target": "riscv32imac-unknown-none-elf",
        }

        result = MODULE.select_profile(
            model,
            variant(["GD32VF103CB"]),
            [candidate("STM32F103RG", ["RCC", "GPIOA", "FLASH", "EXTI"])],
            {},
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["profile"])
        self.assertIn("RISC-V", result["reasons"])

    def test_profile外设API兼容时允许由真实芯片覆盖核心架构(self):
        result = MODULE.select_profile(
            {
                "id": "GD32F303CB",
                "core": "Cortex-M4",
                "rust_target": "thumbv7em-none-eabihf",
            },
            variant(),
            [
                candidate(
                    "STM32F103RG",
                    ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"],
                    core="cm3",
                )
            ],
            {},
        )

        self.assertEqual(result["status"], "projected")
        self.assertEqual(result["profile"], "stm32f103rg")

    def test_同分profile按名称稳定选择(self):
        model = {
            "id": "GD32F303CB",
            "core": "Cortex-M4",
            "rust_target": "thumbv7em-none-eabihf",
        }
        peripherals = ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]

        result = MODULE.select_profile(
            model,
            variant(),
            [candidate("STM32F103ZG", peripherals), candidate("STM32F103RG", peripherals)],
            {},
        )

        self.assertEqual(result["profile"], "stm32f103rg")

    def test_同拓扑优先匹配相同产品系列分支(self):
        model = {
            "id": "GD32F303CB",
            "core": "Cortex-M4",
            "rust_target": "thumbv7em-none-eabihf",
        }
        peripherals = ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]

        result = MODULE.select_profile(
            model,
            variant(),
            [
                candidate("STM32F427VG", peripherals),
                candidate("STM32F303CB", peripherals + ["ST_ONLY"]),
            ],
            {},
        )

        self.assertEqual(result["profile"], "stm32f303cb")

    def test_同系列profile优先使用寄存器兼容API(self):
        peripherals = ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]
        incompatible = candidate("STM32F303CA", peripherals)
        compatible = candidate("STM32F303CB", peripherals)
        for profile, version in ((incompatible, "v2"), (compatible, "v1")):
            next(
                peripheral
                for peripheral in profile["cores"][0]["peripherals"]
                if peripheral["name"] == "TIM5"
            )["registers"] = {"kind": "timer", "version": version, "block": "TIM_GP32"}

        result = MODULE.select_profile(
            {
                "id": "GD32F303CB",
                "core": "Cortex-M4",
                "rust_target": "thumbv7em-none-eabihf",
            },
            variant(),
            [incompatible, compatible],
            {
                "layouts": [
                    {
                        "id": "layout-TIMER4",
                        "exact_candidates": [],
                        "subset_candidates": [
                            {"kind": "timer", "version": "v1", "block": "TIM_GP32"}
                        ],
                    }
                ]
            },
        )

        self.assertEqual(result["profile"], "stm32f303cb")

    def test_profile报告数量从输入型号派生(self):
        models = {
            "devices": [
                {
                    "id": "GD32F303CB",
                    "core": "Cortex-M4",
                    "rust_target": "thumbv7em-none-eabihf",
                },
                {
                    "id": "GD32VF103CB",
                    "core": "RV32IMAC",
                    "rust_target": "riscv32imac-unknown-none-elf",
                },
            ]
        }
        variants = {
            "variants": [
                variant(["GD32F303CB"]),
                variant(["GD32VF103CB"]),
            ]
        }
        candidates = [
            candidate(
                "STM32F103RG", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]
            )
        ]

        report = MODULE.build_profile_report(models, variants, candidates, {})

        self.assertEqual(report["summary"]["devices"], 2)
        self.assertEqual(report["summary"]["projected"], 1)
        self.assertEqual(report["summary"]["blocked"], 1)
        self.assertEqual([row["chip"] for row in report["profiles"]], ["gd32f303cb", "gd32vf103cb"])

    def test_投影删除ST独有外设并保留GD地址(self):
        profile = candidate(
            "STM32F103RG",
            ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5", "ST_ONLY"],
        )
        model = {
            "id": "GD32F303CB",
            "core": "Cortex-M4",
            "rust_target": "thumbv7em-none-eabihf",
        }
        memory = [
            [
                {"name": "BANK_1", "kind": "flash", "address": 0x08000000, "size": 128 * 1024},
                {"name": "SRAM", "kind": "ram", "address": 0x20000000, "size": 32 * 1024},
            ]
        ]

        projected, unsupported = MODULE.project_chip(
            profile, model, variant(), {"memory": memory}
        )

        peripherals = {
            peripheral["name"]: peripheral
            for peripheral in projected["cores"][0]["peripherals"]
        }
        self.assertEqual(set(peripherals), {"RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"})
        self.assertEqual(peripherals["TIM5"]["address"], 6 * 4096)
        self.assertEqual(projected["memory"], memory)
        self.assertEqual(projected["name"], "GD32F303CB")
        self.assertEqual(unsupported, [])

    def test_投影外设拓扑与原生芯片一一对应(self):
        profile = candidate(
            "STM32F103RG",
            [
                "RCC",
                "GPIOA",
                "FLASH",
                "EXTI",
                "TIM1",
                "SPI1",
                "SPI2",
                "USART1",
                "ST_ONLY",
            ],
        )
        native = native_chip(
            [
                {
                    "name": "RCU",
                    "address": 0x40021000,
                    "registers": {"kind": "gdrcu", "version": "v1", "block": "RCU"},
                },
                {
                    "name": "GPIOA",
                    "address": 0x40010800,
                    "registers": {"kind": "gdgpio", "version": "v1", "block": "GPIO"},
                },
                {
                    "name": "FMC",
                    "address": 0x40022000,
                    "registers": {"kind": "gdfmc", "version": "v1", "block": "FMC"},
                },
                {
                    "name": "EXTI",
                    "address": 0x40010400,
                    "registers": {"kind": "gdexti", "version": "v1", "block": "EXTI"},
                },
                {
                    "name": "TIMER0",
                    "address": 0x40012C00,
                    "registers": {"kind": "gdtimer", "version": "v1", "block": "TIMER"},
                },
                {
                    "name": "CAN0",
                    "address": 0x40006400,
                    "registers": {"kind": "gdcan", "version": "v1", "block": "CAN"},
                },
                {
                    "name": "SPI0",
                    "address": 0x40013000,
                    "registers": {"kind": "gdspi", "version": "v1", "block": "SPI"},
                },
                {
                    "name": "USART0",
                    "address": 0x40013800,
                    "registers": {"kind": "gdusart", "version": "v1", "block": "USART"},
                },
                {
                    "name": "USART1",
                    "address": 0x40004400,
                    "registers": {"kind": "gdusart", "version": "v1", "block": "USART"},
                },
            ]
        )

        projected, native_only = MODULE.project_chip(
            profile,
            {"id": "GD32F303CB", "core": "Cortex-M4"},
            variant(),
            {"memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]]},
            native_chip=native,
        )

        peripherals = {
            peripheral["name"]: peripheral
            for peripheral in projected["cores"][0]["peripherals"]
        }
        self.assertEqual(
            set(peripherals),
            {
                "RCC",
                "GPIOA",
                "FLASH",
                "EXTI",
                "TIM1",
                "CAN1",
                "SPI1",
                "USART1",
                "USART2",
            },
        )
        self.assertEqual(peripherals["CAN1"]["address"], 0x40006400)
        self.assertEqual(peripherals["CAN1"]["registers"]["kind"], "gdcan")
        self.assertEqual(native_only, ["CAN1", "USART2"])

    def test_投影拒绝Embassy实例名碰撞(self):
        duplicated = variant()
        duplicated["instances"].append(
            {"name": "TIMER0", "address": 0xDEADBEEF, "layout": "layout-TIMER0-copy"}
        )

        with self.assertRaisesRegex(ValueError, "实例名碰撞"):
            MODULE.project_chip(
                candidate("STM32F103RG", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1"]),
                {"id": "GD32F303CB", "core": "Cortex-M4"},
                duplicated,
                {"memory": [[{"name": "SRAM", "kind": "ram", "address": 0x20000000, "size": 1}]]},
            )

    def test_merge_patch能完整重建投影(self):
        profile = candidate(
            "STM32F103RG", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]
        )
        projected, _ = MODULE.project_chip(
            profile,
            {"id": "GD32F303CB", "core": "Cortex-M4"},
            variant(),
            {"memory": [[{"name": "SRAM", "kind": "ram", "address": 0x20000000, "size": 1}]]},
        )

        patch = MODULE.merge_patch(profile, projected)

        self.assertEqual(apply_merge_patch(profile, patch), projected)

    def test_投影使用GD引脚和DMA事实(self):
        profile = candidate(
            "STM32F103RG",
            ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5", "DMA1"],
        )
        profile["cores"][0]["peripherals"][-1]["registers"] = {
            "kind": "dma",
            "version": "v1",
            "block": "DMA",
        }
        hardware = variant()
        hardware["instances"].append(
            {"name": "DMA0", "address": 0x40020000, "layout": "layout-DMA0"}
        )
        facts = {
            "memory": [[{"name": "SRAM", "kind": "ram", "address": 0x20000000, "size": 1}]],
            "pins": [{"name": "PA0"}],
            "dma_channels": [{"name": "DMA1_CH1", "dma": "DMA1", "channel": 0}],
            "peripheral_pins": {"TIM5": [{"pin": "PA0", "signal": "CH1"}]},
            "peripheral_dma_channels": {
                "TIM5": [{"signal": "CH1", "channel": "DMA1_CH1"}]
            },
            "peripheral_interrupts": {
                "DMA1": [{"signal": "CH1", "interrupt": "DMA1_CHANNEL1"}]
            },
        }

        projected, _ = MODULE.project_chip(
            profile,
            {"id": "GD32F303CB", "core": "Cortex-M4"},
            hardware,
            facts,
        )

        core = projected["cores"][0]
        tim5 = next(peripheral for peripheral in core["peripherals"] if peripheral["name"] == "TIM5")
        self.assertEqual(core["pins"], facts["pins"])
        self.assertEqual(core["dma_channels"], facts["dma_channels"])
        self.assertEqual(tim5["pins"], facts["peripheral_pins"]["TIM5"])
        self.assertEqual(tim5["dma_channels"], facts["peripheral_dma_channels"]["TIM5"])

    def test_引脚引用不存在的GPIO端口时阻塞投影(self):
        profile = candidate(
            "STM32F101C8", ["RCC", "GPIOA", "FLASH", "EXTI", "USART1"]
        )
        hardware = variant()
        hardware["instances"].append(
            {"name": "USART0", "address": 0x40013800, "layout": "layout-USART0"}
        )
        facts = {
            "memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]],
            "pins": [{"name": "PA9"}, {"name": "PF1"}],
            "peripheral_pins": {
                "USART1": [
                    {"pin": "PA9", "signal": "TX"},
                    {"pin": "PF1", "signal": "TX"},
                ]
            },
        }

        with self.assertRaisesRegex(ValueError, "引脚.*GPIOF"):
            MODULE.project_chip(
                profile,
                {"id": "GD32A503CB", "core": "Cortex-M33"},
                hardware,
                facts,
            )

    def test_投影保留超出profile但有真实中断的DMA通道(self):
        profile = candidate(
            "STM32F103RF", ["RCC", "GPIOA", "FLASH", "EXTI", "DMA2"]
        )
        dma = next(
            peripheral
            for peripheral in profile["cores"][0]["peripherals"]
            if peripheral["name"] == "DMA2"
        )
        dma["interrupts"] = [
            {"signal": f"CH{index}", "interrupt": f"DMA2_CHANNEL{index}"}
            for index in range(1, 6)
        ]
        dma["registers"] = {"kind": "dma", "version": "v1", "block": "DMA"}
        hardware = variant(["GD32F205RC"])
        hardware["instances"].append(
            {"name": "DMA1", "address": 0x40020400, "layout": "layout-DMA1"}
        )
        facts = {
            "memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]],
            "dma_channels": [
                {"name": "DMA2_CH5", "dma": "DMA2", "channel": 4},
                {"name": "DMA2_CH6", "dma": "DMA2", "channel": 5},
            ],
            "peripheral_interrupts": {
                "DMA2": [
                    {"signal": f"CH{index}", "interrupt": f"DMA2_CHANNEL{index}"}
                    for index in range(1, 7)
                ]
            },
        }

        projected, _ = MODULE.project_chip(
            profile,
            {"id": "GD32F205RC", "core": "Cortex-M3"},
            hardware,
            facts,
        )

        self.assertEqual(
            projected["cores"][0]["dma_channels"],
            [
                {"name": "DMA2_CH5", "dma": "DMA2", "channel": 4},
                {"name": "DMA2_CH6", "dma": "DMA2", "channel": 5},
            ],
        )
        projected_dma = next(
            peripheral
            for peripheral in projected["cores"][0]["peripherals"]
            if peripheral["name"] == "DMA2"
        )
        self.assertIn(
            {"signal": "CH6", "interrupt": "DMA2_CHANNEL6"},
            projected_dma["interrupts"],
        )

    def test_真实DMA通道缺少中断绑定时阻塞而不裁剪(self):
        profile = candidate(
            "STM32WBA62MG", ["RCC", "GPIOA", "FLASH", "EXTI", "DMA1"]
        )
        profile["cores"][0]["peripherals"][-1]["registers"] = {
            "kind": "dma",
            "version": "v1",
            "block": "DMA",
        }
        hardware = variant(["GD32A503CB"])
        hardware["instances"].append(
            {"name": "DMA0", "address": 0x40020000, "layout": "layout-DMA0"}
        )

        with self.assertRaisesRegex(ValueError, "DMA channel DMA1_CH1 缺少真实中断"):
            MODULE.project_chip(
                profile,
                {"id": "GD32A503CB", "core": "Cortex-M33"},
                hardware,
                {
                    "memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]],
                    "dma_channels": [
                        {"name": "DMA1_CH1", "dma": "DMA1", "channel": 0}
                    ],
                    "peripheral_interrupts": {"DMA1": []},
                },
                native_chip=native_chip_from_variant(hardware),
            )

    def test_真实DMA只有原生寄存器契约时整芯片阻塞(self):
        profile = candidate(
            "STM32WBA62MG", ["RCC", "GPIOA", "FLASH", "EXTI", "GPDMA1"]
        )
        hardware = variant(["GD32A503CB"])
        hardware["instances"].append(
            {"name": "DMA0", "address": 0x40020000, "layout": "layout-DMA0"}
        )

        with self.assertRaisesRegex(ValueError, "DMA1 无可用 Embassy 寄存器契约"):
            MODULE.project_chip(
                profile,
                {"id": "GD32A503CB", "core": "Cortex-M33"},
                hardware,
                {
                    "memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]],
                    "dma_channels": [
                        {"name": "DMA1_CH1", "dma": "DMA1", "channel": 0}
                    ],
                    "peripheral_interrupts": {
                        "DMA1": [
                            {"signal": "CH1", "interrupt": "DMA1_CHANNEL1"}
                        ]
                    },
                },
                native_chip=native_chip_from_variant(hardware),
            )

    def test_归一引脚DMA和中断到Embassy编号(self):
        profile = candidate(
            "STM32F103RG", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM5", "DMA2"]
        )
        profile["cores"][0]["interrupts"] = [{"name": "TIM5", "number": 50}]
        hardware = variant()
        hardware["interrupts"] = [{"name": "TIMER4", "value": 50}]
        memory = {
            "device": "GD32F303CB",
            "memory": [
                {"name": "IROM1", "kind": "flash", "address": 0x08000000, "size": 128},
                {"name": "IRAM1", "kind": "ram", "address": 0x20000000, "size": 32},
            ],
        }
        pins = {
            "id": "GD32F303CB",
            "status": "normalized",
            "pins": [
                {
                    "name": "PA0",
                    "functions": [{"name": "TIMER4_CH0", "source": "alternate"}],
                }
            ],
        }
        dma = {
            "id": "GD32F303CB",
            "status": "normalized",
            "dma_channels": [
                {"name": "DMA1_CH0", "dma": "DMA1", "channel": 0}
            ],
            "dma_requests": [
                {
                    "binding": {
                        "kind": "peripheral",
                        "peripheral": "TIMER4",
                        "signal": "CH0",
                    },
                    "dma": "DMA1",
                    "channel": 0,
                    "request": 9,
                }
            ],
        }

        facts = MODULE.build_projection_facts(profile, hardware, memory, pins, dma)

        self.assertEqual(facts["interrupts"], [{"name": "TIM5", "number": 50}])
        self.assertEqual(facts["peripheral_pins"]["TIM5"], [{"pin": "PA0", "signal": "CH1"}])
        self.assertEqual(
            facts["dma_channels"], [{"name": "DMA2_CH1", "dma": "DMA2", "channel": 0}]
        )
        self.assertEqual(
            facts["peripheral_dma_channels"]["TIM5"],
            [{"signal": "CH1", "channel": "DMA2_CH1"}],
        )

    def test_生成事实为真实额外DMA通道补齐中断绑定(self):
        profile = candidate(
            "STM32F103RF", ["RCC", "GPIOA", "FLASH", "EXTI", "DMA2"]
        )
        profile_dma = next(
            peripheral
            for peripheral in profile["cores"][0]["peripherals"]
            if peripheral["name"] == "DMA2"
        )
        profile_dma["interrupts"] = [
            {"signal": "CH1", "interrupt": "DMA2_Channel1"}
        ]
        hardware = variant(["GD32F205RC"])
        hardware["instances"].append(
            {"name": "DMA1", "address": 0x40020400, "layout": "layout-DMA1"}
        )
        hardware["interrupts"] = [
            {"name": "DMA1_Channel0", "value": 56},
            {"name": "DMA1_Channel1", "value": 57},
        ]

        facts = MODULE.build_projection_facts(
            profile,
            hardware,
            {"memory": [{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]},
            {"status": "normalized", "pins": []},
            {
                "status": "normalized",
                "dma_channels": [
                    {"name": "DMA1_CH0", "dma": "DMA1", "channel": 0},
                    {"name": "DMA1_CH1", "dma": "DMA1", "channel": 1},
                ],
                "dma_requests": [],
            },
        )

        self.assertEqual(
            facts["peripheral_interrupts"]["DMA2"],
            [
                {"signal": "CH1", "interrupt": "DMA2_CHANNEL1"},
                {"signal": "CH2", "interrupt": "DMA2_CHANNEL2"},
            ],
        )

    def test_中断名按真实外设编号归一且不依赖profile向量位置(self):
        native_names = ["DMA0", "DMA1", "CAN0", "CAN1", "TIMER0", "TIMER8"]

        self.assertEqual(
            MODULE._normalized_interrupt_name("DMA0_Channel3", native_names),
            "DMA1_CHANNEL4",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name("DMA1_Channel4", native_names),
            "DMA2_CHANNEL5",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name("DMA1_Channel3_Channel4", native_names),
            "DMA2_CHANNEL4_5",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name("CAN1_RX1", native_names),
            "CAN2_RX1",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name("CAN0_EWMC", native_names),
            "CAN1_SCE",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name("TIMER0_BRK_TIMER8", native_names),
            "TIM1_BRK_TIM9",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name("TIMER0_Channel", native_names),
            "TIM1_CC",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name("TIMER0_TRG_CMT", native_names),
            "TIM1_TRG_COM",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name(
                "TIMER0_TRG_CMT_TIMER10", native_names + ["TIMER10"]
            ),
            "TIM1_TRG_COM_TIM11",
        )

    def test_共享中断中的第二个零起点实例也归一(self):
        self.assertEqual(
            MODULE._normalized_interrupt_name("ADC0_1", ["ADC0", "ADC1"]),
            "ADC1_2",
        )
        self.assertEqual(
            MODULE._project_interrupt_bindings(
                "ADC2",
                [{"signal": "GLOBAL", "interrupt": "ADC1_2"}],
                {"ADC1_2"},
            ),
            [{"signal": "GLOBAL", "interrupt": "ADC1_2"}],
        )

    def test_GD32定时器引脚信号归一为Embassy命名(self):
        self.assertEqual(MODULE._mapped_signal("TIMER0", "CH0_ON"), "CH1N")
        self.assertEqual(MODULE._mapped_signal("TIMER0", "ETI"), "ETR")
        self.assertEqual(MODULE._mapped_signal("TIMER0", "BRKIN"), "BKIN")

    def test_缺少profile必需中断的外设保留为原生PAC(self):
        profile = candidate(
            "STM32F103RF", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM9"]
        )
        profile["cores"][0]["peripherals"][-1]["interrupts"] = [
            {"signal": "UP", "interrupt": "TIM1_UP_TIM10"}
        ]
        hardware = variant()
        hardware["instances"].append(
            {"name": "TIMER8", "address": 0x40014C00, "layout": "layout-TIMER8"}
        )

        projected, native_only = MODULE.project_chip(
            profile,
            {
                "id": "GD32F303CB",
                "core": "Cortex-M4",
                "rust_target": "thumbv7em-none-eabihf",
            },
            hardware,
            {
                "memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 2}]],
                "interrupts": [],
                "pins": [{"name": "PA2"}],
                "dma_channels": [],
                "peripheral_pins": {"TIM9": [{"pin": "PA2", "signal": "CH1"}]},
                "peripheral_dma_channels": {},
                "peripheral_interrupts": {"TIM9": []},
            },
            native_chip=native_chip_from_variant(hardware),
        )

        tim9 = next(
            row
            for row in projected["cores"][0]["peripherals"]
            if row["name"] == "TIM9"
        )
        self.assertEqual(tim9["registers"]["kind"], "gdtimer8")
        self.assertIn("TIM9", native_only)

    def test_无法投影DMA时整芯片阻塞(self):
        profile = candidate(
            "STM32F205RB", ["RCC", "GPIOA", "FLASH", "EXTI", "DMA1", "TIM5"]
        )
        next(
            peripheral
            for peripheral in profile["cores"][0]["peripherals"]
            if peripheral["name"] == "DMA1"
        )["interrupts"] = [
            {"signal": "CH0", "interrupt": "DMA1_Stream0"}
        ]
        hardware = variant()
        hardware["instances"].append(
            {"name": "DMA0", "address": 0x40026000, "layout": "layout-DMA0"}
        )

        with self.assertRaisesRegex(ValueError, "关键外设无法投影.*DMA1"):
            MODULE.project_chip(
                profile,
                {"id": "GD32A503CB", "core": "Cortex-M33"},
                hardware,
                {
                    "memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 2}]],
                    "interrupts": [],
                    "pins": [],
                    "dma_channels": [{"name": "DMA1_CH0", "dma": "DMA1", "channel": 0}],
                    "peripheral_pins": {},
                    "peripheral_interrupts": {"DMA1": [], "TIM5": []},
                    "peripheral_dma_channels": {
                        "TIM5": [{"signal": "UP", "channel": "DMA1_CH0"}]
                    },
                },
            )

    def test_无法投影关键系统外设时整芯片阻塞(self):
        profile = candidate(
            "STM32WB10CC", ["RCC", "GPIOA", "FLASH", "EXTI"]
        )
        rcc = next(
            peripheral
            for peripheral in profile["cores"][0]["peripherals"]
            if peripheral["name"] == "RCC"
        )
        rcc["interrupts"] = [
            {"signal": "GLOBAL", "interrupt": "RCC"},
            {"signal": "LSECSS", "interrupt": "TAMP_STAMP_LSECSS"},
        ]

        with self.assertRaisesRegex(ValueError, "关键系统外设无法投影.*RCC"):
            MODULE.project_chip(
                profile,
                {"id": "GD32G533CC", "core": "Cortex-M33"},
                variant(["GD32G533CC"]),
                {
                    "memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 2}]],
                    "peripheral_interrupts": {
                        "RCC": [{"signal": "GLOBAL", "interrupt": "RCC"}]
                    },
                },
            )

    def test_profile依赖FLASH但真实芯片没有FLASH契约时阻塞(self):
        profile = candidate(
            "STM32F411CC", ["RCC", "GPIOA", "FLASH", "EXTI"]
        )
        hardware = variant(["GD32H779II"])
        hardware["instances"] = [
            {"name": "RCU", "address": 0x40023800, "layout": "layout-RCU"},
            {"name": "GPIOA", "address": 0x40020000, "layout": "layout-GPIOA"},
            {"name": "EXMC", "address": 0x52004000, "layout": "layout-EXMC"},
            {"name": "NVMC", "address": 0x52002000, "layout": "layout-NVMC"},
            {"name": "EXTI", "address": 0x40013C00, "layout": "layout-EXTI"},
        ]

        with self.assertRaisesRegex(ValueError, "关键系统外设无法投影.*FLASH"):
            MODULE.project_chip(
                profile,
                {"id": "GD32H779II", "core": "Cortex-M7"},
                hardware,
                {
                    "memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 2}]],
                    "peripheral_interrupts": {},
                },
                native_chip=native_chip_from_variant(hardware),
            )

    def test_DMA分离中断按profile信号绑定到真实向量(self):
        bindings = [
            {"signal": "CH4", "interrupt": "DMA2_Channel4_5"},
            {"signal": "CH5", "interrupt": "DMA2_Channel4_5"},
        ]

        self.assertEqual(
            MODULE._project_interrupt_bindings(
                "DMA2", bindings, {"DMA2_CHANNEL4", "DMA2_CHANNEL5"}
            ),
            [
                {"signal": "CH4", "interrupt": "DMA2_CHANNEL4"},
                {"signal": "CH5", "interrupt": "DMA2_CHANNEL5"},
            ],
        )

    def test_DMA通道编号基准服从profile契约(self):
        native_names = ["DMA0"]

        self.assertEqual(
            MODULE._normalized_interrupt_name(
                "DMA0_Channel0", native_names, {"DMA1": 0}
            ),
            "DMA1_CHANNEL0",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name(
                "DMA0_Channel0", native_names, {"DMA1": 1}
            ),
            "DMA1_CHANNEL1",
        )
        self.assertEqual(
            MODULE._normalized_interrupt_name(
                "DMA_Channel1_2", ["DMA"], {"DMA1": 1}
            ),
            "DMA1_CHANNEL2_3",
        )

    def test_profile选择优先满足真实中断契约(self):
        hardware = variant()
        hardware["instances"].append(
            {"name": "DMA", "address": 0x40020000, "layout": "layout-DMA"}
        )
        hardware["interrupts"] = [
            {"name": "DMA_Channel0", "value": 9},
            {"name": "DMA_Channel1_2", "value": 10},
            {"name": "DMA_Channel3_4", "value": 11},
        ]
        f0 = candidate(
            "STM32F051C4", ["RCC", "GPIOA", "FLASH", "EXTI", "DMA1"], core="cm0p"
        )
        f4 = candidate("STM32F412ZE", ["RCC", "GPIOA", "FLASH", "EXTI", "DMA1"])
        f0_dma = f0["cores"][0]["peripherals"][-1]
        f4_dma = f4["cores"][0]["peripherals"][-1]
        f0_dma["interrupts"] = [
            {"signal": f"CH{channel}", "interrupt": interrupt}
            for channel, interrupt in (
                (1, "DMA1_Channel1"),
                (2, "DMA1_Channel2_3"),
                (3, "DMA1_Channel2_3"),
                (4, "DMA1_Channel4_5"),
                (5, "DMA1_Channel4_5"),
            )
        ]
        f4_dma["interrupts"] = [
            {"signal": f"CH{channel}", "interrupt": f"DMA1_Stream{channel}"}
            for channel in range(8)
        ]
        f0_dma["registers"] = {"kind": "bdma", "version": "v1", "block": "DMA"}
        f4_dma["registers"] = {"kind": "dma", "version": "v2", "block": "DMA"}

        result = MODULE.select_profile(
            {
                "id": "GD32E230C4",
                "core": "Cortex-M23",
                "rust_target": "thumbv8m.base-none-eabi",
            },
            hardware,
            [f4, f0],
            {
                "layouts": [
                    {
                        "id": "layout-DMA",
                        "exact_candidates": [
                            {"kind": "dma", "version": "v2", "block": "DMA"}
                        ],
                        "subset_candidates": [],
                    }
                ]
            },
        )

        self.assertEqual(result["profile"], "stm32f051c4")

    def test_DMA请求号只在profile要求时保留(self):
        profile = candidate("STM32F429BE", ["DMA1", "TIM5"])
        profile["cores"][0]["peripherals"][0]["interrupts"] = [
            {"signal": "CH0", "interrupt": "DMA1_Stream0"}
        ]
        profile["cores"][0]["peripherals"][1]["dma_channels"] = [
            {"signal": "CH1", "channel": "DMA1_CH0", "request": 0}
        ]
        hardware = variant()
        hardware["instances"].append(
            {"name": "DMA0", "address": 0x40026000, "layout": "layout-DMA0"}
        )

        facts = MODULE.build_projection_facts(
            profile,
            hardware,
            {"memory": [{"name": "SRAM", "kind": "ram", "address": 1, "size": 2}]},
            {"status": "normalized", "pins": []},
            {
                "status": "normalized",
                "kind": "fixed",
                "dma_channels": [{"dma": "DMA0", "channel": 0}],
                "dma_requests": [
                    {
                        "dma": "DMA0",
                        "channel": 0,
                        "request": 3,
                        "binding": {
                            "kind": "peripheral",
                            "peripheral": "TIMER4",
                            "signal": "CH0",
                        },
                    }
                ],
            },
        )

        self.assertEqual(
            facts["peripheral_dma_channels"]["TIM5"],
            [{"signal": "CH1", "channel": "DMA1_CH0", "request": 3}],
        )

    def test_独立Flash基址按真实顺序生成bank名(self):
        facts = MODULE.build_projection_facts(
            candidate("STM32F205RB", []),
            variant(),
            {
                "memory": [
                    {"name": "IROM1", "kind": "flash", "address": 0x08000000, "size": 1},
                    {"name": "IROM2", "kind": "flash", "address": 0x08800000, "size": 1},
                    {"name": "IRAM1", "kind": "ram", "address": 0x20000000, "size": 1},
                ]
            },
            {"status": "normalized", "pins": []},
            {"status": "normalized", "dma_channels": [], "dma_requests": []},
        )

        self.assertEqual(
            [row["name"] for row in facts["memory"][0]],
            ["BANK_1", "BANK_2", "SRAM"],
        )

    def test_DMAMUX请求保留真实请求号(self):
        profile = candidate(
            "STM32G0B1CB", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM5", "DMA1", "DMAMUX1"]
        )
        hardware = variant()
        hardware["instances"].append(
            {"name": "DMAMUX", "address": 0x40020800, "layout": "layout-DMAMUX"}
        )
        facts = MODULE.build_projection_facts(
            profile,
            hardware,
            {
                "memory": [
                    {"name": "IROM1", "kind": "flash", "address": 1, "size": 2}
                ]
            },
            {"status": "normalized", "pins": []},
            {
                "status": "normalized",
                "kind": "dmamux",
                "dma_channels": [
                    {
                        "name": "DMA0_CH0",
                        "dma": "DMA0",
                        "channel": 0,
                        "dmamux_channel": 0,
                    }
                ],
                "dma_requests": [
                    {
                        "request": 5,
                        "binding": {
                            "kind": "peripheral",
                            "peripheral": "TIMER4",
                            "signal": "CH0",
                        },
                    }
                ],
            },
        )

        self.assertEqual(
            facts["peripheral_dma_channels"]["TIM5"],
            [{"signal": "CH1", "dmamux": "DMAMUX1", "request": 5}],
        )
        self.assertEqual(facts["dma_channels"][0]["dmamux_channel"], 0)

    def test_投影使用已验证且能力最完整的寄存器API(self):
        profile = candidate(
            "STM32F303CB", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM5"]
        )
        tim5 = next(
            peripheral
            for peripheral in profile["cores"][0]["peripherals"]
            if peripheral["name"] == "TIM5"
        )
        tim5["registers"] = {"kind": "timer", "version": "v2", "block": "TIM_GP16"}
        hardware = variant()
        facts = MODULE.build_projection_facts(
            profile,
            hardware,
            {
                "memory": [
                    {"name": "IROM1", "kind": "flash", "address": 1, "size": 2}
                ]
            },
            {"status": "normalized", "pins": []},
            {"status": "normalized", "dma_channels": [], "dma_requests": []},
            {
                "layouts": [
                    {
                        "id": "layout-TIMER4",
                        "status": "subset",
                        "exact_candidates": [],
                        "subset_candidates": [
                            {
                                "kind": "timer",
                                "version": "v1",
                                "block": "TIM_BASIC",
                                "fields": 5,
                                "registers": 4,
                            },
                            {
                                "kind": "timer",
                                "version": "v1",
                                "block": "TIM_GP32",
                                "fields": 40,
                                "registers": 20,
                            },
                        ],
                    }
                ]
            },
        )
        facts["peripheral_interrupts"]["TIM5"] = [
            {"signal": "GLOBAL", "interrupt": "TIM5"}
        ]

        projected, _ = MODULE.project_chip(
            profile,
            {"id": "GD32F303CB", "core": "Cortex-M4"},
            hardware,
            facts,
        )
        projected_tim5 = next(
            peripheral
            for peripheral in projected["cores"][0]["peripherals"]
            if peripheral["name"] == "TIM5"
        )
        self.assertEqual(
            projected_tim5["registers"],
            {"kind": "timer", "version": "v1", "block": "TIM_GP32"},
        )

    def test_定时器Embassy块由真实通道数和计数位宽决定(self):
        ir = {
            "block/TIMER1::TIMER1": {
                "items": [
                    {"name": "CNT", "byte_offset": 36, "fieldset": "CNT"},
                    *[
                        {
                            "name": f"CH{channel}CV",
                            "byte_offset": 52 + channel * 4,
                        }
                        for channel in range(4)
                    ],
                ]
            },
            "fieldset/CNT": {
                "fields": [{"name": "CNT", "bit_offset": 0, "bit_size": 32}]
            },
        }

        self.assertEqual(
            MODULE._classify_timer_block(ir, "TIMER1::TIMER1"), "TIM_GP32"
        )

    def test_无寄存器兼容证据的SPI保留为原生PAC(self):
        profile = candidate(
            "STM32F103RC", ["RCC", "GPIOA", "FLASH", "EXTI", "SPI1"]
        )
        spi = profile["cores"][0]["peripherals"][-1]
        spi["registers"] = {"kind": "spi", "version": "v1", "block": "SPI"}
        hardware = variant()
        hardware["instances"].append(
            {"name": "SPI0", "address": 0x40013000, "layout": "layout-SPI0"}
        )
        facts = MODULE.build_projection_facts(
            profile,
            hardware,
            {"memory": [{"name": "IROM1", "kind": "flash", "address": 1, "size": 2}]},
            {"status": "normalized", "pins": []},
            {"status": "normalized", "dma_channels": [], "dma_requests": []},
            {"layouts": []},
        )

        projected, native_only = MODULE.project_chip(
            profile,
            {"id": "GD32F303CB", "core": "Cortex-M4"},
            hardware,
            facts,
            native_chip=native_chip_from_variant(hardware),
        )

        projected_spi = next(
            peripheral
            for peripheral in projected["cores"][0]["peripherals"]
            if peripheral["name"] == "SPI1"
        )
        self.assertEqual(projected_spi["registers"]["kind"], "gdspi0")
        self.assertIn("SPI1", native_only)

    def test_AFIO无兼容证据时带重映射的外设保留为原生PAC(self):
        profile = candidate(
            "STM32F101C8", ["RCC", "GPIOA", "FLASH", "EXTI", "AFIO", "USART1"]
        )
        usart = next(
            peripheral
            for peripheral in profile["cores"][0]["peripherals"]
            if peripheral["name"] == "USART1"
        )
        usart["afio"] = {
            "register": "MAPR",
            "field": "USART1_REMAP",
            "values": [{"value": 0, "pins": ["PA9"]}],
        }
        hardware = variant()
        hardware["instances"].extend(
            [
                {"name": "AFIO", "address": 0x40010000, "layout": "layout-AFIO"},
                {"name": "USART0", "address": 0x40013800, "layout": "layout-USART0"},
            ]
        )

        projected, native_only = MODULE.project_chip(
            profile,
            {"id": "GD32A503CB", "core": "Cortex-M33"},
            hardware,
            {
                "memory": [
                    [{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]
                ],
                "peripheral_registers": {
                    "USART1": {"kind": "usart", "version": "v1", "block": "USART"}
                },
            },
            native_chip=native_chip_from_variant(hardware),
        )

        projected_usart = next(
            peripheral
            for peripheral in projected["cores"][0]["peripherals"]
            if peripheral["name"] == "USART1"
        )
        self.assertNotIn("afio", projected_usart)
        self.assertIn("USART1", native_only)

    def test_profile要求但真实不存在的公共实例不加入投影(self):
        profile = candidate(
            "STM32F103RG",
            ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5", "ADC12_COMMON"],
        )
        projected, _ = MODULE.project_chip(
            profile,
            {"id": "GD32F303CB", "core": "Cortex-M4"},
            variant(),
            {"memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]]},
        )

        names = {row["name"] for row in projected["cores"][0]["peripherals"]}
        self.assertNotIn("ADC12_COMMON", names)

    def test_缺少真实ADC公共实例时ADC保留原生PAC(self):
        profile = candidate(
            "STM32F429BE",
            ["RCC", "GPIOA", "FLASH", "EXTI", "ADC1", "ADC123_COMMON"],
        )
        adc = next(
            row for row in profile["cores"][0]["peripherals"] if row["name"] == "ADC1"
        )
        adc["registers"] = {"kind": "adc", "version": "v2", "block": "ADC"}
        hardware = variant(["GD32A490II"])
        hardware["instances"].append(
            {"name": "ADC0", "address": 0x40012000, "layout": "layout-ADC0"}
        )

        projected, native_only = MODULE.project_chip(
            profile,
            {"id": "GD32A490II", "core": "Cortex-M4"},
            hardware,
            {"memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]]},
            native_chip=native_chip_from_variant(hardware),
        )

        adc1 = next(
            row for row in projected["cores"][0]["peripherals"] if row["name"] == "ADC1"
        )
        self.assertEqual(adc1["registers"]["kind"], "gdadc0")
        self.assertIn("ADC1", native_only)

    def test_缺少校准地址时ADC保留原生PAC但不伪装Embassy驱动(self):
        profile = candidate(
            "STM32F030C8",
            ["RCC", "GPIOA", "FLASH", "EXTI", "ADC1", "VREFINTCAL"],
        )
        adc = next(
            peripheral
            for peripheral in profile["cores"][0]["peripherals"]
            if peripheral["name"] == "ADC1"
        )
        adc["registers"] = {"kind": "adc", "version": "v2", "block": "ADC"}
        hardware = variant(["GD32E230C4"])
        hardware["instances"].append(
            {"name": "ADC0", "address": 0x40012400, "layout": "layout-ADC0"}
        )

        projected, native_only = MODULE.project_chip(
            profile,
            {"id": "GD32E230C4", "core": "Cortex-M23"},
            hardware,
            {"memory": [[{"name": "SRAM", "kind": "ram", "address": 1, "size": 1}]]},
            native_chip=native_chip_from_variant(hardware),
        )

        adc1 = next(
            row
            for row in projected["cores"][0]["peripherals"]
            if row["name"] == "ADC1"
        )
        self.assertEqual(adc1["registers"]["kind"], "gdadc0")
        self.assertIn("ADC1", native_only)

    def test_manifest动态汇总并能重建投影(self):
        profile = candidate(
            "STM32F103RG", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]
        )
        hardware = variant()
        profile_report = {
            "profiles": [
                {
                    "chip": "gd32f303cb",
                    "profile": "stm32f103rg",
                    "rust_target": "thumbv7em-none-eabihf",
                    "status": "projected",
                    "variant": hardware["id"],
                },
                {
                    "chip": "gd32vf103cb",
                    "profile": None,
                    "rust_target": "riscv32imac-unknown-none-elf",
                    "status": "blocked",
                    "reasons": "RISC-V 不适用",
                    "variant": None,
                },
            ]
        }
        inputs = {
            "models": {
                "gd32f303cb": {
                    "id": "GD32F303CB",
                    "core": "Cortex-M4",
                    "rust_target": "thumbv7em-none-eabihf",
                }
            },
            "variants": {hardware["id"]: hardware},
            "official_profiles": {"stm32f103rg": profile},
            "native_chips": {
                "gd32f303cb": native_chip_from_variant(hardware),
            },
            "memory": {
                "gd32f303cb": {
                    "device": "GD32F303CB",
                    "memory": [
                        {"name": "IROM1", "kind": "flash", "address": 0x08000000, "size": 128},
                        {"name": "IRAM1", "kind": "ram", "address": 0x20000000, "size": 32},
                    ],
                }
            },
            "pins": {"gd32f303cb": {"status": "normalized", "pins": []}},
            "dma": {
                "gd32f303cb": {
                    "status": "normalized",
                    "dma_channels": [],
                    "dma_requests": [],
                }
            },
            "rcu": {
                "gd32f303cb": {
                    "status": "normalized",
                    "binding_status": "normalized",
                    "gate_status": "normalized",
                }
            },
            "source_hashes": {"memory_sha256": "abc"},
        }

        manifest = MODULE.build_projection_manifest(profile_report, inputs)

        self.assertEqual(manifest["summary"], {"devices": 2, "projected": 1, "blocked": 1})
        projected = manifest["projections"][0]
        self.assertEqual(projected["chip"], "gd32f303cb")
        self.assertEqual(projected["profile"], "stm32f103rg")
        self.assertEqual(projected["source_hashes"], {"memory_sha256": "abc"})
        self.assertEqual(apply_merge_patch(profile, projected["patch"])["name"], "GD32F303CB")

    def test_命令行同时写出profile和manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            official = root / "official"
            official.mkdir()
            native = root / "native"
            native.mkdir()
            hardware = variant()
            files = {
                "models.json": {
                    "devices": [
                        {
                            "id": "GD32F303CB",
                            "core": "Cortex-M4",
                            "rust_target": "thumbv7em-none-eabihf",
                        }
                    ]
                },
                "variants.json": {"variants": [hardware]},
                "register.json": {},
                "memory.json": {
                    "profiles": [
                        {
                            "device": "GD32F303CB",
                            "memory": [
                                {"name": "IROM1", "kind": "flash", "address": 1, "size": 2}
                            ],
                        }
                    ]
                },
                "pins.json": {
                    "devices": [
                        {"id": "GD32F303CB", "status": "normalized", "pins": []}
                    ]
                },
                "dma.json": {
                    "devices": [
                        {
                            "id": "GD32F303CB",
                            "status": "normalized",
                            "dma_channels": [],
                            "dma_requests": [],
                        }
                    ]
                },
                "rcu.json": {
                    "devices": [
                        {
                            "id": "GD32F303CB",
                            "status": "normalized",
                            "binding_status": "normalized",
                            "gate_status": "normalized",
                        }
                    ]
                },
            }
            for name, value in files.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            (official / "STM32F103RG.json").write_text(
                json.dumps(
                    candidate(
                        "STM32F103RG",
                        ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"],
                    )
                ),
                encoding="utf-8",
            )
            (native / "GD32F303CB.json").write_text(
                json.dumps(native_chip_from_variant(hardware)), encoding="utf-8"
            )
            output = root / "profiles.json"
            manifest = root / "manifest.json"
            registers_output = root / "registers-output"
            argv = [
                str(MODULE_PATH),
                "--models", str(root / "models.json"),
                "--variants", str(root / "variants.json"),
                "--register-compat", str(root / "register.json"),
                "--official-chips", str(official),
                "--native-chips", str(native),
                "--memory", str(root / "memory.json"),
                "--pins", str(root / "pins.json"),
                "--dma", str(root / "dma.json"),
                "--rcu", str(root / "rcu.json"),
                "--output", str(output),
                "--manifest", str(manifest),
                "--registers-output", str(registers_output),
            ]

            with patch.object(sys, "argv", argv):
                self.assertEqual(MODULE.main(), 0)

            self.assertEqual(json.loads(output.read_text())["summary"]["projected"], 1)
            self.assertEqual(json.loads(manifest.read_text())["summary"]["projected"], 1)
            self.assertTrue((registers_output / ".m32-embassy-registers.json").is_file())


if __name__ == "__main__":
    unittest.main()
