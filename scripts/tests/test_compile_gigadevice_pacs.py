import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "compile_gigadevice_pacs.py"
SPEC = importlib.util.spec_from_file_location("compile_gigadevice_pacs", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class PacCompileTests(unittest.TestCase):
    def test_编译器身份忽略主机平台(self):
        version = MODULE.parse_rustc_version(
            "rustc 1.99.0-nightly\ncommit-hash: abc123\nhost: aarch64-apple-darwin\nrelease: 1.99.0-nightly\n"
        )

        self.assertEqual(version, "release=1.99.0-nightly;commit-hash=abc123")

    def test_类型检查包装只补充cortex_m中断trait(self):
        source = MODULE.compile_source(b"#![no_std]\npub struct Pac;\n")

        self.assertTrue(source.startswith(b"#![no_std]"))
        self.assertIn(b"pub unsafe trait InterruptNumber", source)
        self.assertIn(b"fn number(self) -> u16", source)


if __name__ == "__main__":
    unittest.main()
