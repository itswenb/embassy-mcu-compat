import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "normalize_gigadevice_models.py"
SPEC = importlib.util.spec_from_file_location("normalize_gigadevice_models", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class NormalizeTests(unittest.TestCase):
    def test_从普通与车规订货号提取device(self):
        self.assertEqual(MODULE.device_from_part("GD32E235E8P6TR"), "GD32E235E8")
        self.assertEqual(MODULE.device_from_part("GD32VW553HIQ7"), "GD32VW553HI")
        self.assertEqual(MODULE.device_from_part("GD32A714AXJ3TA"), "GD32A714AX")
        self.assertEqual(MODULE.device_from_part("GD32H75EYMJ6B"), "GD32H75EYM")
        self.assertEqual(MODULE.device_from_part("GD32L235K8Q6P"), "GD32L235K8")
        self.assertEqual(MODULE.device_from_part("GD32A503CBT30E"), "GD32A503CB")
        self.assertEqual(MODULE.device_from_part("GD32A503CBT31E"), "GD32A503CB")
        self.assertIsNone(MODULE.device_from_part("GD32F103C8"))
        self.assertIsNone(MODULE.device_from_part("GD32VF103"))

    def test_补充订货号与系列全部归类(self):
        result = MODULE.normalize_models(
            tokens={"GD32F103C8", "GD32F103C8T6", "GD32VF103", "GD32VF103C8T6"},
            records=[
                {
                    "device": "GD32F103C8",
                    "core": "Cortex-M3",
                    "rust_target": "thumbv7m-none-eabi",
                    "source_pack_name": "GD32F10x_DFP",
                    "source_pack_version": "2.0.3",
                }
            ],
        )

        self.assertEqual(result["summary"]["catalog_entries"], 4)
        self.assertEqual(result["summary"]["unresolved_catalog_entries"], 0)
        devices = {device["id"]: device for device in result["devices"]}
        self.assertIn("GD32VF103C8", devices)
        self.assertEqual(devices["GD32F103C8"]["cmsis_devices"], ["GD32F103C8"])

    def test_cmsis中使用完整订货号时仍归一到device(self):
        result = MODULE.normalize_models(
            tokens={"GD32W515", "GD32W515PIQ6", "GD32FFPR"},
            records=[
                {
                    "device": "GD32W515PIQ6",
                    "core": "Cortex-M33",
                    "rust_target": "thumbv8m.main-none-eabihf",
                    "source_pack_name": "GD32W51x_F5HC_DFP",
                    "source_pack_version": "2.0.0",
                }
            ],
        )

        devices = {device["id"]: device for device in result["devices"]}
        entries = {entry["id"]: entry for entry in result["catalog_entries"]}
        self.assertIn("GD32W515PI", devices)
        self.assertEqual(devices["GD32W515PI"]["cmsis_devices"], ["GD32W515PIQ6"])
        self.assertEqual(entries["GD32W515PIQ6"]["kind"], "part_number")
        self.assertEqual(entries["GD32FFPR"]["kind"], "catalog_only")

    def test_cmsis车规别名在目录未列出时仍合并到同一device(self):
        result = MODULE.normalize_models(
            tokens={"GD32A503", "GD32A503CBT3"},
            records=[
                {
                    "device": device,
                    "core": "Cortex-M33",
                    "rust_target": "thumbv8m.main-none-eabihf",
                    "source_pack_name": "GD32A50x_DFP",
                    "source_pack_version": "1.0.0",
                }
                for device in ["GD32A503CB", "GD32A503CBT30E", "GD32A503CBT31E"]
            ],
        )

        self.assertEqual([device["id"] for device in result["devices"]], ["GD32A503CB"])
        self.assertEqual(
            result["devices"][0]["cmsis_devices"],
            ["GD32A503CB", "GD32A503CBT30E", "GD32A503CBT31E"],
        )

    def test_Builder矩阵补齐公开清单缺失的device(self):
        supplemental = MODULE.builder_supplemental_models(
            [
                {"model_pattern": "C211EXP6TR", "generic_family": False, "path": "c211.xml"},
                {"model_pattern": "E231C4T", "generic_family": False, "path": "e231.xml"},
                {"model_pattern": "H77DIXK7", "generic_family": False, "path": "h77.xml"},
                {"model_pattern": "F10X", "generic_family": True, "path": "family.xml"},
            ],
            set(),
        )
        result = MODULE.normalize_models(set(supplemental), [], supplemental)

        self.assertEqual(
            {device["id"] for device in result["devices"]},
            {"GD32C211EX", "GD32E231C4", "GD32H77DIX"},
        )
        entries = {entry["id"]: entry for entry in result["catalog_entries"]}
        self.assertEqual(entries["GD32C211EXP6TR"]["kind"], "part_pattern")
        self.assertEqual(entries["GD32E231C4T"]["kind"], "package_pattern")
        self.assertEqual(result["summary"]["builder_supplemental_devices"], 3)

    def test_Builder固件内核补齐新型号Rust目标(self):
        models = {
            "devices": [
                {"id": "GD32C211EX", "source": "embedded-builder", "core": None, "rust_target": None},
                {"id": "GD32H77DIX", "source": "embedded-builder", "core": None, "rust_target": None},
                {"id": "GD32E235C4", "source": "selection-guide", "core": None, "rust_target": None},
                {"id": "GD32VF103C4", "source": "selection-guide", "core": None, "rust_target": None},
            ]
        }
        firmware = {
            "plugins": [
                {"series": "gd32c2x1", "core": "Cortex-M23", "rust_target": "thumbv8m.base-none-eabi"},
                {"series": "gd32h77x_78x", "core": "Cortex-M7", "rust_target": "thumbv7em-none-eabihf"},
                {"series": "gd32e23x", "core": "Cortex-M23", "rust_target": "thumbv8m.base-none-eabi"},
                {"series": "gd32vf103", "core": None, "rust_target": None},
            ]
        }

        MODULE.apply_builder_targets(models, firmware)

        self.assertEqual(models["devices"][0]["core"], "Cortex-M23")
        self.assertEqual(models["devices"][1]["rust_target"], "thumbv7em-none-eabihf")
        self.assertEqual(models["devices"][2]["rust_target"], "thumbv8m.base-none-eabi")
        self.assertIsNone(models["devices"][3]["rust_target"])

    def test_Riscv固件补齐VF与VW目标(self):
        models = {
            "devices": [
                {"id": "GD32VF103C8", "core": None, "rust_target": None},
                {"id": "GD32VW553HI", "core": None, "rust_target": None},
                {"id": "GD32A711BR", "core": None, "rust_target": None},
            ]
        }
        riscv = {
            "libraries": [
                {
                    "series": "GD32VF103",
                    "isa": "RV32IMAC",
                    "rust_target": "riscv32imac-unknown-none-elf",
                },
                {
                    "series": "GD32VW55x",
                    "isa": "RV32IMAFC",
                    "rust_target": "riscv32imafc-unknown-none-elf",
                },
            ]
        }

        MODULE.apply_riscv_targets(models, riscv)

        self.assertEqual(models["devices"][0]["core"], "RV32IMAC")
        self.assertEqual(
            models["devices"][1]["rust_target"],
            "riscv32imafc-unknown-none-elf",
        )
        self.assertIsNone(models["devices"][2]["rust_target"])

    def test_IAR设备支持包补齐A7目标(self):
        models = {"devices": [{"id": "GD32A711AR", "core": None, "rust_target": None}]}
        iar = {
            "devices": [
                {
                    "id": "GD32A711AR",
                    "core": "Cortex-M7",
                    "rust_target": "thumbv7em-none-eabihf",
                }
            ]
        }

        MODULE.apply_iar_targets(models, iar)

        self.assertEqual(models["devices"][0]["core"], "Cortex-M7")
        self.assertEqual(models["devices"][0]["rust_target"], "thumbv7em-none-eabihf")

    def test_Programmer完整料号替代Builder容量通配型号(self):
        parts = {"GD32H77DIIK7", "GD32H77DIPK7", "GD32H77DIWK7"}
        supplemental = MODULE.builder_supplemental_models(
            [
                {
                    "model_pattern": "H77DIXK7",
                    "generic_family": False,
                    "path": "h77.xml",
                }
            ],
            parts,
            authoritative_parts=parts,
        )
        result = MODULE.normalize_models(
            parts,
            [],
            supplemental,
            external_device_sources={
                "GD32H77DII": "programmer",
                "GD32H77DIP": "programmer",
                "GD32H77DIW": "programmer",
            },
        )

        self.assertEqual(supplemental, {})
        self.assertEqual(
            {device["id"] for device in result["devices"]},
            {"GD32H77DII", "GD32H77DIP", "GD32H77DIW"},
        )
        self.assertTrue(all(device["source"] == "programmer" for device in result["devices"]))

    def test_Programmer新型号有Builder固件才补目标(self):
        models = {
            "devices": [
                {"id": "GD32H77DII", "source": "programmer", "core": None, "rust_target": None},
                {"id": "GD32A711BR", "source": "programmer", "core": None, "rust_target": None},
            ]
        }
        firmware = {
            "plugins": [
                {"series": "gd32h77x_78x", "core": "Cortex-M7", "rust_target": "thumbv7em-none-eabihf"}
            ]
        }

        MODULE.apply_builder_targets(models, firmware)

        self.assertEqual(models["devices"][0]["core"], "Cortex-M7")
        self.assertIsNone(models["devices"][1]["core"])

    def test_产品选择器补充旧选型手册缺失的A7型号(self):
        parts = MODULE.product_part_numbers(
            {
                "products": [
                    {"part_number": "GD32A714AIT3TB"},
                    {"part_number": "GD32A714BIT3TB"},
                ]
            }
        )
        result = MODULE.normalize_models(
            {"GD32A714AIT3TA"} | parts,
            [],
            external_device_sources={"GD32A714BI": "product-selector"},
        )

        devices = {row["id"]: row for row in result["devices"]}
        self.assertEqual(parts, {"GD32A714AIT3TB", "GD32A714BIT3TB"})
        self.assertEqual(devices["GD32A714BI"]["source"], "product-selector")
        self.assertEqual(
            devices["GD32A714AI"]["part_numbers"],
            ["GD32A714AIT3TA", "GD32A714AIT3TB"],
        )


if __name__ == "__main__":
    unittest.main()
