import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "index_gigadevice_firmware_headers.py"
SPEC = importlib.util.spec_from_file_location(
    "index_gigadevice_firmware_headers", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


HEADER = """\
/* SPDX-License-Identifier: Apache-2.0 */
typedef enum IRQn {
    NonMaskableInt_IRQn = -14,
    WWDGT_IRQn = 0,
    RCU_IRQn,
    USART0_IRQn = 37,
#ifdef GD32TEST_FULL
    LPUART1_IRQn = 71
#else
    LPUART_IRQn = 67,
#endif
    USBDWakeUp_IRQChannel = 42,
} IRQn_Type;

#define APB1_BUS_BASE_NS ((uint32_t)0x40000000U)
#define USART_BASE_NS (APB1_BUS_BASE_NS + 0x00004400UL)
#define USART_BASE USART_BASE_NS
#define USART0_BASE USART_BASE
#define USART0 ((volatile void *) USART0_BASE)
"""


class FirmwareHeaderIndexTests(unittest.TestCase):
    def test_无值include_guard不会吞掉下一行宏(self):
        source = "#define GD32TEST_H\n#define SHRTIMER0 (SHRTIMER_BASE + 0x0U)\n"

        self.assertEqual(
            MODULE.DEFINE_RE.findall(source),
            [("SHRTIMER0", "(SHRTIMER_BASE + 0x0U)")],
        )

    def test_解析链式基址和隐式中断号(self):
        facts = MODULE.parse_header_facts(HEADER)

        self.assertEqual(
            facts["base_addresses"],
            {
                "APB1_BUS_BASE_NS": 0x40000000,
                "USART0_BASE": 0x40004400,
                "USART_BASE": 0x40004400,
                "USART_BASE_NS": 0x40004400,
            },
        )
        self.assertEqual(
            facts["interrupts"],
            [
                {"name": "NonMaskableInt", "value": -14},
                {"name": "WWDGT", "value": 0},
                {"name": "RCU", "value": 1},
                {"name": "USART0", "value": 37},
                {"name": "LPUART1", "value": 71},
                {"name": "LPUART", "value": 67},
                {"name": "USBDWakeUp", "value": 42},
            ],
        )

    def test_只接受同时包含中断和外设基址的中心头文件(self):
        self.assertTrue(MODULE.is_device_header(HEADER))
        self.assertFalse(MODULE.is_device_header("#define USART0_BASE 0x40004400U\n"))

    def test_索引校验解包标记并去重相同头文件(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "GD32TEST_Firmware_Library_V1.0.0"
            library.mkdir()
            (library / ".source.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "archive": "GD32TEST_Firmware_Library_V1.0.0.7z",
                        "archive_sha256": "a" * 64,
                        "tree_sha256": "b" * 64,
                    }
                ),
                encoding="utf-8",
            )
            first = library / "Firmware" / "one" / "gd32test.h"
            second = library / "Firmware" / "two" / "gd32test.h"
            example = library / "Examples" / "gd32test.h"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            example.parent.mkdir(parents=True)
            first.write_text(HEADER, encoding="utf-8")
            second.write_text(HEADER, encoding="utf-8")
            example.write_text(HEADER.replace("0x40000000U", "0x50000000U"), encoding="utf-8")
            lock = {
                "firmware": [
                    {
                        "filename": "GD32TEST_Firmware_Library_V1.0.0.7z",
                        "name": "GD32TEST Firmware Library",
                        "version": "1.0.0",
                        "document_id": 1,
                        "sha256": "a" * 64,
                    }
                ]
            }

            report = MODULE.build_report(lock, root)

        self.assertEqual(report["summary"]["firmware_libraries"], 1)
        self.assertEqual(report["summary"]["unique_device_headers"], 1)
        self.assertEqual(report["summary"]["libraries_without_device_header"], 0)
        header = report["libraries"][0]["device_headers"][0]
        self.assertEqual(header["license"], "Apache-2.0")
        self.assertEqual(header["duplicate_paths"], ["Firmware/two/gd32test.h"])


if __name__ == "__main__":
    unittest.main()
