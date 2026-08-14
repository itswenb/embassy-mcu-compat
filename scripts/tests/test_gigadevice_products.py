import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "gigadevice_products.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_products", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


PAGE = """
<table>
  <tr><th>Part No.</th><th>Core <span>Cortex-M7</span></th><th>Series <span>GD32A711</span></th><th>Package <span>LQFP64</span></th><th>Max Speed (MHz)</th><th>Flash (Bytes) <span>1024K</span></th><th>SRAM (Bytes) <span>128K</span></th></tr>
  <tr><td><a href="/product/mcu/mcus-product-selector/gd32a711art3tb">GD32A711ART3TB</a></td><td>Cortex-M7</td><td>GD32A711</td><td>LQFP64</td><td>160</td><td>1024K</td><td>128K</td></tr>
  <tr><td>GD32A722AXJ3TB</td><td>Dual Cortex-M7</td><td>GD32A722</td><td>BGA257</td><td>320</td><td>2048K</td><td>256K</td></tr>
</table>
"""


class GigaDeviceProductsTests(unittest.TestCase):
    def test_解析并稳定输出A7官方产品事实(self):
        products = MODULE.parse_product_page(PAGE)

        self.assertEqual([item["part_number"] for item in products], ["GD32A711ART3TB", "GD32A722AXJ3TB"])
        self.assertEqual(products[1]["core"], "Dual Cortex-M7")
        self.assertEqual(products[0]["flash_bytes"], 1024 * 1024)
        self.assertEqual(products[0]["url"], "https://www.gigadevice.com/product/mcu/mcus-product-selector/gd32a711art3tb")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "products.json"
            MODULE.write_report(output, PAGE, products)
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(report["summary"]["products"], 2)
        self.assertNotIn("generated_at", report)


if __name__ == "__main__":
    unittest.main()
