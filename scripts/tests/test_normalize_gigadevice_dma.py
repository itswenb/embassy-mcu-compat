import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "normalize_gigadevice_dma.py"
SPEC = importlib.util.spec_from_file_location("normalize_gigadevice_dma", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class DmaTests(unittest.TestCase):
    def test_请求按最长外设名前缀绑定并保留系统请求(self):
        peripherals = {
            "ADC",
            "ADC0",
            "DAC",
            "EVIC",
            "I2C0",
            "MFCOM",
            "SPI",
            "TIMER0",
            "EDIM_AFMT",
        }

        cases = {
            "ADC0_ROUTINE": ("peripheral", "ADC0", "ROUTINE"),
            "I2C0_RX": ("peripheral", "I2C0", "RX"),
            "TIMER0": ("peripheral", "TIMER0", "TIMER0"),
            "M2M": ("system", None, "M2M"),
            "GENERATOR3": ("system", None, "GENERATOR3"),
            "SSTAT2": ("peripheral", "MFCOM", "SSTAT2"),
            "DAC0_CH0": ("peripheral", "DAC", "CH0"),
            "EVIC11": ("peripheral", "EVIC", "EVIC11"),
            "SPI0_TX": ("peripheral", "SPI", "TX"),
            "AFMT": ("peripheral", "EDIM_AFMT", "AFMT"),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                binding, issue = MODULE.bind_request(peripherals, name)
                self.assertIsNone(issue)
                self.assertEqual(
                    (
                        binding["kind"],
                        binding.get("peripheral"),
                        binding["signal"],
                    ),
                    expected,
                )

        binding, issue = MODULE.bind_request(peripherals, "UNKNOWN_RX")
        self.assertIsNone(binding)
        self.assertEqual(issue["reason"], "peripheral-missing")

    def test_从中断闭包建立dma与dmamux通道(self):
        variant = {
            "instances": [
                {"name": "DMA0"},
                {"name": "DMA1"},
                {"name": "DMAMUX"},
            ],
            "interrupts": [
                {"name": "DMA0_Channel0", "value": 11},
                {"name": "DMA0_Channel1", "value": 12},
                {"name": "DMAMUX", "value": 47},
                {"name": "DMA1_Channel0", "value": 56},
            ],
        }
        dma = {"kind": "dmamux", "dmamux_channels": [0, 1, 2]}

        channels, issues = MODULE.build_channels(variant, dma)

        self.assertEqual(issues, [])
        self.assertEqual(
            channels,
            [
                {
                    "name": "DMA0_CH0",
                    "dma": "DMA0",
                    "channel": 0,
                    "interrupt": "DMA0_Channel0",
                    "interrupt_number": 11,
                    "dmamux": "DMAMUX",
                    "dmamux_channel": 0,
                },
                {
                    "name": "DMA0_CH1",
                    "dma": "DMA0",
                    "channel": 1,
                    "interrupt": "DMA0_Channel1",
                    "interrupt_number": 12,
                    "dmamux": "DMAMUX",
                    "dmamux_channel": 1,
                },
                {
                    "name": "DMA1_CH0",
                    "dma": "DMA1",
                    "channel": 0,
                    "interrupt": "DMA1_Channel0",
                    "interrupt_number": 56,
                    "dmamux": "DMAMUX",
                    "dmamux_channel": 2,
                },
            ],
        )

    def test_从官方枚举建立mdma通道与请求(self):
        variant = {
            "instances": [{"name": "DMA0"}, {"name": "MDMA"}],
            "interrupts": [{"name": "MDMA", "value": 122}],
        }
        mdma = {
            "channels": [0, 1],
            "requests": [
                {"kind": "hardware", "name": "DMA0_CH0_FTFIF", "request": 0},
                {"kind": "software", "name": "SW", "request": 0x40000000},
            ],
        }

        channels, requests, issues = MODULE.build_mdma(
            variant, mdma, {"DMA0", "MDMA"}
        )

        self.assertEqual(issues, [])
        self.assertEqual(
            channels,
            [
                {
                    "name": "MDMA_CH0",
                    "dma": "MDMA",
                    "channel": 0,
                    "interrupt": "MDMA",
                    "interrupt_number": 122,
                },
                {
                    "name": "MDMA_CH1",
                    "dma": "MDMA",
                    "channel": 1,
                    "interrupt": "MDMA",
                    "interrupt_number": 122,
                },
            ],
        )
        self.assertEqual(requests[0]["binding"]["peripheral"], "DMA0")
        self.assertEqual(requests[0]["binding"]["signal"], "CH0_FTFIF")
        self.assertEqual(requests[1]["binding"], {"kind": "system", "signal": "SW"})

    def test_设备级报告同时归一dmamux与手册固定映射(self):
        variants = {
            "summary": {"normalized_devices": 3, "variants": 2},
            "variants": [
                {
                    "id": "mux",
                    "series": "GD32MUX",
                    "devices": ["GD32MUX1"],
                    "instances": [{"name": "DMA"}, {"name": "DMAMUX"}],
                    "interrupts": [{"name": "DMA_Channel0", "value": 10}],
                    "dma": {
                        "source": {"path": "mux_dma.h", "sha256": "a" * 64},
                        "kind": "dmamux",
                        "dmamux_channels": [0],
                        "requests": [
                            {
                                "name": "ADC_RX",
                                "request": 5,
                                "source_name": "DMA_REQUEST_ADC_RX",
                            }
                        ],
                    },
                },
                {
                    "id": "fixed",
                    "series": "GD32F10x",
                    "devices": ["GD32F103C8"],
                    "instances": [{"name": "DMA"}, {"name": "ADC"}],
                    "interrupts": [{"name": "DMA_Channel0", "value": 10}],
                    "layouts": [],
                    "dma": {
                        "source": {"path": "fixed_dma.h", "sha256": "b" * 64},
                        "kind": "fixed",
                        "dma_channels": [0],
                        "dmamux_channels": [],
                        "requests": [],
                    },
                },
            ],
        }
        rcu = {
            "devices": [
                {"id": "GD32MUX1", "variant": "mux", "peripheral_names": ["ADC"]},
                {
                    "id": "GD32F103C8",
                    "variant": "fixed",
                    "peripheral_names": ["ADC"],
                },
                {"id": "GD32MISSING1", "variant": None, "peripheral_names": []},
            ]
        }

        manuals = {
            "schema_version": 1,
            "manuals": [
                {
                    "name": "GD32F10x User Manual",
                    "pdf": {"filename": "f10.pdf", "sha256": "c" * 64},
                    "tables": [
                        {
                            "number": "9-3",
                            "controller": "DMA",
                            "channels": [0],
                            "applies_to": [],
                            "routes": [
                                {
                                    "channel": 0,
                                    "signal": "ADC",
                                    "source": {"page": 10},
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        full, report = MODULE.build_outputs(variants, rcu, manuals)

        self.assertEqual(
            [device["status"] for device in full["devices"]],
            ["normalized", "missing", "normalized"],
        )
        self.assertEqual(report["summary"]["devices_with_normalized_dma"], 2)
        self.assertEqual(report["summary"]["devices_with_fixed_request_map_missing"], 0)
        self.assertEqual(report["summary"]["devices_without_dma_source"], 1)
        fixed = next(device for device in full["devices"] if device["id"] == "GD32F103C8")
        self.assertEqual(fixed["dma_requests"][0]["channel"], 0)
        self.assertEqual(fixed["dma_requests"][0]["binding"]["peripheral"], "ADC")

    def test_手册脚注按firmware字段自动生成重映射(self):
        variant = {
            "layouts": [
                {
                    "id": "syscfg",
                    "fields": [
                        {
                            "name": "SYSCFG_CFG0_USART0_TX_DMA_RMP",
                            "register": "SYSCFG_CFG0",
                            "bit_size": 1,
                        }
                    ],
                    "sources": [{"path": "syscfg.h", "sha256": "d" * 64}],
                }
            ]
        }

        remap, issue = MODULE.build_remap(
            variant, "USART0_TX", 2
        )

        self.assertIsNone(issue)
        self.assertEqual(remap["register"], "SYSCFG_CFG0")
        self.assertEqual(remap["field"], "SYSCFG_CFG0_USART0_TX_DMA_RMP")
        self.assertEqual(remap["value"], 1)


if __name__ == "__main__":
    unittest.main()
