import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "gigadevice_manuals.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_manuals", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


PAGE = """
<ul class="sheet-data">
  <li class="cl"><dl>
    <dd class="data-name">GD32F10x User Manual</dd>
    <dd class="data-version">2.9</dd>
    <dd class="enversion"><a href="/download/down/document_id/181/path_type/1">EN</a></dd>
    <dd class="chversion"><a href="/download/down/document_id/181/path_type/2">CN</a></dd>
    <dd class="data-time">2025-02-19</dd>
  </dl></li>
  <li class="cl"><dl>
    <dd class="data-name">GD32A513 User Manual</dd>
    <dd class="data-version">1.5</dd>
    <dd class="enversion"><a href="/download/agree/box_id/12/document_id/565/path_type/1">EN</a></dd>
    <dd class="data-time">2025-09-10</dd>
  </dl></li>
  <li class="cl"><dl>
    <dd class="data-name">AN011 Migration</dd>
    <dd class="data-version">1.0</dd>
    <dd class="enversion"><a href="/download/down/document_id/358/path_type/1">EN</a></dd>
    <dd class="data-time">2022-04-21</dd>
  </dl></li>
</ul>
<a href="/cn/download/6/p/2">2</a>
"""

DATASHEET_PAGE = """
<ul class="sheet-data">
  <li class="cl"><dl>
    <dd class="data-name">GD32A508xx Datasheet</dd>
    <dd class="data-version">1.3</dd>
    <dd class="enversion"><a href="/download/down/document_id/630/path_type/1">EN</a></dd>
    <dd class="data-time">2026-04-15</dd>
  </dl></li>
  <li class="cl"><dl>
    <dd class="data-name">GD32A508xx User Manual</dd>
    <dd class="data-version">1.5</dd>
    <dd class="enversion"><a href="/download/down/document_id/565/path_type/1">EN</a></dd>
    <dd class="data-time">2025-09-10</dd>
  </dl></li>
</ul>
"""


class ManualTests(unittest.TestCase):
    def test_只提取GD32用户手册及语言入口(self):
        sources, pages = MODULE.parse_manual_page(PAGE)

        self.assertEqual(pages, 2)
        self.assertEqual(
            sources,
            [
                MODULE.ManualSource("GD32A513 User Manual", "1.5", 565, "2025-09-10", (1,)),
                MODULE.ManualSource("GD32F10x User Manual", "2.9", 181, "2025-02-19", (1, 2)),
            ],
        )

    def test_同一脚本可发现并校验官方数据手册(self):
        sources, pages = MODULE.parse_manual_page(
            DATASHEET_PAGE, MODULE.DATASHEET_KIND
        )

        self.assertEqual(pages, 1)
        self.assertEqual(
            sources,
            [
                MODULE.ManualSource(
                    "GD32A508xx Datasheet", "1.3", 630, "2026-04-15", (1,)
                )
            ],
        )
        self.assertEqual(
            MODULE.validate_download_url(
                "/data/documents/datasheet/GD32A508xx_Datasheet_Rev1.3.pdf",
                MODULE.DATASHEET_KIND,
            ),
            "https://www.gd32mcu.com/data/documents/datasheet/"
            "GD32A508xx_Datasheet_Rev1.3.pdf",
        )
        self.assertEqual(
            MODULE.validate_download_url(
                "/data/documents/datasheet/GD32A103xx%20Datasheet_Rev1.2.pdf",
                MODULE.DATASHEET_KIND,
            ),
            "https://www.gd32mcu.com/data/documents/datasheet/"
            "GD32A103xx%20Datasheet_Rev1.2.pdf",
        )

    def test_只接受官网用户手册PDF(self):
        self.assertEqual(
            MODULE.validate_download_url(
                "/data/documents/userManual/GD32F10x_User_Manual_Rev2.9.pdf"
            ),
            "https://www.gd32mcu.com/data/documents/userManual/"
            "GD32F10x_User_Manual_Rev2.9.pdf",
        )
        for value in (
            "https://example.com/data/documents/userManual/file.pdf",
            "/data/documents/userManual/../file.pdf",
            "/data/documents/toolSoftware/file.pdf",
            "/data/documents/userManual/file.7z",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.validate_download_url(value)

    def test_清单稳定且优先记录英文选择(self):
        source = MODULE.ManualSource(
            "GD32F10x User Manual", "2.9", 181, "2025-02-19", (1, 2)
        )
        record = MODULE.ManualRecord(
            source,
            1,
            "https://www.gd32mcu.com/data/documents/userManual/manual.pdf",
            "181-1-manual.pdf",
            123,
            "a" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manuals.lock.json"
            MODULE.write_manifest(path, [record])
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["manuals"][0]["selected_path_type"], 1)
        self.assertNotIn("generated_at", data)

    def test_未变化手册不解析下载地址或执行HEAD(self):
        source = MODULE.ManualSource(
            "GD32F10x User Manual", "2.9", 181, "2025-02-19", (1, 2)
        )
        record = MODULE.ManualRecord(
            source,
            1,
            "https://www.gd32mcu.com/data/documents/userManual/manual.pdf",
            "181-1-manual.pdf",
            123,
            "a" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manuals.lock.json"
            MODULE.write_manifest(manifest, [record])
            (root / record.filename).write_bytes(b"x" * record.size)

            with mock.patch.object(
                MODULE, "materialize", side_effect=AssertionError("不应物化未变化手册")
            ):
                records, history, plan = MODULE.incremental_manual_records(
                    [source], manifest, root, MODULE.MANUAL_KIND
                )

        self.assertEqual(records[0].sha256, "a" * 64)
        self.assertEqual(history, [])
        self.assertEqual(plan["unchanged"], [source.name])


if __name__ == "__main__":
    unittest.main()
