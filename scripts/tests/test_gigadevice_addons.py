import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "gigadevice_addons.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_addons", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


PAGE = """
<li class="cl">
  <dd class="data-name">GD32F5xx AddOn</dd>
  <dd class="data-version">1.4.0</dd>
  <a class="agree_box" href="/download/agree/box_id/13/document_id/23/path_type/1">下载</a>
  <dd class="data-time">2026-02-11</dd>
</li>
<li class="cl">
  <dd class="data-name">GD32F20x_AddOn_V2.2.0.rar</dd>
  <dd class="data-version">2.2.0</dd>
  <a class="agree_box" href="/download/agree/box_id/13/document_id/261/path_type/1">下载</a>
  <dd class="data-time">2020-01-01</dd>
</li>
"""


class AddonTests(unittest.TestCase):
    def test_只保留当前AddOn条目(self):
        entries = MODULE.parse_addon_page(PAGE)[0]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "GD32F5xx AddOn")
        self.assertEqual(entries[0].document_id, 23)

    def test_锁定清单输出稳定(self):
        source = MODULE.parse_addon_page(PAGE)[0][0]
        record = MODULE.AddonRecord(
            source=source,
            url="https://www.gd32mcu.com/data/documents/toolSoftware/GD32F527_AddOn_V1.4.0.7z",
            filename="GD32F527_AddOn_V1.4.0.7z",
            size=123,
            sha256="c" * 64,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "addons.lock.json"
            MODULE.write_manifest(path, [record])
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["addons"][0]["document_id"], 23)
        self.assertNotIn("generated_at", data)

    def test_未变化Addon不解析下载地址或执行HEAD(self):
        source = MODULE.parse_addon_page(PAGE)[0][0]
        record = MODULE.AddonRecord(
            source,
            "https://www.gd32mcu.com/data/documents/toolSoftware/addon.7z",
            "addon.7z",
            123,
            "c" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "addons.lock.json"
            MODULE.write_manifest(manifest, [record])

            with mock.patch.object(
                MODULE, "materialize", side_effect=AssertionError("不应物化未变化 AddOn")
            ):
                records, history, plan = MODULE.incremental_addon_records(
                    [source], manifest, root, None
                )

        self.assertEqual(records[0].sha256, "c" * 64)
        self.assertEqual(history, [])
        self.assertEqual(plan["unchanged"], [source.name])


if __name__ == "__main__":
    unittest.main()
