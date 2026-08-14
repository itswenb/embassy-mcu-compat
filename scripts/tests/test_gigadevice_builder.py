import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "gigadevice_builder.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_builder", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


PAGE = """
<li class="cl">
  <dd class="data-name">GD32 Embedded Builder</dd>
  <dd class="data-version">1.5.11_Rel_r41150</dd>
  <a class="agree_box" href="/download/agree/box_id/15/document_id/552/path_type/1">下载</a>
  <dd class="data-time">2026-07-24</dd>
</li>
"""


class BuilderTests(unittest.TestCase):
    def test_解析唯一Builder版本(self):
        source = MODULE.parse_builder_page(PAGE)

        self.assertEqual(source.version, "1.5.11_Rel_r41150")
        self.assertEqual(source.document_id, 552)
        self.assertEqual(source.published, "2026-07-24")

    def test_清单输出稳定(self):
        source = MODULE.parse_builder_page(PAGE)
        record = MODULE.BuilderRecord(
            source=source,
            url="https://www.gd32mcu.com/data/documents/toolSoftware/GD32EB_v1.5.11_Rel(1).7z",
            filename="GD32EB_v1.5.11_Rel(1).7z",
            size=123,
            sha256="b" * 64,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "builder.lock.json"
            MODULE.write_manifest(path, record)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["builder"]["document_id"], 552)
        self.assertNotIn("generated_at", data)

    def test_未变化Builder不解析下载地址或执行HEAD(self):
        source = MODULE.parse_builder_page(PAGE)
        record = MODULE.BuilderRecord(
            source,
            "https://www.gd32mcu.com/data/documents/toolSoftware/builder.7z",
            "builder.7z",
            123,
            "b" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "builder.lock.json"
            MODULE.write_manifest(manifest, record)

            with mock.patch.object(
                MODULE, "materialize", side_effect=AssertionError("不应物化未变化 Builder")
            ):
                reused, history, plan = MODULE.incremental_builder_record(
                    source, manifest, root, None
                )

        self.assertEqual(reused.sha256, "b" * 64)
        self.assertEqual(history, [])
        self.assertEqual(plan["unchanged"], ["GD32 Embedded Builder"])


if __name__ == "__main__":
    unittest.main()
