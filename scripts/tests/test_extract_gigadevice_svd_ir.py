import importlib.util
import stat
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "extract_gigadevice_svd_ir.py"
SPEC = importlib.util.spec_from_file_location("extract_gigadevice_svd_ir", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class SvdIrTests(unittest.TestCase):
    def test_提取转换并复用已校验缓存(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdsc = root / "pack/vendor.pdsc"
            svd = root / "pack/test.svd"
            pdsc.parent.mkdir(parents=True)
            pdsc.write_text("<package/>", encoding="utf-8")
            svd.write_text("<device/>", encoding="utf-8")
            chiptool = root / "chiptool"
            converter = root / "converter"
            chiptool.write_text(
                "#!/bin/sh\nout=\"$5\"\nmkdir -p \"$out\"\nprintf 'block/TEST:\\n  items: []\\n' > \"$out/test.yaml\"\n",
                encoding="utf-8",
            )
            converter.write_text(
                "#!/bin/sh\nmkdir -p \"$2\"\nprintf '{\"block/TEST\":{\"items\":[]}}\\n' > \"$2/test.json\"\n",
                encoding="utf-8",
            )
            chiptool.chmod(chiptool.stat().st_mode | stat.S_IXUSR)
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            sha256 = MODULE.common._sha256(svd)
            audit = {
                "chiptool": {"revision": "a" * 40},
                "svds": [
                    {
                        "path": "test.svd",
                        "sha256": sha256,
                        "size": svd.stat().st_size,
                        "source_pdsc_path": "pack/vendor.pdsc",
                        "normalized_sha256": sha256,
                        "normalizations": [],
                        "status": "cached",
                        "peripheral_register_roots": [
                            {"address": 0x4000, "name": "TEST", "register_root": "TEST"}
                        ],
                        "interrupt_vectors": [{"name": "TEST_IRQ", "value": 1}],
                    }
                ],
            }
            cache = root / "cache"

            first = MODULE.extract(audit, root, root / "normalized", chiptool, converter, cache)
            second = MODULE.extract(audit, root, root / "normalized", chiptool, converter, cache)
            converter.write_text(
                "#!/bin/sh\nmkdir -p \"$2\"\nprintf '{\"block/TEST\":{\"items\":[],\"description\":\"new\"}}\\n' > \"$2/test.json\"\n",
                encoding="utf-8",
            )
            third = MODULE.extract(audit, root, root / "normalized", chiptool, converter, cache)

        self.assertEqual(first["summary"], {"svds": 1, "generated": 1, "cached": 0, "json_files": 1})
        self.assertEqual(second["summary"], {"svds": 1, "generated": 0, "cached": 1, "json_files": 1})
        self.assertEqual(second["svds"][0]["register_roots"][0]["register_root"], "TEST")
        self.assertEqual(second["svds"][0]["interrupt_vectors"][0]["value"], 1)
        self.assertEqual(third["summary"]["generated"], 1)


if __name__ == "__main__":
    unittest.main()
