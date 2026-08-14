import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "audit_gigadevice_svds.py"
SPEC = importlib.util.spec_from_file_location("audit_gigadevice_svds", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


SVD = """<device><name>TEST123</name><peripherals>
  <peripheral><name>GPIOA</name><baseAddress>0x40010800</baseAddress>
    <interrupt><name>EXTI0</name><value>6</value></interrupt>
    <registers><register><name>CTL</name><addressOffset>0</addressOffset>
      <fields><field><name>MODE</name><bitOffset>0</bitOffset><bitWidth>2</bitWidth></field></fields>
    </register></registers>
  </peripheral>
</peripherals></device>"""

RCU_SVD = """<device><name>TEST_RCU</name><peripherals>
  <peripheral><name>CCTL</name><groupName>RCU</groupName><baseAddress>0x40021000</baseAddress><registers>
    <register><name>AHBEN</name><addressOffset>0x14</addressOffset><fields>
      <field><name>DMA0EN</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
      <field><name>IGNORED</name><bitOffset>1</bitOffset><bitWidth>2</bitWidth></field>
    </fields></register>
    <register><name>AHBRST</name><addressOffset>0x28</addressOffset><fields>
      <field><name>DMA0RST</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
    </fields></register>
  </registers></peripheral>
</peripherals></device>"""


class SvdAuditTests(unittest.TestCase):
    def test_支持IAR报告中的直接SVD相对路径(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            svd = root / "GD/GD32A71x.svd"
            svd.parent.mkdir(parents=True)
            svd.write_text("<device/>", encoding="utf-8")
            entry = {
                "path": "GD/GD32A71x.svd",
                "sha256": MODULE.common._sha256(svd),
                "size": svd.stat().st_size,
            }

            self.assertEqual(MODULE._source_path(root, entry), svd)

    def test_统计SVD生成前的结构闭包(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.svd"
            path.write_text(SVD, encoding="utf-8")
            stats = MODULE.svd_stats(path)

        self.assertEqual(stats["device_name"], "TEST123")
        self.assertEqual(stats["peripherals"], 1)
        self.assertEqual(stats["peripheral_names"], ["GPIOA"])
        self.assertEqual(stats["interrupts"], 1)
        self.assertEqual(stats["registers"], 1)
        self.assertEqual(stats["fields"], 1)

    def test_拒绝没有外设的SVD(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.svd"
            path.write_text("<device><name>EMPTY</name></device>", encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE.svd_stats(path)

    def test_提取RCU单比特门控作为独立证据(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rcu.svd"
            path.write_text(RCU_SVD, encoding="utf-8")
            stats = MODULE.svd_stats(path)

        self.assertEqual(
            stats["rcu_gates"],
            [
                {
                    "bit": 0,
                    "field": "DMA0EN",
                    "kind": "enable",
                    "name": "DMA0",
                    "register": "AHBEN",
                    "register_offset": 0x14,
                },
                {
                    "bit": 0,
                    "field": "DMA0RST",
                    "kind": "reset",
                    "name": "DMA0",
                    "register": "AHBRST",
                    "register_offset": 0x28,
                },
            ],
        )

    def test_规范化XML声明前的BOM与空白(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prefixed.svd"
            path.write_bytes(
                b"\xef\xbb\xbf \r\n<?xml version=\"1.0\" encoding=\"UTF-8\"?>" + SVD.encode()
            )
            stats = MODULE.svd_stats(path)

        self.assertEqual(stats["device_name"], "TEST123")

    def test_规范化厂商access别名与空派生寄存器(self):
        xml = b"""<device><access>read</access><access>write</access>
          <peripherals>
            <peripheral><name>ADC0</name><registers><register><name>SAMPR0</name>
              <addressOffset>0x20</addressOffset></register></registers></peripheral>
            <peripheral><name>ADC2</name><interrupt><name>RTC_T\tamper</name><value>2</value></interrupt>
              <registers><register derivedFrom="ADC0.SAMPR0"></register></registers></peripheral>
          </peripherals></device>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.svd"
            path.write_bytes(xml)
            data, transformations = MODULE.normalized_svd_bytes(path)

        self.assertIn(b"<access>read-only</access>", data)
        self.assertIn(b"<access>write-only</access>", data)
        self.assertIn(b"<name>SAMPR0</name>", data)
        self.assertIn(b"<addressOffset>0x20</addressOffset>", data)
        self.assertIn(b"<name>RTC_Tamper</name>", data)
        self.assertEqual(len(transformations), 4)

    def test_压平链式派生外设(self):
        xml = b"""<device><peripherals>
          <peripheral><name>GPIOA</name><baseAddress>0x4000</baseAddress></peripheral>
          <peripheral derivedFrom="GPIOA"><name>GPIOB</name><baseAddress>0x4100</baseAddress></peripheral>
          <peripheral derivedFrom="GPIOB"><name>GPIOC</name><baseAddress>0x4200</baseAddress></peripheral>
        </peripherals></device>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "derived.svd"
            path.write_bytes(xml)
            data, transformations = MODULE.normalized_svd_bytes(path)

        self.assertIn(b'<peripheral derivedFrom="GPIOA"><name>GPIOC</name>', data)
        self.assertIn("flatten-peripheral-derived-from:1", transformations)


if __name__ == "__main__":
    unittest.main()
