import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "scan_gigadevice_programmer.py"
SPEC = importlib.util.spec_from_file_location("scan_gigadevice_programmer", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ProgrammerScanTests(unittest.TestCase):
    def test_拒绝归档越界路径(self):
        with self.assertRaisesRegex(ValueError, "不安全"):
            MODULE.validate_archive_members(["ok/file.xml", "../escape"])

    def test_从文件名和内容收集_h77_a7_证据(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "GD32H77_devices.xml").write_text(
                '<device name="GD32H77DIX"/>', encoding="utf-8"
            )
            (root / "database.bin").write_bytes(b"prefix GD32A7XX suffix")

            report = MODULE.scan_tree(root)

        self.assertEqual(report["h77_tokens"], ["GD32H77DIX", "GD32H77_DEVICES"])
        self.assertEqual(report["a7_tokens"], ["GD32A7XX"])
        self.assertEqual(report["h77_files"], ["GD32H77_devices.xml"])
        self.assertEqual(report["a7_files"], ["database.bin"])


if __name__ == "__main__":
    unittest.main()
