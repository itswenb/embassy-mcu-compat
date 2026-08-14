import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "extract_gigadevice_manual_text.py"
SPEC = importlib.util.spec_from_file_location(
    "extract_gigadevice_manual_text", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ManualTextTests(unittest.TestCase):
    def test_按PDF哈希与工具版本确定性缓存文本(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "manuals" / "181-1-manual.pdf"
            pdf.parent.mkdir()
            pdf.write_bytes(b"%PDF-test")
            lock = {
                "schema_version": 1,
                "manuals": [
                    {
                        "name": "GD32F10x User Manual",
                        "version": "2.9",
                        "document_id": 181,
                        "selected_path_type": 1,
                        "filename": pdf.name,
                        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    }
                ],
            }
            calls = []
            original = MODULE.convert_pdf

            def fake_convert(_binary, _source, output):
                calls.append(output)
                output.write_text("DMA table\fnext page\n", encoding="utf-8")

            MODULE.convert_pdf = fake_convert
            try:
                first = MODULE.build_report(
                    lock, pdf.parent, root / "text", Path("pdftotext"), "pdftotext 1.0"
                )
                second = MODULE.build_report(
                    lock, pdf.parent, root / "text", Path("pdftotext"), "pdftotext 1.0"
                )
            finally:
                MODULE.convert_pdf = original

        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(first["summary"]["manuals"], 1)
        self.assertEqual(first["manuals"][0]["pages"], 2)
        self.assertRegex(first["manuals"][0]["text_sha256"], r"^[0-9a-f]{64}$")

    def test_同一转换器支持数据手册清单(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "datasheets" / "630-1-datasheet.pdf"
            pdf.parent.mkdir()
            pdf.write_bytes(b"%PDF-test")
            lock = {
                "schema_version": 1,
                "datasheets": [
                    {
                        "name": "GD32A508xx Datasheet",
                        "version": "1.3",
                        "document_id": 630,
                        "selected_path_type": 1,
                        "filename": pdf.name,
                        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    }
                ],
            }
            original = MODULE.convert_pdf
            MODULE.convert_pdf = lambda _binary, _source, output: output.write_text(
                "Alternate function table\n", encoding="utf-8"
            )
            try:
                report = MODULE.build_report(
                    lock,
                    pdf.parent,
                    root / "text",
                    Path("pdftotext"),
                    "pdftotext 1.0",
                    "datasheets",
                )
            finally:
                MODULE.convert_pdf = original

        self.assertEqual(report["summary"]["datasheets"], 1)
        self.assertEqual(report["datasheets"][0]["document_id"], 630)


if __name__ == "__main__":
    unittest.main()
