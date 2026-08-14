import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "build_gigadevice_firmware_ir.py"
SPEC = importlib.util.spec_from_file_location("build_gigadevice_firmware_ir", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def header(path, address, offset=0):
    return {
        "path": path,
        "sha256": path * 64,
        "instances": {"USART0": address},
        "instance_blocks": {"USART0": "USART"},
        "register_blocks": {"USART_STAT": "USART"},
        "registers": [
            {
                "name": "USART_STAT",
                "offset": offset,
                "parameters": ["usartx"],
                "width": 32,
            }
        ],
        "fields": [
            {
                "name": "USART_STAT_PERR",
                "register": "USART_STAT",
                "bit_offset": 0,
                "bit_size": 1,
            }
        ],
        "unresolved_registers": [],
        "unassigned_instances": [],
        "unassigned_registers": [],
        "invalid_fields": [],
    }


class FirmwareIrTests(unittest.TestCase):
    def test_官方范围与逐项别名共同校正数组地址(self):
        registers = [
            {
                "name": "EXMC_SDCTL",
                "offset": 0x130,
                "width": 32,
                "array_parameters": {
                    "device": {
                        "start": 0,
                        "end": 1,
                        "stride": 4,
                        "bound_evidence": "source-comment-range",
                    }
                },
            },
            {"name": "EXMC_SDCTL0", "offset": 0x140, "width": 32},
            {"name": "EXMC_SDCTL1", "offset": 0x144, "width": 32},
        ]

        result = MODULE.infer_array_bounds(registers)

        self.assertEqual(result[0]["offset"], 0x140)
        self.assertEqual(
            result[0]["array_parameters"]["device"]["address_evidence"],
            "scalar-register-sequence",
        )
    def test_用同族标量寄存器序列恢复数组上界(self):
        registers = [
            {
                "name": "DMA_CHCTL",
                "offset": 8,
                "width": 32,
                "array_parameters": {"channel": {"start": 0, "stride": 20}},
            },
            {"name": "DMA_CH0CTL", "offset": 8, "width": 32},
            {"name": "DMA_CH1CTL", "offset": 28, "width": 32},
            {"name": "DMA_CH2CTL", "offset": 48, "width": 32},
            {"name": "DMA_CH99CTL", "offset": 0x400, "width": 32},
            {"name": "UNRELATED", "offset": 68, "width": 32},
        ]

        result = MODULE.infer_array_bounds(registers)

        self.assertEqual(
            result[0]["array_parameters"]["channel"],
            {
                "start": 0,
                "end": 2,
                "stride": 20,
                "bound_evidence": "scalar-register-sequence",
            },
        )

    def test_用同族标量寄存器序列恢复非零起点(self):
        registers = [
            {
                "name": "EXMC_NPCTL",
                "offset": 0x40,
                "width": 32,
                "array_parameters": {"bank": {"start": 0, "stride": 0x20}},
            },
            {"name": "EXMC_NPCTL1", "offset": 0x60, "width": 32},
            {"name": "EXMC_NPCTL2", "offset": 0x80, "width": 32},
            {"name": "EXMC_NPCTL3", "offset": 0xA0, "width": 32},
        ]

        result = MODULE.infer_array_bounds(registers)

        self.assertEqual(result[0]["offset"], 0x60)
        self.assertEqual(
            result[0]["array_parameters"]["bank"],
            {
                "start": 1,
                "end": 3,
                "stride": 0x20,
                "bound_evidence": "scalar-register-sequence",
            },
        )

    def test_用选项字节标量别名恢复通用字数组范围(self):
        registers = [
            {
                "name": "OP_BYTE",
                "offset": 0,
                "width": 32,
                "array_parameters": {"x": {"start": 0, "stride": 4}},
            },
            {"name": "OB_SPC_USER", "offset": 0, "width": 32},
            {"name": "OB_DATA", "offset": 4, "width": 32},
            {"name": "OB_WP0", "offset": 8, "width": 32},
            {"name": "OB_WP1", "offset": 12, "width": 32},
        ]

        result = MODULE.infer_array_bounds(registers)

        self.assertEqual(
            result[0]["array_parameters"]["x"],
            {
                "start": 0,
                "end": 3,
                "stride": 4,
                "bound_evidence": "scalar-block-sequence",
            },
        )

    def test_按实例证据专用化共享寄存器布局(self):
        source = {"path": "Firmware/Source/gd32_tzpcu.c", "sha256": "f" * 64}
        library = {
            "series": "GD32TEST",
            "archive_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "device_header_sha256": "c" * 64,
            "register_headers": [
                {
                    "path": "d",
                    "sha256": "d" * 64,
                    "instances": {
                        "TZBMPC0": 0x40000000,
                        "TZBMPC1": 0x40000400,
                        "TZBMPC2": 0x40000800,
                    },
                    "instance_blocks": {
                        "TZBMPC0": "TZBMPC",
                        "TZBMPC1": "TZBMPC",
                        "TZBMPC2": "TZBMPC",
                    },
                    "register_blocks": {"TZPCU_TZBMPC_VEC": "TZBMPC"},
                    "registers": [
                        {
                            "name": "TZPCU_TZBMPC_VEC",
                            "offset": 0x100,
                            "width": 32,
                            "array_parameters": {"y": {"start": 0, "stride": 4}},
                        }
                    ],
                    "fields": [],
                    "unresolved_registers": [],
                    "unassigned_instances": [],
                    "unassigned_registers": [],
                    "invalid_fields": [],
                }
            ],
            "instance_array_bounds": [
                {
                    "instance": "TZBMPC0",
                    "register": "TZPCU_TZBMPC_VEC",
                    "parameter": "y",
                    "end": 7,
                    "bound_evidence": "source-block-position-range",
                    "source": source,
                },
                {
                    "instance": "TZBMPC1",
                    "register": "TZPCU_TZBMPC_VEC",
                    "parameter": "y",
                    "end": 7,
                    "bound_evidence": "source-block-position-range",
                    "source": source,
                },
                {
                    "instance": "TZBMPC2",
                    "register": "TZPCU_TZBMPC_VEC",
                    "parameter": "y",
                    "end": 15,
                    "bound_evidence": "source-block-position-range",
                    "source": source,
                },
            ],
        }

        result = MODULE.build_library_ir(library)
        layouts = {row["id"]: row for row in result["layouts"]}
        ends = {
            row["name"]: layouts[row["layout"]]["registers"][0]["array_parameters"]["y"]["end"]
            for row in result["instances"]
        }

        self.assertEqual(ends, {"TZBMPC0": 7, "TZBMPC1": 7, "TZBMPC2": 15})
        self.assertEqual(len(layouts), 2)
        self.assertTrue(all(source in row["sources"] for row in layouts.values()))

    def test_相同布局去重且保留实例来源(self):
        library = {
            "series": "GD32TEST",
            "archive_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "device_header_sha256": "c" * 64,
            "register_headers": [header("d", 0x40013800), header("e", 0x40013800)],
        }

        result = MODULE.build_library_ir(library)

        self.assertEqual(len(result["layouts"]), 1)
        self.assertEqual(result["instances"][0]["name"], "USART0")
        self.assertEqual(result["instances"][0]["address"], 0x40013800)
        self.assertEqual(len(result["instances"][0]["sources"]), 2)
        self.assertEqual(result["instance_layout_conflicts"], [])

    def test_同一实例的互补扩展头合并为一个布局(self):
        base = header("d", 0x40013800)
        extension = header("e", 0x40013800)
        extension["register_blocks"] = {"USART_CTL": "USART"}
        extension["registers"] = [
            {
                "name": "USART_CTL",
                "offset": 4,
                "parameters": ["usartx"],
                "width": 32,
            }
        ]
        extension["fields"] = []
        library = {
            "series": "GD32TEST",
            "archive_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "device_header_sha256": "c" * 64,
            "register_headers": [base, extension],
        }

        result = MODULE.build_library_ir(library)

        self.assertEqual(result["instance_layout_conflicts"], [])
        self.assertEqual(len(result["layouts"]), 1)
        self.assertEqual(len(result["layouts"][0]["registers"]), 2)

    def test_同一实例的不同布局必须报告冲突(self):
        library = {
            "series": "GD32TEST",
            "archive_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "device_header_sha256": "c" * 64,
            "register_headers": [header("d", 0x40013800), header("e", 0x40013800, 4)],
        }

        result = MODULE.build_library_ir(library)

        self.assertEqual(len(result["instance_layout_conflicts"]), 1)
        self.assertEqual(result["instance_layout_conflicts"][0]["name"], "USART0")


if __name__ == "__main__":
    unittest.main()
