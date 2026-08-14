import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "normalize_gigadevice_rcu.py"
SPEC = importlib.util.spec_from_file_location("normalize_gigadevice_rcu", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RcuTests(unittest.TestCase):
    def test_IAR_SVD可直接提供无Firmware型号的RCU门控(self):
        variants = {
            "summary": {"normalized_devices": 1, "variants_with_rcu": 0},
            "variants": [],
            "missing_devices": [
                {"device": "GD32A711AR", "reason": "firmware-series-not-matched"}
            ],
        }
        models = {"devices": [{"id": "GD32A711AR", "cmsis_devices": []}]}
        iar = {
            "devices": [{"id": "GD32A711AR", "svd": "GD32A71x.svd"}],
            "svd_files": [{"path": "GD/GD32A71x.svd", "sha256": "iar-sha"}],
        }
        iar_svds = {
            "svds": [
                {
                    "sha256": "iar-sha",
                    "peripheral_names": ["CCTL", "DMA0", "GPIOA"],
                    "rcu_gates": [
                        {
                            "kind": "enable",
                            "name": "DMA0",
                            "register": "AHBEN",
                            "field": "DMA0EN",
                            "register_offset": 0x14,
                            "bit": 14,
                        },
                        {
                            "kind": "enable",
                            "name": "PA",
                            "register": "AHBEN",
                            "field": "PAEN",
                            "register_offset": 0x14,
                            "bit": 0,
                        },
                    ],
                }
            ]
        }

        full, report = MODULE.build_outputs(
            variants, models, iar=iar, iar_svds=iar_svds
        )

        self.assertEqual(report["devices"][0]["status"], "normalized")
        self.assertEqual(report["devices"][0]["enable_gates"], 2)
        self.assertEqual(
            full["devices"][0]["enable"][1]["binding"]["peripherals"],
            ["GPIOA"],
        )

    def test_从设备头基址提取外设名并排除总线与内存(self):
        self.assertEqual(
            MODULE.base_instance_names(
                {
                    "USBFS_BASE": 0x50000000,
                    "CAU_BASE_NS": 0x40025000,
                    "CAU_BASE_S": 0x50025000,
                    "WIFI_RF_BASE": 0x40200000,
                    "APB1_BUS_BASE": 0x40000000,
                    "FLASH_BASE": 0x08000000,
                    "SRAM0_BASE": 0x20000000,
                    "USBD_RAM_BASE": 0x40006000,
                }
            ),
            ["CAU", "USBFS", "WIFI_RF"],
        )

    def test_门控绑定支持精确索引辅助别名和系统资源(self):
        instances = {
            "DMA0": [{"name": "DMA0"}],
            "DAC0": [{"name": "DAC0"}],
            "DAC1": [{"name": "DAC1"}],
            "ENET": [{"name": "ENET"}],
            "AFIO": [{"name": "AFIO"}],
            "SYSCFG": [{"name": "SYSCFG"}],
            "CMP": [{"name": "CMP"}],
            "SYSCFG": [{"name": "SYSCFG"}],
            "USBFS_GLOBAL": [{"name": "USBFS_GLOBAL"}],
            "USBFS_DEVICE": [{"name": "USBFS_DEVICE"}],
            "USBHS": [{"name": "USBHS"}],
            "EDIM_AFMT": [{"name": "EDIM_AFMT"}],
            "WIFI_RF": [{"name": "WIFI_RF"}],
            "TZBMPC0": [{"name": "TZBMPC0"}],
            "TZBMPC1": [{"name": "TZBMPC1"}],
            "TZIAC": [{"name": "TZIAC"}],
            "TZSPC": [{"name": "TZSPC"}],
        }

        cases = {
            "DMA0": ("peripheral", "exact", ["DMA0"]),
            "DAC": ("peripheral", "indexed", ["DAC0", "DAC1"]),
            "ENETTX": ("peripheral", "auxiliary", ["ENET"]),
            "AF": ("peripheral", "alias", ["AFIO"]),
            "CFG": ("peripheral", "alias", ["SYSCFG"]),
            "CFGCMP": ("peripheral", "alias", ["CMP", "SYSCFG"]),
            "USBFS": (
                "peripheral",
                "grouped",
                ["USBFS_DEVICE", "USBFS_GLOBAL"],
            ),
            "ULPI": ("peripheral", "alias", ["USBHS"]),
            "USBHS0": ("system", "unmodeled-usbhs-instance", []),
            "USBHS1ULPI": ("system", "unmodeled-usbhs-instance", []),
            "RF": ("peripheral", "alias", ["WIFI_RF"]),
            "AFMT": ("peripheral", "alias", ["EDIM_AFMT"]),
            "TZPCU": (
                "peripheral",
                "trustzone-group",
                ["TZBMPC0", "TZBMPC1", "TZIAC", "TZSPC"],
            ),
            "BLE": ("system", "wireless-subsystem", []),
            "RFI": ("system", "wireless-subsystem", []),
            "BKP": ("system", "system-memory", []),
            "SRAM0": ("system", "system-memory", []),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                binding, issue = MODULE.bind_gate(instances, name)
                self.assertIsNone(issue)
                self.assertEqual(
                    (binding["kind"], binding["rule"], binding["peripherals"]),
                    expected,
                )

        binding, issue = MODULE.bind_gate(instances, "UNKNOWN")
        self.assertIsNone(binding)
        self.assertEqual(issue["reason"], "peripheral-missing")

    def test_按寄存器偏移和位号解析不规则字段名(self):
        layout = {
            "registers": [
                {"name": "RCU_APB2EN", "offset": 0x18, "width": 32, "parameters": []}
            ],
            "fields": [
                {
                    "name": "RCU_APB2EN_PAEN",
                    "register": "RCU_APB2EN",
                    "bit_offset": 2,
                    "bit_size": 1,
                }
            ],
        }

        gate, issue = MODULE.resolve_gate(
            layout, {"name": "GPIOA", "register_offset": 0x18, "bit": 2}
        )

        self.assertIsNone(issue)
        self.assertEqual(
            gate,
            {
                "name": "GPIOA",
                "register": "RCU_APB2EN",
                "field": "RCU_APB2EN_PAEN",
                "register_offset": 0x18,
                "bit": 2,
            },
        )

    def test_字段缺失或重叠时保留阻塞问题(self):
        missing, missing_issue = MODULE.resolve_gate(
            {"registers": [], "fields": []},
            {"name": "DMA0", "register_offset": 0x14, "bit": 0},
        )
        ambiguous, ambiguous_issue = MODULE.resolve_gate(
            {
                "registers": [
                    {"name": "RCU_AHBEN", "offset": 0x14, "width": 32, "parameters": []}
                ],
                "fields": [
                    {
                        "name": "RCU_AHBEN_DMA0EN",
                        "register": "RCU_AHBEN",
                        "bit_offset": 0,
                        "bit_size": 1,
                    },
                    {
                        "name": "RCU_AHBEN_DMAEN",
                        "register": "RCU_AHBEN",
                        "bit_offset": 0,
                        "bit_size": 1,
                    },
                ],
            },
            {"name": "DMA0", "register_offset": 0x14, "bit": 0},
        )

        self.assertIsNone(missing)
        self.assertEqual(missing_issue["reason"], "register-missing")
        self.assertIsNone(ambiguous)
        self.assertEqual(ambiguous_issue["reason"], "field-ambiguous")

    def test_唯一寄存器可由Firmware枚举直接提供门控位(self):
        gate, issue = MODULE.resolve_gate(
            {
                "registers": [
                    {"name": "RCU_AHB2RST", "offset": 0x38, "parameters": []}
                ],
                "fields": [],
            },
            {"name": "TRNG", "register_offset": 0x38, "bit": 3},
        )

        self.assertIsNone(issue)
        self.assertEqual(gate["field"], None)
        self.assertEqual(gate["resolution"], "firmware-enum")

    def test_字段宏可纠正厂商枚举中的错误位号(self):
        layout = {
            "registers": [
                {"name": "RCU_APB1RST", "offset": 0x10, "parameters": []}
            ],
            "fields": [
                {
                    "name": "RCU_APB1RST_LPUARTRST",
                    "register": "RCU_APB1RST",
                    "bit_offset": 18,
                    "bit_size": 1,
                }
            ],
        }

        gate, issue = MODULE.resolve_firmware_field_gate(
            layout,
            "reset",
            {"name": "LPUART", "register_offset": 0x10, "bit": 25},
        )

        self.assertIsNone(issue)
        self.assertEqual(gate["bit"], 18)
        self.assertEqual(gate["resolution"], "firmware-field")

    def test_归一变体并显式保留无来源型号(self):
        variants = {
            "summary": {"normalized_devices": 3, "variants_with_rcu": 1},
            "variants": [
                {
                    "id": "gd32-test",
                    "series": "GD32TEST",
                    "devices": ["GD32TEST1", "GD32TEST2"],
                    "instances": [
                        {
                            "name": "RCU",
                            "address": 0x40021000,
                            "layout": "rcu-layout",
                        },
                        {
                            "name": "DMA0",
                            "address": 0x40020000,
                            "layout": "dma-layout",
                        },
                    ],
                    "layouts": [
                        {
                            "id": "rcu-layout",
                            "registers": [
                                {
                                    "name": "RCU_AHBEN",
                                    "offset": 0x14,
                                    "width": 32,
                                    "parameters": [],
                                },
                                {
                                    "name": "RCU_AHBRST",
                                    "offset": 0x28,
                                    "width": 32,
                                    "parameters": [],
                                },
                            ],
                            "fields": [
                                {
                                    "name": "RCU_AHBEN_DMA0EN",
                                    "register": "RCU_AHBEN",
                                    "bit_offset": 0,
                                    "bit_size": 1,
                                },
                                {
                                    "name": "RCU_AHBRST_DMA0RST",
                                    "register": "RCU_AHBRST",
                                    "bit_offset": 0,
                                    "bit_size": 1,
                                },
                            ],
                        }
                    ],
                    "rcu": {
                        "source": {"path": "gd32test_rcu.h", "sha256": "abc"},
                        "enable": [
                            {"name": "DMA0", "register_offset": 0x14, "bit": 0}
                        ],
                        "reset": [
                            {"name": "DMA0", "register_offset": 0x28, "bit": 0}
                        ],
                    },
                }
            ],
            "missing_devices": [
                {"device": "GD32TEST3", "reason": "firmware-series-not-matched"}
            ],
        }

        models = {
            "devices": [
                {"id": "GD32TEST1", "cmsis_devices": ["GD32TEST1"]},
                {"id": "GD32TEST2", "cmsis_devices": ["GD32TEST2"]},
                {"id": "GD32TEST3", "cmsis_devices": ["GD32TEST3"]},
            ]
        }

        full, report = MODULE.build_outputs(variants, models)

        self.assertEqual(full["variants"][0]["status"], "normalized")
        self.assertEqual(full["variants"][0]["gate_status"], "normalized")
        self.assertEqual(full["variants"][0]["binding_status"], "normalized")
        self.assertEqual(
            full["variants"][0]["enable"][0]["field"], "RCU_AHBEN_DMA0EN"
        )
        self.assertEqual(
            full["variants"][0]["enable"][0]["binding"]["peripherals"], ["DMA0"]
        )
        self.assertEqual(
            report["summary"],
            {
                "normalized_devices": 3,
                "variants": 1,
                "variants_with_normalized_gate_table": 1,
                "variants_with_gate_table_conflict": 0,
                "variants_with_normalized_rcu": 1,
                "variants_with_rcu_conflict": 0,
                "devices_with_normalized_gate_table": 2,
                "devices_with_gate_table_conflict": 0,
                "devices_with_normalized_rcu": 2,
                "devices_with_rcu_conflict": 0,
                "devices_without_rcu_source": 1,
                "devices_with_svd_only_rcu": 0,
                "enable_gates": 1,
                "reset_gates": 1,
                "bound_enable_gates": 1,
                "bound_reset_gates": 1,
                "system_gates": 0,
                "unbound_gates": 0,
                "issues": 0,
            },
        )
        self.assertEqual(report["devices"][0]["gate_status"], "normalized")
        self.assertEqual(report["devices"][-1]["status"], "missing")

    def test_规范型号聚合同一Firmware变体的多个CMSIS名称(self):
        variants = {
            "summary": {"normalized_devices": 2, "variants_with_rcu": 1},
            "variants": [
                {
                    "id": "aggregate-test",
                    "series": "GD32TEST",
                    "devices": ["GD32RAW1", "GD32RAW2"],
                    "instances": [
                        {"name": "RCU", "address": 0x40021000, "layout": "rcu"}
                    ],
                    "layouts": [{"id": "rcu", "registers": [], "fields": []}],
                    "rcu": {
                        "source": {"path": "rcu.h", "sha256": "abc"},
                        "enable": [],
                        "reset": [],
                    },
                }
            ],
            "missing_devices": [],
        }
        models = {
            "devices": [
                {
                    "id": "GD32AGG",
                    "cmsis_devices": ["GD32RAW1", "GD32RAW2"],
                }
            ]
        }

        _, report = MODULE.build_outputs(variants, models)

        self.assertEqual(report["summary"]["normalized_devices"], 1)
        self.assertEqual(report["devices"][0]["id"], "GD32AGG")

    def test_SVD实例补齐Firmware结构体外设的门控绑定(self):
        variants = {
            "summary": {"normalized_devices": 1, "variants_with_rcu": 1},
            "variants": [
                {
                    "id": "usb-test",
                    "series": "GD32TEST",
                    "devices": ["GD32USB"],
                    "instances": [
                        {"name": "RCU", "address": 0x40021000, "layout": "rcu"}
                    ],
                    "layouts": [
                        {
                            "id": "rcu",
                            "registers": [
                                {
                                    "name": "RCU_AHBEN",
                                    "offset": 0x14,
                                    "parameters": [],
                                },
                                {
                                    "name": "RCU_AHBRST",
                                    "offset": 0x28,
                                    "parameters": [],
                                },
                            ],
                            "fields": [
                                {
                                    "name": "RCU_AHBEN_USBFSEN",
                                    "register": "RCU_AHBEN",
                                    "bit_offset": 12,
                                    "bit_size": 1,
                                },
                                {
                                    "name": "RCU_AHBRST_USBFSRST",
                                    "register": "RCU_AHBRST",
                                    "bit_offset": 12,
                                    "bit_size": 1,
                                },
                            ],
                        }
                    ],
                    "rcu": {
                        "source": {"path": "rcu.h", "sha256": "abc"},
                        "enable": [
                            {"name": "USBFS", "register_offset": 0x14, "bit": 12}
                        ],
                        "reset": [
                            {"name": "USBFS", "register_offset": 0x28, "bit": 12}
                        ],
                    },
                }
            ],
            "missing_devices": [],
        }
        models = {"devices": [{"id": "GD32USB", "cmsis_devices": ["GD32USB"]}]}
        resources = {
            "devices": [
                {
                    "device": "GD32USB",
                    "debug": [{"file": {"sha256": "svd-sha"}}],
                }
            ]
        }
        svds = {
            "svds": [
                {
                    "sha256": "svd-sha",
                    "peripheral_names": ["USBFS_DEVICE", "USBFS_GLOBAL"],
                }
            ]
        }

        full, report = MODULE.build_outputs(
            variants, models, resources=resources, svds=svds
        )

        self.assertEqual(report["variants"][0]["status"], "conflict")
        self.assertEqual(report["devices"][0]["status"], "normalized")
        self.assertEqual(
            full["devices"][0]["enable"][0]["binding"]["peripherals"],
            ["USBFS_DEVICE", "USBFS_GLOBAL"],
        )

    def test_SVD门控消解Firmware枚举与宏冲突(self):
        variants = {
            "summary": {"normalized_devices": 1, "variants_with_rcu": 1},
            "variants": [
                {
                    "id": "rcu-conflict",
                    "series": "GD32TEST",
                    "devices": ["GD32TEST1"],
                    "instances": [
                        {"name": "RCU", "address": 0x40021000, "layout": "rcu"},
                        {"name": "LPUART", "address": 0x40008000, "layout": "uart"},
                    ],
                    "layouts": [
                        {
                            "id": "rcu",
                            "registers": [
                                {"name": "RCU_APB1RST", "offset": 0x10, "parameters": []}
                            ],
                            "fields": [],
                        }
                    ],
                    "rcu": {
                        "source": {"path": "rcu.h", "sha256": "abc"},
                        "enable": [],
                        "reset": [
                            {"name": "LPUART", "register_offset": 0x10, "bit": 25}
                        ],
                    },
                }
            ],
            "missing_devices": [],
        }
        models = {"devices": [{"id": "GD32TEST1", "cmsis_devices": ["GD32TEST1"]}]}
        resources = {
            "devices": [
                {
                    "device": "GD32TEST1",
                    "debug": [{"file": {"sha256": "svd-sha"}}],
                }
            ]
        }
        svds = {
            "svds": [
                {
                    "sha256": "svd-sha",
                    "peripheral_names": ["LPUART"],
                    "rcu_gates": [
                        {
                            "kind": "reset",
                            "name": "LPUART",
                            "register": "APB1RST",
                            "field": "LPUARTRST",
                            "register_offset": 0x10,
                            "bit": 18,
                        }
                    ],
                }
            ]
        }

        full, report = MODULE.build_outputs(
            variants, models, resources=resources, svds=svds
        )

        self.assertEqual(report["devices"][0]["status"], "normalized")
        self.assertEqual(full["devices"][0]["reset"][0]["bit"], 18)
        self.assertEqual(full["devices"][0]["reset"][0]["resolution"], "svd")
        self.assertEqual(
            full["devices"][0]["reset"][0]["firmware_entry"]["bit"], 25
        )

    def test_现行Pack可否定Firmware中的过期门控(self):
        variants = {
            "summary": {"normalized_devices": 1, "variants_with_rcu": 1},
            "variants": [
                {
                    "id": "stale-gate",
                    "series": "GD32TEST",
                    "devices": ["GD32TEST1"],
                    "instances": [{"name": "RCU", "address": 1, "layout": "rcu"}],
                    "layouts": [
                        {
                            "id": "rcu",
                            "registers": [
                                {"name": "RCU_APB1RST", "offset": 0x10, "parameters": []}
                            ],
                            "fields": [
                                {
                                    "name": "RCU_APB1RST_CAN2RST",
                                    "register": "RCU_APB1RST",
                                    "bit_offset": 31,
                                    "bit_size": 1,
                                }
                            ],
                        }
                    ],
                    "rcu": {
                        "source": {"path": "rcu.h", "sha256": "abc"},
                        "enable": [],
                        "reset": [{"name": "CAN2", "register_offset": 0x10, "bit": 31}],
                    },
                }
            ],
            "missing_devices": [],
        }
        models = {"devices": [{"id": "GD32TEST1", "cmsis_devices": ["GD32TEST1"]}]}
        resources = {
            "devices": [
                {"device": "GD32TEST1", "debug": [{"file": {"sha256": "svd-sha"}}]}
            ]
        }
        svds = {
            "svds": [
                {"sha256": "svd-sha", "peripheral_names": ["RCU"], "rcu_gates": []}
            ]
        }

        full, report = MODULE.build_outputs(variants, models, resources, svds)

        self.assertEqual(report["devices"][0]["status"], "normalized")
        self.assertEqual(full["devices"][0]["reset"], [])
        self.assertEqual(full["devices"][0]["omitted_gates"][0]["name"], "CAN2")
        self.assertEqual(
            full["devices"][0]["omitted_gates"][0]["resolution"],
            "current-pack-supersedes-firmware",
        )


if __name__ == "__main__":
    unittest.main()
