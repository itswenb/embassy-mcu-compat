import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "index_gigadevice_programmer.py"
SPEC = importlib.util.spec_from_file_location("index_gigadevice_programmer", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ProgrammerIndexTests(unittest.TestCase):
    def test_完整料号归并到容量明确的规范型号(self):
        tokens = [
            "GD32A711ART3TA",
            "GD32A711ART3TB",
            "GD32A711BRT3TB",
            "GD32H77DIIK7",
            "GD32H77DIPK7",
            "GD32H77X",
        ]

        devices = MODULE.devices_from_tokens(tokens)

        self.assertEqual(
            devices,
            [
                {"id": "GD32A711AR", "part_numbers": ["GD32A711ART3TA", "GD32A711ART3TB"]},
                {"id": "GD32A711BR", "part_numbers": ["GD32A711BRT3TB"]},
                {"id": "GD32H77DII", "part_numbers": ["GD32H77DIIK7"]},
                {"id": "GD32H77DIP", "part_numbers": ["GD32H77DIPK7"]},
            ],
        )

    def test_解析并校验_programmer_flash_页几何(self):
        xml = """\
<MCUGroup series="GD32H77x">
  <Flash McuPartNo="GD32H77xXIXX" baseAddress="0x08000000" RRAMSize="128" Size="64">
    <pageGroup pageNumber="3">
      <Page startIndex="0" endIndex="1" startAddress="0x08000000" endAddress="0x0801FFFF" pageSize="65536" bank="0" type="RRAM"/>
      <Page startIndex="2" endIndex="2" startAddress="0x08200000" endAddress="0x0820FFFF" pageSize="65536" bank="" type="Flash"/>
    </pageGroup>
  </Flash>
</MCUGroup>
"""

        profiles = MODULE.parse_flash_xml(xml)

        self.assertEqual(profiles[0]["pattern"], "GD32H77XXIXX")
        self.assertEqual(profiles[0]["rram_size"], 128 * 1024)
        self.assertEqual(profiles[0]["flash_size"], 64 * 1024)
        self.assertEqual(profiles[0]["pages"][0]["count"], 2)


if __name__ == "__main__":
    unittest.main()
