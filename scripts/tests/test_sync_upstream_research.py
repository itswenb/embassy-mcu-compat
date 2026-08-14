import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "sync_upstream_research.py"
SPEC = importlib.util.spec_from_file_location("sync_upstream_research", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class UpstreamTests(unittest.TestCase):
    def test_解析滚动仓库HEAD(self):
        revision = "b" * 40
        self.assertEqual(MODULE.parse_remote_head(f"{revision}\tHEAD\n"), revision)
        with self.assertRaises(ValueError):
            MODULE.parse_remote_head("main\tHEAD\n")

    def test_只接受完整Git提交(self):
        MODULE.validate_revision("a" * 40)

        for revision in ("main", "abc", "g" * 40):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                MODULE.validate_revision(revision)


if __name__ == "__main__":
    unittest.main()
