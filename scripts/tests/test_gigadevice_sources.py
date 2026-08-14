import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "gigadevice_sources.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_sources", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


PAGE = """
<ul class="sheet-data">
  <li class="cl">
    <dd class="data-name"> GD32E11x Firmware Library </dd>
    <dd class="data-version">1.6.0</dd>
    <a class="agree_box" href="/download/agree/box_id/12/document_id/397/path_type/1">下载</a>
    <dd class="data-time">2026-02-11</dd>
  </li>
  <li class="cl">
    <dd class="data-name">GD32E11x User Manual</dd>
    <dd class="data-version">1.0</dd>
    <a class="agree_box" href="/download/agree/box_id/12/document_id/999/path_type/1">下载</a>
    <dd class="data-time">2026-01-01</dd>
  </li>
</ul>
<ul class="pagination"><li><a href="/cn/download/7/p/2?kw=Firmware">2</a></li></ul>
"""

UNDERSCORE_NAME_PAGE = """
<li class="cl">
  <dd class="data-name">GD32M53x_Firmware_Library</dd>
  <dd class="data-version">1.0.0</dd>
  <a class="agree_box" href="/download/agree/box_id/12/document_id/777/path_type/1">下载</a>
  <dd class="data-time">2025-06-24</dd>
</li>
"""


class FirmwarePageTests(unittest.TestCase):
    def test_只提取固件库及翻页数(self):
        entries, page_count = MODULE.parse_firmware_page(PAGE)

        self.assertEqual(page_count, 2)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "GD32E11x Firmware Library")
        self.assertEqual(entries[0].version, "1.6.0")
        self.assertEqual(entries[0].document_id, 397)
        self.assertEqual(entries[0].published, "2026-02-11")

    def test_拒绝重复文档编号(self):
        entry = MODULE.parse_firmware_page(PAGE)[0][0]

        with self.assertRaisesRegex(ValueError, "重复的 document_id"):
            MODULE.merge_entries([entry], [entry])

    def test_兼容官网使用下划线的固件库名称(self):
        entries, _ = MODULE.parse_firmware_page(UNDERSCORE_NAME_PAGE)

        self.assertEqual(entries[0].name, "GD32M53x Firmware Library")


