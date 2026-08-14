import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "index_gigadevice_riscv.py"
SPEC = importlib.util.spec_from_file_location("index_gigadevice_riscv", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RiscvSourceTests(unittest.TestCase):
    def test_IAR核心选择映射到Rust目标(self):
        vf = """<project><option><name>GCoreDevice</name><state>RV32IMAC</state></option></project>"""
        vw = """<project><option><name>GCoreDevice</name><state>RV32IMAFC</state></option></project>"""

        self.assertEqual(
            MODULE.parse_iar_core(vf),
            ("RV32IMAC", "riscv32imac-unknown-none-elf"),
        )
        self.assertEqual(
            MODULE.parse_iar_core(vw),
            ("RV32IMAFC", "riscv32imafc-unknown-none-elf"),
        )

    def test_链接脚本只提取有效MEMORY区域(self):
        text = """
MEMORY
{
    flash (rx) : ORIGIN = 0x08000000, LENGTH = 128k
    ram (rwx) : ORIGIN = 0x20000000, LENGTH = 32K
/*  flash (rx) : ORIGIN = 0x20000000, LENGTH = 24k */
}
"""

        self.assertEqual(
            MODULE.parse_linker_memory(text),
            [
                {"name": "flash", "kind": "flash", "address": 0x08000000, "size": 128 * 1024},
                {"name": "ram", "kind": "ram", "address": 0x20000000, "size": 32 * 1024},
            ],
        )

    def test_锁定Firmware生成Riscv来源报告(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "GD32VF103_Firmware_Library_V1.7.0"
            linker_dir = library / "Firmware/RISCV/env_Eclipse"
            project_dir = library / "Template/IAR_project"
            linker_dir.mkdir(parents=True)
            project_dir.mkdir(parents=True)
            (library / ".source.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "archive": "GD32VF103_Firmware_Library_V1.7.0.7z",
                        "archive_sha256": "a" * 64,
                        "tree_sha256": "b" * 64,
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / "Project.ewp").write_text(
                "<project><option><name>GCoreDevice</name><state>RV32IMAC</state></option></project>",
                encoding="utf-8",
            )
            (linker_dir / "GD32VF103xB.lds").write_text(
                "MEMORY {\nflash (rx) : ORIGIN = 0x08000000, LENGTH = 128K\n"
                "ram (rwx) : ORIGIN = 0x20000000, LENGTH = 32K\n}\n",
                encoding="utf-8",
            )
            lock = {
                "firmware": [
                    {
                        "filename": "GD32VF103_Firmware_Library_V1.7.0.7z",
                        "sha256": "a" * 64,
                        "version": "1.7.0",
                        "document_id": 1,
                    }
                ]
            }

            report = MODULE.build_report(lock, root)

        self.assertEqual(report["summary"], {"libraries": 1, "linker_profiles": 1})
        self.assertEqual(report["libraries"][0]["series"], "GD32VF103")
        self.assertEqual(report["libraries"][0]["rust_target"], "riscv32imac-unknown-none-elf")
        self.assertEqual(report["libraries"][0]["linker_profiles"][0]["pattern"], "GD32VF103XB")


if __name__ == "__main__":
    unittest.main()
