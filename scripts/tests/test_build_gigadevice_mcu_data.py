import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "build_gigadevice_mcu_data.py"
SPEC = importlib.util.spec_from_file_location("build_gigadevice_mcu_data", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class McuDataTests(unittest.TestCase):
    def test_IAR_Flash几何和算法进入统一内存状态(self):
        row = MODULE.iar_memory_data(
            {
                "id": "GD32A711AR",
                "configuration": "arm/config/devices/GD32A711AR.i79",
                "linker": {
                    "path": "arm/config/linker/GD32A711x.icf",
                    "memory": [
                        {"name": "IROM1", "kind": "flash", "start": 0x08000000, "size": 0x100000},
                        {"name": "IRAM1", "kind": "ram", "start": 0x24000000, "size": 0x20000},
                    ],
                },
                "flash": {
                    "path": "arm/config/flashloader/GD/FlashGD32A711x.board",
                    "regions": [
                        {
                            "start": 0x08000000,
                            "size": 0x100000,
                            "write_size": 8,
                            "erase_size": 0x800,
                            "algorithm": {"sha256": "a" * 64},
                        }
                    ]
                },
            }
        )

        self.assertEqual(row["memory_status"], "normalized")
        self.assertEqual(row["flash_status"], "normalized")
        self.assertEqual(row["memory_regions"], 2)
        self.assertEqual(row["algorithms"], ["a" * 64])

    def test_固件系列模式支持组合系列且不扩大到H76(self):
        self.assertTrue(MODULE.series_matches_device("GD32H73x_75x", "GD32H757ZI"))
        self.assertFalse(MODULE.series_matches_device("GD32H73x_75x", "GD32H767ZI"))
        self.assertTrue(MODULE.series_matches_device("GD32VF103", "GD32VF103C8"))

    def test_存在Pack提示时选择专用固件否则选择通用固件(self):
        available = {"GD32W51x", "GD32W51x_F5HC"}

        self.assertEqual(
            MODULE.choose_firmware_series(
                "GD32W515PI", [{"name": "GD32W51x_F5HC_DFP"}], available
            ),
            "GD32W51x_F5HC",
        )
        self.assertEqual(
            MODULE.choose_firmware_series("GD32W515PI", [], available),
            "GD32W51x",
        )

    def test_同名Pack支持不规则型号但显式系列别名仍校验范围(self):
        available = {"GD32F3x0", "GD32H73x_75x"}

        self.assertEqual(
            MODULE.choose_firmware_series(
                "GD32F355C8", [{"name": "GD32F3x0_DFP"}], available
            ),
            "GD32F3x0",
        )
        self.assertIsNone(
            MODULE.choose_firmware_series(
                "GD32H767IM", [{"name": "GD32H7xx_DFP"}], available
            )
        )

    def test_系列匹配忽略规范型号中的占位符大小写(self):
        self.assertTrue(MODULE.series_matches_device("GD32C10x", "GD32C103CBxxA"))

    def test_来源冲突阻止状态提升但保留PAC编译证据(self):
        model = {
            "id": "GD32TESTC8",
            "feature": "gd32testc8",
            "support_state": "catalogued",
            "core": "Cortex-M3",
            "rust_target": "thumbv7m-none-eabi",
            "cmsis_devices": ["GD32TESTC8"],
            "source_packs": [{"name": "GD32TEST_DFP", "version": "1.0.0"}],
            "part_numbers": ["GD32TESTC8T6"],
        }
        resource = {
            "device": "GD32TESTC8",
            "memory": [{"id": "IROM1"}],
            "algorithm": [{"file": {"sha256": "f" * 64}}],
            "debug": [{"file": {"path": "test.svd", "sha256": "a" * 64}}],
        }
        comparison = {
            "svd_sha256": "a" * 64,
            "conflict_status": "known-blocking",
            "path": "test.svd",
        }
        pac = {"svd_sha256": "a" * 64, "status": "cached"}
        builder = {"id": "GD32TESTC8", "evidence": "pattern-verified", "matrix_paths": []}
        firmware = {"series": "GD32TEST", "device_headers": [{"sha256": "b" * 64}]}

        row = MODULE.build_device_row(
            model,
            [resource],
            builder,
            firmware,
            {"series": "GD32TEST", "register_headers": []},
            {"a" * 64: comparison},
            {"a" * 64: pac},
        )

        self.assertEqual(row["support_state"], "catalogued")
        self.assertEqual(row["facts"]["interrupts"], "conflict")
        self.assertEqual(row["artifacts"]["pac"], "compiled")
        self.assertIn("interrupt-source-conflict", row["blockers"])

    def test_没有SVD时宽松许可Firmware仍提供寄存器事实(self):
        model = {
            "id": "GD32VF103C8",
            "feature": "gd32vf103c8",
            "core": None,
            "rust_target": None,
            "cmsis_devices": [],
            "source_packs": [],
            "part_numbers": [],
        }
        builder = {"id": "GD32VF103C8", "evidence": "pattern-verified", "matrix_paths": []}
        firmware = {"series": "GD32VF103", "device_headers": [{"sha256": "b" * 64}]}
        registers = {
            "series": "GD32VF103",
            "register_headers": [
                {
                    "instances": {"RCU": 0x40021000},
                    "registers": [{"name": "RCU_CTL"}],
                    "invalid_fields": [],
                }
            ],
        }

        row = MODULE.build_device_row(
            model, [], builder, firmware, registers, {}, {}
        )

        self.assertEqual(row["facts"]["registers"], "firmware-indexed")
        self.assertEqual(row["facts"]["flash"], "firmware-candidate")
        self.assertEqual(row["artifacts"]["firmware_registers"]["instances"], 1)
        self.assertNotIn("register-source-missing", row["blockers"])
        self.assertIn("flash-not-normalized", row["blockers"])

    def test_无SVD但完整Firmware_PAC通过时提升为pac_generated(self):
        model = {
            "id": "GD32TESTC8",
            "feature": "gd32testc8",
            "core": "Cortex-M3",
            "rust_target": "thumbv7m-none-eabi",
            "cmsis_devices": [],
            "source_packs": [],
            "part_numbers": [],
        }
        variant = {
            "id": "gd32test",
            "series": "GD32TEST",
            "defines": ["GD32TEST"],
            "instances": [{"name": "RCU", "address": 0x40021000}],
            "layouts": [{"registers": [{"name": "RCU_CTL"}], "fields": []}],
            "source_issues": [],
        }

        row = MODULE.build_device_row(
            model,
            [],
            {"id": "GD32TESTC8", "evidence": "builder-model", "matrix_paths": []},
            {"series": "GD32TEST"},
            {"series": "GD32TEST", "register_headers": []},
            {},
            {},
            variant=variant,
            firmware_pac={
                "id": "gd32test",
                "compile_status": "compiled",
                "unbounded_array_registers": 0,
            },
            pins={"id": "GD32TESTC8", "status": "normalized"},
            memory={
                "id": "GD32TESTC8",
                "memory_status": "normalized",
                "flash_status": "normalized",
            },
            rcu={
                "id": "GD32TESTC8",
                "variant": "gd32test",
                "gate_status": "normalized",
                "binding_status": "normalized",
            },
            dma={"id": "GD32TESTC8", "variant": "gd32test", "status": "normalized"},
        )

        self.assertEqual(row["blockers"], [])
        self.assertEqual(row["artifacts"]["firmware_pac"], "compiled")
        self.assertEqual(row["support_state"], "pac_generated")

    def test_设备级Firmware变体的已知问题只阻断对应设备(self):
        model = {
            "id": "GD32L235C8",
            "feature": "gd32l235c8",
            "core": "Cortex-M23",
            "rust_target": "thumbv8m.base-none-eabi",
            "cmsis_devices": ["GD32L235C8"],
            "source_packs": [{"name": "GD32L23x_DFP", "version": "1.0.0"}],
            "part_numbers": [],
        }
        firmware = {"series": "GD32L23x", "device_headers": [{"sha256": "b" * 64}]}
        registers = {"series": "GD32L23x", "register_headers": []}
        variant = {
            "id": "gd32l23x-test",
            "series": "GD32L23x",
            "defines": ["GD32L235", "GD32L23x"],
            "instances": [{"name": "RCU", "address": 0x40021000}],
            "layouts": [
                {
                    "registers": [{"name": "RCU_CFG2"}],
                    "fields": [],
                }
            ],
            "source_issues": [{"conflict_status": "known-blocking"}],
        }

        row = MODULE.build_device_row(
            model,
            [],
            {"id": "GD32L235C8", "evidence": "none", "matrix_paths": []},
            firmware,
            registers,
            {},
            {},
            variant=variant,
            firmware_pac={
                "id": "gd32l23x-test",
                "compile_status": "compiled",
                "unbounded_array_registers": 2,
            },
        )

        self.assertEqual(row["facts"]["registers"], "conflict")
        self.assertIn("register-source-conflict", row["blockers"])
        self.assertEqual(row["sources"]["firmware_variant"], "gd32l23x-test")
        self.assertEqual(row["artifacts"]["firmware_pac"], "compiled")
        self.assertIn("register-array-bounds-not-normalized", row["blockers"])

    def test_builder归一引脚消除归一阻塞并保留统计(self):
        model = {
            "id": "GD32F103C8",
            "feature": "gd32f103c8",
            "core": "Cortex-M3",
            "rust_target": "thumbv7m-none-eabi",
            "cmsis_devices": [],
            "source_packs": [],
            "part_numbers": [],
        }
        row = MODULE.build_device_row(
            model,
            [],
            {"id": "GD32F103C8", "evidence": "builder-model", "matrix_paths": ["f103.xml"]},
            None,
            None,
            {},
            {},
            pins={
                "id": "GD32F103C8",
                "status": "normalized",
                "matrix_paths": ["f103.xml"],
                "afio_paths": ["f10x.xml"],
                "gpio_pins": 37,
                "functions": 120,
                "packages": ["LQFP48"],
                "afio_routes": 20,
            },
        )

        self.assertEqual(row["facts"]["pins"], "normalized")
        self.assertNotIn("pins-not-normalized", row["blockers"])
        self.assertNotIn("pin-source-missing", row["blockers"])
        self.assertEqual(row["artifacts"]["pins"]["gpio_pins"], 37)
        self.assertEqual(row["sources"]["builder_afio"], ["f10x.xml"])

    def test_引脚来源冲突会阻断发布(self):
        model = {
            "id": "GD32F103C8",
            "feature": "gd32f103c8",
            "core": "Cortex-M3",
            "rust_target": "thumbv7m-none-eabi",
            "cmsis_devices": [],
            "source_packs": [],
            "part_numbers": [],
        }
        row = MODULE.build_device_row(
            model,
            [],
            {"id": "GD32F103C8", "evidence": "none", "matrix_paths": []},
            None,
            None,
            {},
            {},
            pins={"id": "GD32F103C8", "status": "conflict"},
        )

        self.assertEqual(row["facts"]["pins"], "conflict")
        self.assertIn("pin-source-conflict", row["blockers"])

    def test_pack内存与flash归一状态进入发布门(self):
        model = {
            "id": "GD32F103C8",
            "feature": "gd32f103c8",
            "core": "Cortex-M3",
            "rust_target": "thumbv7m-none-eabi",
            "cmsis_devices": [],
            "source_packs": [],
            "part_numbers": [],
        }
        row = MODULE.build_device_row(
            model,
            [],
            {"id": "GD32F103C8", "evidence": "none", "matrix_paths": []},
            None,
            None,
            {},
            {},
            memory={
                "id": "GD32F103C8",
                "memory_status": "normalized",
                "flash_status": "conflict",
                "profiles": ["GD32F103C8"],
                "memory_regions": 2,
                "flash_regions": 1,
                "algorithms": ["a" * 64],
            },
        )

        self.assertEqual(row["facts"]["memory"], "normalized")
        self.assertEqual(row["facts"]["flash"], "conflict")
        self.assertNotIn("memory-source-missing", row["blockers"])
        self.assertIn("flash-source-conflict", row["blockers"])
        self.assertEqual(row["artifacts"]["memory"]["regions"], 2)

    def test_RCU门控表已归一但实例未绑定时继续阻塞发布(self):
        model = {
            "id": "GD32F103C8",
            "feature": "gd32f103c8",
            "core": "Cortex-M3",
            "rust_target": "thumbv7m-none-eabi",
            "cmsis_devices": [],
            "source_packs": [],
            "part_numbers": [],
        }
        row = MODULE.build_device_row(
            model,
            [],
            {"id": "GD32F103C8", "evidence": "none", "matrix_paths": []},
            {"series": "GD32F10x"},
            {"series": "GD32F10x", "register_headers": []},
            {},
            {},
            rcu={
                "id": "GD32F103C8",
                "variant": "gd32f10x-test",
                "status": "conflict",
                "gate_status": "normalized",
                "binding_status": "conflict",
            },
        )

        self.assertEqual(row["facts"]["rcc"], "gate-table-normalized")
        self.assertIn("rcc-not-normalized", row["blockers"])
        self.assertEqual(row["sources"]["rcu_variant"], "gd32f10x-test")
        self.assertEqual(row["artifacts"]["rcc"]["binding_status"], "conflict")

    def test_DMA归一状态进入发布门并保留通道统计(self):
        model = {
            "id": "GD32H757ZI",
            "feature": "gd32h757zi",
            "core": "Cortex-M7",
            "rust_target": "thumbv7em-none-eabihf",
            "cmsis_devices": [],
            "source_packs": [],
            "part_numbers": [],
        }
        row = MODULE.build_device_row(
            model,
            [],
            {"id": "GD32H757ZI", "evidence": "none", "matrix_paths": []},
            {"series": "GD32H73x_75x"},
            {"series": "GD32H73x_75x", "register_headers": []},
            {},
            {},
            dma={
                "id": "GD32H757ZI",
                "variant": "gd32h73x-test",
                "status": "normalized",
                "kind": "dmamux",
                "channels": 16,
                "requests": 50,
                "mdma_channels": 16,
                "mdma_requests": 28,
            },
        )

        self.assertEqual(row["facts"]["dma"], "normalized")
        self.assertNotIn("dma-not-normalized", row["blockers"])
        self.assertNotIn("dma-source-missing", row["blockers"])
        self.assertEqual(row["artifacts"]["dma"]["mdma_channels"], 16)
        self.assertEqual(row["sources"]["dma_variant"], "gd32h73x-test")

    def test_报告只按设备级变体关联Firmware(self):
        def model(device):
            return {
                "id": device,
                "feature": device.lower(),
                "core": "Cortex-M3",
                "rust_target": "thumbv7m-none-eabi",
                "cmsis_devices": [],
                "source_packs": [],
                "part_numbers": [],
            }

        report = MODULE.build_report(
            {
                "models": {
                    "summary": {"normalized_devices": 2},
                    "devices": [model("GD32T101A"), model("GD32T101B")],
                    "catalog_entries": [],
                },
                "resources": {"devices": []},
                "builders": {
                    "devices": [
                        {"id": "GD32T101A", "evidence": "none"},
                        {"id": "GD32T101B", "evidence": "none"},
                    ]
                },
                "pins": {
                    "summary": {
                        "normalized_devices": 2,
                        "devices_with_normalized_pins": 0,
                    },
                    "devices": [
                        {"id": "GD32T101A", "status": "missing"},
                        {"id": "GD32T101B", "status": "missing"},
                    ],
                },
                "memory": {
                    "summary": {
                        "normalized_devices": 2,
                        "devices_with_normalized_memory": 0,
                        "devices_with_normalized_flash": 0,
                        "devices_with_flash_source_conflict": 0,
                    },
                    "devices": [
                        {
                            "id": "GD32T101A",
                            "memory_status": "missing",
                            "flash_status": "missing",
                        },
                        {
                            "id": "GD32T101B",
                            "memory_status": "missing",
                            "flash_status": "missing",
                        },
                    ],
                },
                "rcu": {
                    "summary": {
                        "normalized_devices": 2,
                        "devices_with_normalized_gate_table": 1,
                        "devices_with_gate_table_conflict": 0,
                        "devices_with_normalized_rcu": 0,
                    },
                    "devices": [
                        {
                            "id": "GD32T101A",
                            "variant": "gd32t101a-test",
                            "status": "conflict",
                            "gate_status": "normalized",
                            "binding_status": "conflict",
                        },
                        {
                            "id": "GD32T101B",
                            "variant": None,
                            "status": "missing",
                            "gate_status": "missing",
                            "binding_status": "missing",
                        },
                    ],
                },
                "dma": {
                    "summary": {
                        "normalized_devices": 2,
                        "devices_with_normalized_dma": 0,
                        "devices_with_dma_conflict": 0,
                        "devices_with_fixed_request_map_missing": 1,
                        "devices_without_dma_source": 1,
                    },
                    "devices": [
                        {
                            "id": "GD32T101A",
                            "variant": "gd32t101a-test",
                            "status": "source-incomplete",
                            "kind": "fixed",
                            "channels": 1,
                            "requests": 0,
                            "mdma_channels": 0,
                            "mdma_requests": 0,
                        },
                        {
                            "id": "GD32T101B",
                            "variant": None,
                            "status": "missing",
                            "kind": None,
                            "channels": 0,
                            "requests": 0,
                            "mdma_channels": 0,
                            "mdma_requests": 0,
                        },
                    ],
                },
                "firmware": {"libraries": [{"series": "GD32T10x"}]},
                "registers": {
                    "libraries": [
                        {
                            "series": "GD32T10x",
                            "register_headers": [
                                {"registers": [{"name": "RCU_CTL"}], "instances": {}}
                            ],
                        }
                    ]
                },
                "variants": {
                    "summary": {
                        "normalized_devices": 2,
                        "devices": 1,
                        "missing_devices": 1,
                    },
                    "variants": [
                        {
                            "id": "gd32t101a-test",
                            "series": "GD32T10x",
                            "devices": ["GD32T101A"],
                            "defines": ["GD32T101A"],
                            "instances": [],
                            "layouts": [],
                            "source_issues": [
                                {"conflict_status": "known-blocking"}
                            ],
                        }
                    ],
                },
                "firmware_pacs": {
                    "summary": {"variants": 1, "devices": 1},
                    "pacs": [
                        {
                            "id": "gd32t101a-test",
                            "compile_status": "cached",
                            "unbounded_array_registers": 1,
                        }
                    ],
                },
                "comparisons": {"comparisons": []},
                "pacs": {"pacs": []},
            }
        )

        by_id = {device["id"]: device for device in report["devices"]}
        self.assertEqual(
            by_id["GD32T101A"]["sources"]["firmware_variant"],
            "gd32t101a-test",
        )
        self.assertIsNone(by_id["GD32T101B"]["sources"]["firmware_series"])
        self.assertIn("firmware-source-missing", by_id["GD32T101B"]["blockers"])
        self.assertIn("register-source-conflict", by_id["GD32T101A"]["blockers"])
        self.assertNotIn("register-source-conflict", by_id["GD32T101B"]["blockers"])
        self.assertEqual(report["summary"]["devices_with_register_source_conflict"], 1)
        self.assertEqual(report["summary"]["devices_with_firmware_pac"], 1)
        self.assertEqual(report["summary"]["devices_with_normalized_rcu_gate_table"], 1)
        self.assertEqual(by_id["GD32T101A"]["facts"]["rcc"], "gate-table-normalized")
        self.assertEqual(by_id["GD32T101A"]["artifacts"]["firmware_pac"], "compiled")
        self.assertEqual(by_id["GD32T101B"]["artifacts"]["firmware_pac"], "missing")

    def test_合并变体按来源选择同系列Builder事实(self):
        official = {"libraries": [{"series": "GD32TEST", "marker": "official"}]}
        builder = {"libraries": [{"series": "GD32TEST", "marker": "builder"}]}

        firmware, registers = MODULE.firmware_sources_by_kind(
            official, official, builder, builder
        )

        self.assertEqual(firmware[("builder", "GD32TEST")]["marker"], "builder")
        self.assertEqual(registers[("official", "GD32TEST")]["marker"], "official")


if __name__ == "__main__":
    unittest.main()