class DownloadUrlTests(unittest.TestCase):
    def test_只接受官网工具目录中的7z(self):
        url = MODULE.validate_download_url(
            "/data/documents/toolSoftware/GD32E11x_Firmware_Library_V1.6.0.7z"
        )

        self.assertEqual(
            url,
            "https://www.gd32mcu.com/data/documents/toolSoftware/"
            "GD32E11x_Firmware_Library_V1.6.0.7z",
        )

    def test_拒绝跨站或越界路径(self):
        for url in (
            "https://example.com/data/documents/toolSoftware/file.7z",
            "/data/documents/toolSoftware/../file.7z",
            "/data/documents/manual/file.pdf",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                MODULE.validate_download_url(url)


class ArchiveTests(unittest.TestCase):
    def test_归档成员必须是规范相对路径(self):
        MODULE.validate_archive_members(["root/", "root/file.h"])

        for member in ("/root/file.h", "../file.h", "root/../file.h", r"root\file.h"):
            with self.subTest(member=member), self.assertRaises(ValueError):
                MODULE.validate_archive_members([member])

    def test_解析7zip技术清单并拒绝链接(self):
        listing = """
Path = archive.7z
Type = 7z

----------
Path = root
Folder = +

Path = root/file.h
Folder = -
"""
        self.assertEqual(MODULE.parse_7zip_members(listing), ["root", "root/file.h"])

        with self.assertRaises(ValueError):
            MODULE.parse_7zip_members(
                "Path = link\nFolder = -\nSymbolic Link = ../outside\n"
            )

    def test_从供应商文本中提取唯一SHA256(self):
        digest = "3a9989bea29e6ea5c78e436a9f26d8ef4ea86b28e4c0c9913b1801f9238fb49e"

        self.assertEqual(MODULE.declared_sha256([f"SHA256：{digest}".encode()]), digest)

        with self.assertRaises(ValueError):
            MODULE.declared_sha256([b"no digest"])

    def test_解包目录树哈希检测内容变化并忽略来源标记(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested/file.h").write_bytes(b"one")
            first = MODULE.tree_sha256(root)
            (root / ".source.json").write_text("ignored", encoding="utf-8")
            self.assertEqual(MODULE.tree_sha256(root), first)
            (root / "nested/file.h").write_bytes(b"two")

            self.assertNotEqual(MODULE.tree_sha256(root), first)

    def test_兼容直接载荷与双层归档(self):
        outer = Path("outer.7z")
        inner = Path("inner.7z")

        self.assertEqual(MODULE.select_payload_archive(outer, []), outer)
        self.assertEqual(MODULE.select_payload_archive(outer, [inner]), inner)
        with self.assertRaises(ValueError):
            MODULE.select_payload_archive(outer, [inner, Path("other.7z")])


class ManifestTests(unittest.TestCase):
    def test_清单输出稳定且不含时间戳(self):
        entry = MODULE.parse_firmware_page(PAGE)[0][0]
        record = MODULE.FirmwareRecord(
            source=entry,
            url=MODULE.validate_download_url(
                "/data/documents/toolSoftware/GD32E11x_Firmware_Library_V1.6.0.7z"
            ),
            filename="GD32E11x_Firmware_Library_V1.6.0.7z",
            size=123,
            sha256="a" * 64,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            MODULE.write_manifest(path, [record])
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["schema_version"], 1)
        self.assertNotIn("generated_at", data)
        self.assertEqual(data["firmware"][0]["document_id"], 397)

    def test_未变化来源直接复用锁且不访问归档(self):
        source = MODULE.parse_firmware_page(PAGE)[0][0]
        record = MODULE.FirmwareRecord(
            source,
            "https://www.gd32mcu.com/data/documents/toolSoftware/source.7z",
            "source.7z",
            123,
            "a" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "firmware.lock.json"
            MODULE.write_manifest(manifest, [record])

            with mock.patch.object(
                MODULE, "materialize", side_effect=AssertionError("不应物化未变化来源")
            ):
                records, history, plan = MODULE.incremental_firmware_records(
                    [source], manifest, root
                )

        self.assertEqual(records[0].sha256, "a" * 64)
        self.assertEqual(history, [])
        self.assertEqual(plan["unchanged"], [source.name])


class SourceUpdateTests(unittest.TestCase):
    def test_规划新增更新下架并保留未变化记录(self):
        current = [
            {
                "name": "GD32A Firmware Library",
                "version": "1.0.0",
                "document_id": 1,
                "published": "2025-01-01",
                "sha256": "a" * 64,
            },
            {
                "name": "GD32B Firmware Library",
                "version": "1.0.0",
                "document_id": 2,
                "published": "2025-01-02",
                "sha256": "b" * 64,
            },
            {
                "name": "GD32C Firmware Library",
                "version": "1.0.0",
                "document_id": 3,
                "published": "2025-01-03",
                "sha256": "c" * 64,
            },
        ]
        discovered = [
            {
                "name": "GD32A Firmware Library",
                "version": "1.0.0",
                "document_id": 1,
                "published": "2025-01-01",
            },
            {
                "name": "GD32B Firmware Library",
                "version": "1.1.0",
                "document_id": 4,
                "published": "2026-01-02",
            },
            {
                "name": "GD32D Firmware Library",
                "version": "1.0.0",
                "document_id": 5,
                "published": "2026-01-03",
            },
        ]

        plan = MODULE.plan_source_updates(current, discovered)

        self.assertEqual(plan["unchanged"], ["GD32A Firmware Library"])
        self.assertEqual(plan["updated"], ["GD32B Firmware Library"])
        self.assertEqual(plan["added"], ["GD32D Firmware Library"])
        self.assertEqual(plan["withdrawn"], ["GD32C Firmware Library"])

    def test_合并时下架不删除且历史只追加一次(self):
        current = [
            {
                "name": "GD32A Firmware Library",
                "version": "1.0.0",
                "document_id": 1,
                "published": "2025-01-01",
                "sha256": "a" * 64,
            },
            {
                "name": "GD32B Firmware Library",
                "version": "1.0.0",
                "document_id": 2,
                "published": "2025-01-02",
                "sha256": "b" * 64,
            },
        ]
        replacement = {
            "name": "GD32A Firmware Library",
            "version": "1.1.0",
            "document_id": 3,
            "published": "2026-01-01",
            "sha256": "c" * 64,
        }
        discovered = [
            {key: replacement[key] for key in ("name", "version", "document_id", "published")}
        ]
        plan = MODULE.plan_source_updates(current, discovered)

        merged, history = MODULE.merge_source_updates(current, [replacement], plan, [])
        merged_again, history_again = MODULE.merge_source_updates(
            current, [replacement], plan, history
        )

        self.assertEqual([row["name"] for row in merged], [
            "GD32A Firmware Library",
            "GD32B Firmware Library",
        ])
        self.assertEqual(merged[0]["status"], "active")
        self.assertEqual(merged[1]["status"], "withdrawn")
        self.assertEqual(history[0]["status"], "superseded")
        self.assertEqual(history_again, history)
        self.assertEqual(merged_again, merged)

    def test_拒绝重复逻辑标识(self):
        duplicate = {
            "name": "GD32A Firmware Library",
            "version": "1.0.0",
            "document_id": 1,
            "published": "2025-01-01",
        }

        with self.assertRaisesRegex(ValueError, "重复的来源逻辑标识"):
            MODULE.plan_source_updates([], [duplicate, duplicate])

    def test_允许调用方增加来源特有比较字段(self):
        current = [{
            "name": "GD32A User Manual",
            "version": "1.0",
            "document_id": 1,
            "published": "2025-01-01",
            "path_types": [1],
        }]
        discovered = [{**current[0], "path_types": [1, 2]}]

        plan = MODULE.plan_source_updates(
            current,
            discovered,
            compare_fields=("version", "document_id", "published", "path_types"),
        )

        self.assertEqual(plan["updated"], ["GD32A User Manual"])


if __name__ == "__main__":
    unittest.main()
