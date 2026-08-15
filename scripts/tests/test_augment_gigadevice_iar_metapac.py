import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "augment_gigadevice_iar_metapac.py"
SPEC = importlib.util.spec_from_file_location("augment_gigadevice_iar_metapac", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class AugmentIarMetapacTests(unittest.TestCase):
    def test_只剥离PAC开头的crate内部属性(self):
        source = '# ! [allow (unused)] #![doc = "test"] # ! [no_std] pub enum Interrupt {}'

        self.assertEqual(MODULE.strip_inner_attributes(source), "pub enum Interrupt {}")
        self.assertEqual(MODULE.strip_inner_attributes("pub mod gpio {}"), "pub mod gpio {}")

    def test_只移除chiptool内置common并保留后续外设模块(self):
        source = (
            "pub mod gpio { pub fn get() {} } "
            'pub mod common { pub const DOC: &str = "}"; pub struct Reg<T> { value: T } } '
            "pub mod tcm { pub struct Tcm; }"
        )

        self.assertEqual(
            MODULE.strip_embedded_common(source),
            "pub mod gpio { pub fn get() {} }\npub mod tcm { pub struct Tcm; }",
        )


if __name__ == "__main__":
    unittest.main()
