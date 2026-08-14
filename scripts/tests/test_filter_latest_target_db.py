import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "filter_latest_target_db.py"
SPEC = importlib.util.spec_from_file_location("filter_latest_target_db", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class LatestTests(unittest.TestCase):
    def test_GitHub仓库地址跨检出方式保持稳定(self):
        self.assertEqual(
            MODULE.canonical_repository("git@github.com:itswenb/cmsis-rust-target-db.git"),
            "https://github.com/itswenb/cmsis-rust-target-db",
        )

    def test_每个Pack只保留最新语义版本(self):
        records = [
            {"source_pack_vendor": "GigaDevice", "source_pack_name": "A", "source_pack_version": "1.0.0", "device": "OLD"},
            {"source_pack_vendor": "GigaDevice", "source_pack_name": "A", "source_pack_version": "1.2.0", "device": "NEW"},
            {"source_pack_vendor": "GigaDevice", "source_pack_name": "B", "source_pack_version": "2.0.0", "device": "OTHER"},
        ]

        latest = MODULE.latest_records(records)

        self.assertEqual([record["device"] for record in latest], ["NEW", "OTHER"])

    def test_拒绝非语义版本(self):
        with self.assertRaises(ValueError):
            MODULE.latest_records(
                [{"source_pack_vendor": "V", "source_pack_name": "P", "source_pack_version": "latest"}]
            )

    def test_记录生成器提交与派生文件哈希(self):
        record = {
            "source_pack_vendor": "GigaDevice",
            "source_pack_name": "GD32F10x_DFP",
            "source_pack_version": "2.3.0",
            "device": "GD32F103C8",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "devices.jsonl"
            output_dir = root / "latest"
            manifest = root / "target-db.lock.json"
            input_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            MODULE.filter_file(
                input_path,
                output_dir,
                generator={"repository": "https://example.test/db", "revision": "a" * 40},
                manifest=manifest,
            )
            metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
            lock = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertEqual(metadata["generator"]["revision"], "a" * 40)
        self.assertEqual(lock["generator"], metadata["generator"])
        self.assertRegex(lock["outputs"]["devices_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
