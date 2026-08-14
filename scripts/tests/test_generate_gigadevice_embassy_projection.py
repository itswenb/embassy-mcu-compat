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

    def test_profile核心架构必须和真实芯片一致(self):
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

        self.assertEqual(result["status"], "blocked")
        self.assertIn("核心架构", result["reasons"])

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
            "STM32F103RG", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]
        )
        facts = {
            "memory": [[{"name": "SRAM", "kind": "ram", "address": 0x20000000, "size": 1}]],
            "pins": [{"name": "PA0"}],
            "dma_channels": [{"name": "DMA1_CH1", "dma": "DMA1", "channel": 0}],
            "peripheral_pins": {"TIM5": [{"pin": "PA0", "signal": "CH1"}]},
            "peripheral_dma_channels": {
                "TIM5": [{"signal": "CH1", "channel": "DMA1_CH1"}]
            },
        }

        projected, _ = MODULE.project_chip(
            profile,
            {"id": "GD32F303CB", "core": "Cortex-M4"},
            variant(),
            facts,
        )

        core = projected["cores"][0]
        tim5 = next(peripheral for peripheral in core["peripherals"] if peripheral["name"] == "TIM5")
        self.assertEqual(core["pins"], facts["pins"])
        self.assertEqual(core["dma_channels"], facts["dma_channels"])
        self.assertEqual(tim5["pins"], facts["peripheral_pins"]["TIM5"])
        self.assertEqual(tim5["dma_channels"], facts["peripheral_dma_channels"]["TIM5"])

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

    def test_寄存器布局使用通过兼容门的API版本(self):
        profile = candidate(
            "STM32F303CB", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM5"]
        )
        tim5 = next(
            peripheral
            for peripheral in profile["cores"][0]["peripherals"]
            if peripheral["name"] == "TIM5"
        )
        tim5["registers"] = {"kind": "timer", "version": "v2", "block": "TIM_GP32"}
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
                            {"kind": "timer", "version": "v1", "block": "TIM_GP32"}
                        ],
                    }
                ]
            },
        )

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

    def test_同一芯片同类外设选择共同寄存器版本(self):
        profile = candidate(
            "STM32F303CB", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]
        )
        peripherals = {
            peripheral["name"]: peripheral
            for peripheral in profile["cores"][0]["peripherals"]
        }
        peripherals["TIM1"]["registers"] = {
            "kind": "timer", "version": "v1", "block": "TIM_ADV"
        }
        peripherals["TIM5"]["registers"] = {
            "kind": "timer", "version": "l0", "block": "TIM_GP32"
        }
        facts = MODULE.build_projection_facts(
            profile,
            variant(),
            {"memory": [{"name": "IROM1", "kind": "flash", "address": 1, "size": 2}]},
            {"status": "normalized", "pins": []},
            {"status": "normalized", "dma_channels": [], "dma_requests": []},
            {
                "layouts": [
                    {
                        "id": "layout-TIMER0",
                        "exact_candidates": [],
                        "subset_candidates": [
                            {"kind": "timer", "version": "v1", "block": "TIM_ADV"}
                        ],
                    },
                    {
                        "id": "layout-TIMER4",
                        "exact_candidates": [],
                        "subset_candidates": [
                            {"kind": "timer", "version": "l0", "block": "TIM_GP32"},
                            {"kind": "timer", "version": "v1", "block": "TIM_GP32"},
                        ],
                    },
                ]
            },
        )

        self.assertEqual(
            {row["version"] for row in facts["peripheral_registers"].values()},
            {"v1"},
        )

    def test_未验证同类实例没有共同版本时阻塞(self):
        profile = candidate(
            "STM32A508VE", ["RCC", "GPIOA", "FLASH", "EXTI", "TIM1", "TIM5"]
        )
        for peripheral in profile["cores"][0]["peripherals"]:
            if peripheral["name"] in {"TIM1", "TIM5"}:
                peripheral["registers"] = {
                    "kind": "timer", "version": "v4", "block": peripheral["name"]
                }

        with self.assertRaisesRegex(ValueError, "没有芯片内共同兼容版本"):
            MODULE.build_projection_facts(
                profile,
                variant(),
                {"memory": [{"name": "IROM1", "kind": "flash", "address": 1, "size": 2}]},
                {"status": "normalized", "pins": []},
                {"status": "normalized", "dma_channels": [], "dma_requests": []},
                {
                    "layouts": [
                        {
                            "id": "layout-TIMER0",
                            "exact_candidates": [],
                            "subset_candidates": [
                                {"kind": "timer", "version": "v1", "block": "TIM_ADV"}
                            ],
                        }
                    ]
                },
            )

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
            output = root / "profiles.json"
            manifest = root / "manifest.json"
            argv = [
                str(MODULE_PATH),
                "--models", str(root / "models.json"),
                "--variants", str(root / "variants.json"),
                "--register-compat", str(root / "register.json"),
                "--official-chips", str(official),
                "--memory", str(root / "memory.json"),
                "--pins", str(root / "pins.json"),
                "--dma", str(root / "dma.json"),
                "--rcu", str(root / "rcu.json"),
                "--output", str(output),
                "--manifest", str(manifest),
            ]

            with patch.object(sys, "argv", argv):
                self.assertEqual(MODULE.main(), 0)

            self.assertEqual(json.loads(output.read_text())["summary"]["projected"], 1)
            self.assertEqual(json.loads(manifest.read_text())["summary"]["projected"], 1)


if __name__ == "__main__":
    unittest.main()
