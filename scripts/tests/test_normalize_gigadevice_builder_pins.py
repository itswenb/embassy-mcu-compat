import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "normalize_gigadevice_builder_pins.py"
SPEC = importlib.util.spec_from_file_location(
    "normalize_gigadevice_builder_pins", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuilderPinTests(unittest.TestCase):
    def test_归一封装位置和全部函数类别(self):
        xml = """<PinPadMatrix>
          <PinPad Name="PA0" Type="I/O"
            MainFunction="GPIO_Input,EXTI0"
            Alternate1Functions="USART0_TX,ADC01_IN0"
            AlternateFunctions="SPI0_MISO"
            AdditionalFunctions="WKUP0"
            RemapFunctions="TIMER0_CH0">
            <Package_LQFP48 Number="10"/>
            <Package_QFN32 Number="-"/>
          </PinPad>
          <PinPad Name="VSS" Type="P" MainFunction="VSS">
            <Package_LQFP48 Number="11"/>
          </PinPad>
        </PinPadMatrix>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pins.xml"
            path.write_text(xml, encoding="utf-8")
            matrix = MODULE.parse_matrix(path, Path("GD32F10x/103xxDatasheet.xml"))

        pins = {pin["name"]: pin for pin in matrix["pins"]}
        self.assertEqual(pins["PA0"]["packages"], {"LQFP48": ["10"]})
        self.assertEqual(pins["VSS"]["packages"], {"LQFP48": ["11"]})
        self.assertEqual(
            pins["PA0"]["functions"],
            [
                {"af": None, "name": "WKUP0", "source": "additional"},
                {"af": None, "name": "SPI0_MISO", "source": "alternate"},
                {"af": 1, "name": "ADC01_IN0", "source": "alternate"},
                {"af": 1, "name": "USART0_TX", "source": "alternate"},
                {"af": None, "name": "EXTI0", "source": "main"},
                {"af": None, "name": "GPIO_Input", "source": "main"},
                {"af": None, "name": "TIMER0_CH0", "source": "remap"},
            ],
        )

    def test_同名引脚的多个封装位置不会被覆盖(self):
        xml = """<PinPadMatrix>
          <PinPad Name="PA9" Type="I/O" MainFunction="GPIO_Input">
            <Package_LQFP48 Number="29"/>
          </PinPad>
          <PinPad Name="PA9" Type="I/O alternate" AlternateFunctions="USART0_TX">
            <Package_LQFP48 Number="33"/>
          </PinPad>
        </PinPadMatrix>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pins.xml"
            path.write_text(xml, encoding="utf-8")
            matrix = MODULE.parse_matrix(path, Path("GD32C221/C221CxT6.xml"))

        self.assertEqual(len(matrix["pins"]), 1)
        self.assertEqual(matrix["pins"][0]["packages"], {"LQFP48": ["29", "33"]})
        self.assertEqual(matrix["pins"][0]["types"], ["I/O", "I/O alternate"])
        self.assertEqual(
            [function["name"] for function in matrix["pins"][0]["functions"]],
            ["USART0_TX", "GPIO_Input"],
        )

    def test_解析_afio_路由和二进制重映射值(self):
        xml = """<AFIOPins Remap="true">
          <AFIOPinItem name="USART">
            <PinNames FunctionGroupName="USART0" RemapAFValue="REMAP10">
              <Pin><PinUsedFunction>USART0_TX</PinUsedFunction><PinName>PB6-WKUP</PinName></Pin>
            </PinNames>
          </AFIOPinItem>
        </AFIOPins>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "afio.xml"
            path.write_text(xml, encoding="utf-8")
            source = MODULE.parse_afio(path, Path("GD32F50x/GD32F503.xml"))

        self.assertEqual(
            source["routes"],
            [
                {
                    "function": "USART0_TX",
                    "group": "USART0",
                    "pin": "PB6",
                    "remap": "REMAP10",
                    "value": 2,
                }
            ],
        )

    def test_afio_型号模式只匹配对应产品线(self):
        specific = MODULE.afio_pattern(Path("GD32F50x/GD32F503.xml"))
        family = MODULE.afio_pattern(Path("GD32F4xx.xml"))

        self.assertTrue(MODULE.pattern_matches_device(specific, "GD32F503CC"))
        self.assertFalse(MODULE.pattern_matches_device(specific, "GD32F502CC"))
        self.assertTrue(MODULE.pattern_matches_device(family, "GD32F450VI"))
        self.assertFalse(MODULE.pattern_matches_device(family, "GD32F350CB"))

    def test_仅有AFIO路由也生成规范功能引脚(self):
        afio = {
            "path": "GD32H77x/GD32H779.xml",
            "sha256": "a" * 64,
            "model_pattern": "H779",
            "specificity": 4,
            "_pattern": MODULE.afio_pattern(Path("GD32H77x/GD32H779.xml")),
            "routes": [
                {
                    "function": "USART0_TX",
                    "group": "USART0_TX",
                    "pin": "PA9",
                    "remap": "AF7",
                    "value": 7,
                }
            ],
        }

        full, report = MODULE.build_outputs(
            {"devices": [{"id": "GD32H779II", "matrix_paths": []}]},
            [],
            [afio],
        )

        self.assertEqual(full["devices"][0]["status"], "normalized")
        self.assertEqual(full["devices"][0]["routes"], afio["routes"])
        self.assertEqual(report["devices"][0]["gpio_pins"], 1)


if __name__ == "__main__":
    unittest.main()
