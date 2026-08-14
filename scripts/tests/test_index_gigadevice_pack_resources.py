import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "index_gigadevice_pack_resources.py"
SPEC = importlib.util.spec_from_file_location("index_gigadevice_pack_resources", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


PDSC = """<package>
  <vendor>GigaDevice</vendor><name>TEST_DFP</name>
  <releases><release version="1.2.3"/></releases>
  <devices><family Dfamily="TEST">
    <compile header="Device\\Include\\test.h" define="FAMILY"/>
    <debug svd="SVD\\family.svd"/>
    <memory id="IRAM1" start="0x20000000" size="0x1000"/>
    <subFamily DsubFamily="TEST1">
      <device Dname="TEST123">
        <memory id="IRAM1" size="0x2000"/>
        <algorithm name="Flash\\test.FLM" start="0x08000000" size="0x4000"/>
      </device>
      <device Dname="TEST456"><debug svd="SVD/other.svd"/></device>
    </subFamily>
  </family></devices>
</package>"""


class PackResourceTests(unittest.TestCase):
    def test_继承并覆盖PDSC资源(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "GigaDevice.TEST_DFP.pdsc"
            path.write_text(PDSC, encoding="utf-8")
            identity, records = MODULE.parse_pdsc_resources(path)

        by_name = {record["device"]: record for record in records}
        self.assertEqual(identity, ("TEST_DFP", "1.2.3"))
        self.assertEqual(by_name["TEST123"]["debug"][0]["svd"], "SVD/family.svd")
        self.assertEqual(by_name["TEST456"]["debug"][0]["svd"], "SVD/other.svd")
        self.assertEqual(by_name["TEST123"]["memory"][0]["start"], 0x20000000)
        self.assertEqual(by_name["TEST123"]["memory"][0]["size"], 0x2000)

    def test_拒绝逃逸Pack根目录的资源路径(self):
        self.assertEqual(MODULE.normalize_pack_path(r"SVD\chip.svd"), "SVD/chip.svd")
        for value in ("../chip.svd", "/tmp/chip.svd", r"C:\chip.svd"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.normalize_pack_path(value)


if __name__ == "__main__":
    unittest.main()
