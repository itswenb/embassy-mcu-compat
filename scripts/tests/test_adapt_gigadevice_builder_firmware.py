import importlib.util
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "adapt_gigadevice_builder_firmware.py"
SPEC = importlib.util.spec_from_file_location("adapt_gigadevice_builder_firmware", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuilderFirmwareAdapterTests(unittest.TestCase):
    def test_生成现有Firmware流水线可读的适配层(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            plugin_id = "com.gigadevice.templatefwlib.arm.gd32h77x_78x_1.2.3.4"
            header = root / f"plugins/{plugin_id}/Firmware/device.h"
            header.parent.mkdir(parents=True)
            header.write_text("#define DEVICE 1\n", encoding="utf-8")
            header_sha256 = hashlib.sha256(header.read_bytes()).hexdigest()
            output = Path(directory) / "adapter"
            report = {
                "provenance": {
                    "builder_archive_sha256": "a" * 64,
                    "builder_tree_sha256": "b" * 64,
                },
                "plugins": [
                    {
                        "id": plugin_id,
                        "series": "gd32h77x_78x",
                        "device_headers": [
                            {"path": f"plugins/{plugin_id}/Firmware/device.h", "sha256": header_sha256}
                        ],
                    }
                ],
            }

            adapter = MODULE.build_adapter(report, root, output)
            adapter_again = MODULE.build_adapter(report, root, output)

        self.assertEqual(adapter, adapter_again)
        self.assertEqual(adapter["lock"]["firmware"][0]["version"], "1.2.3")
        self.assertEqual(adapter["headers"]["libraries"][0]["series"], "GD32H77X_78X")


if __name__ == "__main__":
    unittest.main()
