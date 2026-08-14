import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "generate_gigadevice_embassy_projection.py"
SPEC = importlib.util.spec_from_file_location(
    "generate_gigadevice_embassy_projection", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def candidate(name: str, peripherals: list[str]) -> dict[str, object]:
    return {
        "name": name,
        "cores": [
            {
                "name": "cm3",
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


if __name__ == "__main__":
    unittest.main()
