import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "plan_gigadevice_update.py"
SPEC = importlib.util.spec_from_file_location("plan_gigadevice_update", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class UpdatePlanTests(unittest.TestCase):
    def test_来源与指纹决定三种动作(self):
        unchanged = {
            "firmware": {key: [] for key in ("unchanged", "added", "updated", "withdrawn")}
        }
        unchanged["firmware"]["unchanged"] = ["GD32A Firmware Library"]
        changed = json.loads(json.dumps(unchanged))
        changed["firmware"]["updated"] = ["GD32A Firmware Library"]

        self.assertEqual(MODULE.decide_action(unchanged, "new", "new"), "noop")
        self.assertEqual(MODULE.decide_action(unchanged, "new", "old"), "derive")
        self.assertEqual(MODULE.decide_action(unchanged, "new", None), "materialize")
        self.assertEqual(MODULE.decide_action(changed, "new", "new"), "materialize")

    def test_流水线指纹稳定且能检测脚本变化(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.py"
            second = root / "second.sh"
            first.write_text("print('one')\n", encoding="utf-8")
            second.write_text("echo two\n", encoding="utf-8")

            before = MODULE.pipeline_fingerprint(root, [second, first])
            repeated = MODULE.pipeline_fingerprint(root, [first, second])
            first.write_text("print('changed')\n", encoding="utf-8")
            after = MODULE.pipeline_fingerprint(root, [first, second])

        self.assertEqual(before, repeated)
        self.assertNotEqual(before, after)

    def test_只有显式成功标记才替换旧指纹(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            marker = root / "success.json"
            old = "a" * 64
            new = "b" * 64
            marker.write_text(
                json.dumps({"pipeline_fingerprint": old}) + "\n", encoding="utf-8"
            )
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "action": "derive",
                        "pipeline_fingerprint": new,
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(MODULE.read_successful_fingerprint(marker), old)
            MODULE.mark_success(plan_path, marker)

            data = json.loads(marker.read_text(encoding="utf-8"))

        self.assertEqual(data["pipeline_fingerprint"], new)
        self.assertEqual(data["action"], "derive")
        self.assertNotIn("generated_at", data)

    def test_网页包装变化不应冒充产品事实更新(self):
        products = [{"part_number": "GD32A711AV", "flash_bytes": 1048576}]

        unchanged = MODULE.plan_snapshot_change("GD32A7 产品选择器", products, products)
        updated = MODULE.plan_snapshot_change(
            "GD32A7 产品选择器",
            products,
            [{"part_number": "GD32A711AV", "flash_bytes": 2097152}],
        )

        self.assertEqual(unchanged["unchanged"], ["GD32A7 产品选择器"])
        self.assertEqual(updated["updated"], ["GD32A7 产品选择器"])

    def test_旧Catalog锁从中英文文档推导下载入口(self):
        current = MODULE.normalize_catalog_lock_record(
            {
                "version": "2025",
                "document_id": 206,
                "published": "2025-10-17",
                "documents": {"en": {}, "zh": {}},
            }
        )

        self.assertEqual(current["available_path_types"], [1, 2])


if __name__ == "__main__":
    unittest.main()
