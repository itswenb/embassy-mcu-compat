import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "gigadevice_catalog.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_catalog", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


PAGE = """
<li class="cl">
  <dd class="data-name">GD32 MCU Selection Guide</dd>
  <dd class="data-version">2025</dd>
  <a href="/download/down/document_id/206/path_type/1">英文</a>
  <a href="/download/down/document_id/206/path_type/2">中文</a>
  <dd class="data-time">2025-10-17</dd>
</li>
"""


class CatalogTests(unittest.TestCase):
    def test_解析中英文选型手册入口(self):
        source = MODULE.parse_catalog_page(PAGE)

        self.assertEqual(source.version, "2025")
        self.assertEqual(source.document_id, 206)
        self.assertEqual(source.path_types, (1, 2))

    def test_提取并过滤型号标记(self):
        tokens = MODULE.extract_model_tokens(
            "GD32 MCU: GD32F103C8T6, GD32F103xx，GD32W515PIQ6。"
        )

        self.assertEqual(tokens, {"GD32F103C8T6", "GD32F103xx", "GD32W515PIQ6"})

    def test_派生目录输出稳定(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            MODULE.write_catalog(path, {"GD32F103C8T6"}, {"GD32F103C8T6", "GD32VW553"})
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["all"], ["GD32F103C8T6", "GD32VW553"])
        self.assertEqual(data["only_zh"], ["GD32VW553"])
        self.assertNotIn("generated_at", data)

    def test_未变化选型指南不解析下载地址或执行HEAD(self):
        source = MODULE.parse_catalog_page(PAGE)
        documents = [
            MODULE.CatalogDocument(
                language,
                f"https://www.gd32mcu.com/data/documents/otherDocument/{language}.pdf",
                f"{language}.pdf",
                123,
                language * 64,
            )
            for language in ("en", "zh")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "catalog.lock.json"
            MODULE.write_manifest(manifest, source, documents)
            for document in documents:
                (root / document.filename).write_bytes(b"x" * document.size)

            with mock.patch.object(
                MODULE, "materialize", side_effect=AssertionError("不应物化未变化选型指南")
            ):
                reused_source, reused_documents, history, plan = (
                    MODULE.incremental_catalog_documents(source, manifest, root)
                )

        self.assertEqual(reused_source, source)
        self.assertEqual(reused_documents, documents)
        self.assertEqual(history, [])
        self.assertEqual(plan["unchanged"], ["GD32 MCU Selection Guide"])


if __name__ == "__main__":
    unittest.main()
