import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "gigadevice_official_tool.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_official_tool", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


HTML = """
<li class="cl">
  <dd class="data-name">GD32 All-In-One Programmer</dd>
  <dd class="data-version">5.2.0.40390</dd>
  <dd class="data-time">2026-07-24</dd>
  <a class="agree_box" href="/download/agree/box_id/15/document_id/123/path_type/1">下载</a>
</li>
"""


class OfficialToolTests(unittest.TestCase):
    def test_精确匹配官方工具并保留下载标识(self):
        source = MODULE.parse_tool_page(HTML, "GD32 All-In-One Programmer")

        self.assertEqual(source.name, "GD32 All-In-One Programmer")
        self.assertEqual(source.version, "5.2.0.40390")
        self.assertEqual(source.box_id, 15)
        self.assertEqual(source.document_id, 123)

    def test_工具名称必须唯一精确匹配(self):
        with self.assertRaisesRegex(ValueError, "唯一"):
            MODULE.parse_tool_page(HTML, "GD32 Programmer")

    def test_未变化官方工具不解析下载地址或执行HEAD(self):
        source = MODULE.parse_tool_page(HTML, "GD32 All-In-One Programmer")
        record = MODULE.builder.BuilderRecord(
            source,
            "https://www.gd32mcu.com/data/documents/toolSoftware/programmer.7z",
            "programmer.7z",
            123,
            "d" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "programmer.lock.json"
            MODULE.write_manifest(manifest, source, record)
            (root / record.filename).write_bytes(b"x" * record.size)
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8"))["tool"]["document_id"],
                123,
            )

            with mock.patch.object(
                MODULE.builder,
                "materialize",
                side_effect=AssertionError("不应物化未变化官方工具"),
            ):
                reused, history, plan = MODULE.incremental_tool_record(
                    source, manifest, root, None
                )

        self.assertEqual(reused.sha256, "d" * 64)
        self.assertEqual(history, [])
        self.assertEqual(plan["unchanged"], [source.name])


if __name__ == "__main__":
    unittest.main()
