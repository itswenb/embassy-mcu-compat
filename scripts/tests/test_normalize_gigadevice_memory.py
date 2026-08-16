import importlib.util
import struct
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "normalize_gigadevice_memory.py"
SPEC = importlib.util.spec_from_file_location("normalize_gigadevice_memory", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class MemoryTests(unittest.TestCase):
    def test_官方FMC接口提取真实最小写入粒度(self):
        self.assertEqual(
            MODULE.program_sizes_from_text(
                """
fmc_state_enum fmc_word_program(uint32_t address, uint32_t data);
fmc_state_enum fmc_halfword_program(uint32_t address, uint16_t data);
fmc_state_enum fmc_fourword_program(uint32_t address, uint64_t low, uint64_t high);
"""
            ),
            [2, 4, 16],
        )

    def test_Programmer分段覆盖FLM并保留每个物理Bank参数(self):
        memory, flash = MODULE.apply_flash_geometry(
            [
                {
                    "name": "IROM1",
                    "kind": "flash",
                    "address": 0x08000000,
                    "size": 1024 * 1024,
                },
                {
                    "name": "IRAM1",
                    "kind": "ram",
                    "address": 0x20000000,
                    "size": 96 * 1024,
                },
            ],
            [
                {
                    "address": 0x08000000,
                    "size": 1024 * 1024,
                    "algorithm_sha256": "a" * 64,
                    "program_page_size": 1024,
                    "erase_value": 0xFF,
                    "sectors": [{"offset": 0, "size": 2048}],
                    "descriptor_conflicts": [],
                    "descriptor_resolutions": [],
                }
            ],
            [
                {
                    "address": 0x08000000,
                    "size": 512 * 1024,
                    "erase_size": 2048,
                    "bank": "0",
                },
                {
                    "address": 0x08080000,
                    "size": 512 * 1024,
                    "erase_size": 4096,
                    "bank": "1",
                },
            ],
            write_size=2,
            source={"path": "GD32F303.xml", "sha256": "b" * 64},
        )

        flash_memory = [region for region in memory if region["kind"] == "flash"]
        self.assertEqual(
            [(region["address"], region["size"], region["settings"]) for region in flash_memory],
            [
                (0x08000000, 512 * 1024, {"erase_size": 2048, "write_size": 2, "erase_value": 0xFF}),
                (0x08080000, 512 * 1024, {"erase_size": 4096, "write_size": 2, "erase_value": 0xFF}),
            ],
        )
        self.assertEqual([region["bank"] for region in flash], ["0", "1"])
        self.assertEqual([region["erase_size"] for region in flash], [2048, 4096])
        self.assertEqual(memory[-1]["size"], 96 * 1024)

    def test_解析Builder链接脚本中的固定RAM区域(self):
        memory = MODULE.parse_linker_ram(
            """MEMORY
{
  CNVM (rx): ORIGIN = $(LD1Origin), LENGTH = $(LD1Length)K
  AXISRAM (xrw): ORIGIN = 0x24000000, LENGTH = 768K
  ITCMRAM (xrw): ORIGIN = 0x00000000, LENGTH = 128K
}"""
        )

        self.assertEqual(
            memory,
            [
                {"name": "ITCMRAM", "kind": "ram", "address": 0, "size": 128 * 1024},
                {
                    "name": "AXISRAM",
                    "kind": "ram",
                    "address": 0x24000000,
                    "size": 768 * 1024,
                },
            ],
        )

    def test_Programmer页数量和总容量可勘误错误结束地址(self):
        profiles = MODULE.build_programmer_profiles(
            {
                "devices": [
                    {
                        "id": "GD32H77DIP",
                        "part_numbers": ["GD32H77DIPK7"],
                        "source": "programmer",
                    }
                ]
            },
            {
                "flash_profiles": [
                    {
                        "pattern": "GD32H77XXPXX",
                        "declared_page_number": 2,
                        "rram_size": 0,
                        "flash_size": 8192,
                        "pages": [
                            {
                                "address": 0x08200000,
                                "count": 2,
                                "page_size": 4096,
                                "size": 8192,
                                "end_address": 0x087FFFFF,
                                "geometry_status": "source-inconsistent",
                                "type": "DFlash",
                                "bank": "",
                            }
                        ],
                        "source": {"path": "GD32H77x.xml", "sha256": "a" * 64},
                    }
                ]
            },
            [{"name": "AXISRAM", "kind": "ram", "address": 0x24000000, "size": 1024}],
            {"path": "gd32h77x_78x_flash.ld", "sha256": "b" * 64},
        )

        self.assertEqual(profiles[0]["device"], "GD32H77DIP")
        self.assertEqual(profiles[0]["flash_status"], "geometry-only")
        self.assertEqual(profiles[0]["flash"][0]["size"], 8192)
        self.assertEqual(
            profiles[0]["flash"][0]["source_resolutions"],
            ["page-count-and-declared-total-supersede-end-address"],
        )

    def test_Riscv链接内存与Programmer擦除几何合并(self):
        profiles = MODULE.build_riscv_profiles(
            {
                "devices": [
                    {
                        "id": "GD32VF103C8",
                        "part_numbers": ["GD32VF103C8T6"],
                        "source": "selection-guide",
                    }
                ]
            },
            {
                "flash_profiles": [
                    {
                        "pattern": "GD32VF103X8XX",
                        "rram_size": 0,
                        "flash_size": 64 * 1024,
                        "pages": [
                            {
                                "address": 0x08000000,
                                "count": 64,
                                "page_size": 1024,
                                "geometry_status": "consistent",
                                "type": "",
                                "bank": "0",
                            }
                        ],
                        "source": {"path": "GD32VF103.xml", "sha256": "a" * 64},
                    }
                ]
            },
            {
                "libraries": [
                    {
                        "series": "GD32VF103",
                        "linker_profiles": [
                            {
                                "pattern": "GD32VF103X8",
                                "memory": [
                                    {"name": "flash", "kind": "flash", "address": 0x08000000, "size": 64 * 1024},
                                    {"name": "ram", "kind": "ram", "address": 0x20000000, "size": 20 * 1024},
                                ],
                                "source": {"path": "GD32VF103x8.lds", "sha256": "b" * 64},
                            }
                        ],
                    }
                ]
            },
        )

        self.assertEqual(profiles[0]["device"], "GD32VF103C8")
        self.assertEqual(profiles[0]["source_kind"], "programmer-and-firmware")
        self.assertEqual(profiles[0]["memory"][0]["size"], 64 * 1024)
        self.assertEqual(profiles[0]["memory"][1]["size"], 20 * 1024)
        self.assertEqual(profiles[0]["flash_status"], "geometry-only")

    def test_解析_cmsis_flashdevice_结构和分段擦除几何(self):
        data = bytearray(184)
        struct.pack_into("<H", data, 0, 0x0101)
        data[2:10] = b"GD32TEST"
        struct.pack_into("<HIIIII", data, 130, 1, 0x08000000, 0x40000, 0x800, 0, 0)
        data[148] = 0xFF
        struct.pack_into("<II", data, 152, 100, 3000)
        struct.pack_into("<II", data, 160, 0x800, 0)
        struct.pack_into("<II", data, 168, 0x1000, 0x20000)
        struct.pack_into("<II", data, 176, 0xFFFFFFFF, 0xFFFFFFFF)

        flash = MODULE.decode_flash_device(bytes(data))

        self.assertEqual(flash["name"], "GD32TEST")
        self.assertEqual(flash["address"], 0x08000000)
        self.assertEqual(flash["page_size"], 0x800)
        self.assertEqual(
            flash["sectors"],
            [
                {"offset": 0, "size": 0x800},
                {"offset": 0x20000, "size": 0x1000},
            ],
        )

    def test_pdsc_内存类型只接受明确的_irom_和_iram(self):
        self.assertEqual(
            MODULE.normalize_memory({"id": "IROM1", "start": 0x08000000, "size": 0x10000}),
            {"name": "IROM1", "kind": "flash", "address": 0x08000000, "size": 0x10000},
        )
        self.assertEqual(
            MODULE.normalize_memory({"id": "IRAM2", "start": 0x20010000, "size": 0x8000}),
            {"name": "IRAM2", "kind": "ram", "address": 0x20010000, "size": 0x8000},
        )
        self.assertEqual(
            MODULE.normalize_memory({"id": "DTCMRAM", "start": 0x20000000, "size": 0x10000})[
                "kind"
            ],
            "ram",
        )
        with self.assertRaisesRegex(ValueError, "未知 PDSC 内存类型"):
            MODULE.normalize_memory({"id": "MEM", "start": 0, "size": 1})

    def test_pdsc与flm容量冲突被记录而不是猜测修复(self):
        region = MODULE._flash_region(
            {"start": 0x08800000, "size": 0x8000},
            {
                "address": 0x08800000,
                "size": 0x4000,
                "page_size": 0x400,
                "empty_value": 0xFF,
                "sectors": [{"offset": 0, "size": 0x400}],
            },
        )

        self.assertEqual(region["size"], 0x8000)
        self.assertEqual(region["descriptor_conflicts"], ["size"])

    def test_设备容量而非算法最大范围参与flm校验(self):
        region = MODULE._flash_region(
            {"start": 0x08000000, "size": 0x80000},
            {
                "address": 0x08000000,
                "size": 0x40000,
                "name": "GD32TEST_256K",
                "page_size": 0x400,
                "empty_value": 0xFF,
                "sectors": [{"offset": 0, "size": 0x400}],
            },
            {"address": 0x08000000, "size": 0x40000},
        )

        self.assertEqual(region["descriptor_conflicts"], [])

    def test_flm名称与pdsc一致时记录结构体容量勘误(self):
        region = MODULE._flash_region(
            {"start": 0x08800000, "size": 0x8000},
            {
                "address": 0x08800000,
                "size": 0x4000,
                "name": "GD32TEST_DF_32KB",
                "page_size": 0x400,
                "empty_value": 0xFF,
                "sectors": [{"offset": 0, "size": 0x400}],
            },
            {"address": 0x08800000, "size": 0x8000},
        )

        self.assertEqual(region["descriptor_conflicts"], [])
        self.assertEqual(
            region["descriptor_resolutions"],
            ["flm-name-and-pdsc-supersede-embedded-size"],
        )


if __name__ == "__main__":
    unittest.main()
