import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "gigadevice_inventory.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_inventory", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class InventoryTests(unittest.TestCase):
    def test_汇总当前下架历史来源和发布产物(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locks = root / "sources/gigadevice"
            publication = root / ".cache/generated/publication"
            locks.mkdir(parents=True)
            (publication / "src").mkdir(parents=True)
            (locks / "firmware.lock.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "firmware": [
                            {
                                "name": "GD32A Firmware Library",
                                "version": "1.0.0",
                                "document_id": 1,
                                "status": "active",
                                "sha256": "a" * 64,
                            },
                            {
                                "name": "GD32B Firmware Library",
                                "version": "1.0.0",
                                "document_id": 2,
                                "status": "withdrawn",
                                "sha256": "b" * 64,
                            },
                        ],
                        "history": [
                            {
                                "name": "GD32A Firmware Library",
                                "version": "0.9.0",
                                "document_id": 3,
                                "status": "superseded",
                                "sha256": "c" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (locks / "builder.lock.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "builder": {
                            "name": "GD32 Embedded Builder",
                            "version": "1.0.0",
                            "document_id": 4,
                            "sha256": "d" * 64,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (publication / "generation.json").write_text("{}\n", encoding="utf-8")
            (publication / "src/lib.rs").write_text("#![no_std]\n", encoding="utf-8")

            inventory = MODULE.build_inventory(root, locks, publication)

        self.assertEqual(inventory["schema_version"], 1)
        self.assertEqual(inventory["summary"]["source_count"], 4)
        self.assertEqual(
            inventory["summary"]["sources_by_status"],
            {"active": 2, "superseded": 1, "withdrawn": 1},
        )
        self.assertEqual(inventory["summary"]["generated_count"], 2)
        self.assertTrue(any(row["kind"] == "tree" for row in inventory["generated"]))
        self.assertEqual(
            [row["path"] for row in inventory["generated"]],
            ["generation.json", "src"],
        )
        self.assertTrue(any(source["historical"] for source in inventory["sources"]))
        self.assertTrue(
            all(not Path(row["path"]).is_absolute() for row in inventory["generated"])
        )
        self.assertNotIn(str(root), json.dumps(inventory))

    def test_拒绝重复当前来源和发布目录外路径(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locks = root / "sources/gigadevice"
            publication = root / "publication"
            locks.mkdir(parents=True)
            publication.mkdir()
            duplicate = {"name": "same", "version": "1", "document_id": 1}
            (locks / "firmware.lock.json").write_text(
                json.dumps({"firmware": [duplicate, duplicate]}), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "重复"):
                MODULE.build_inventory(root, locks, publication)

            (locks / "firmware.lock.json").write_text(
                json.dumps({"firmware": [duplicate]}), encoding="utf-8"
            )
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "发布目录之外"):
                MODULE.build_inventory(root, locks, publication, [outside])


if __name__ == "__main__":
    unittest.main()
