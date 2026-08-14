import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "gigadevice_iar.py"
SPEC = importlib.util.spec_from_file_location("gigadevice_iar", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


RELEASE = {
    "tag_name": "9.70.4",
    "published_at": "2026-03-06T12:03:00Z",
    "html_url": "https://github.com/iarsystems/arm/releases/tag/9.70.4",
    "assets": [
        {
            "name": "cxarm-device-support-additional-9.70.4.20260226111516.tar.bz2",
            "browser_download_url": "https://github.com/iarsystems/arm/releases/download/9.70.4/cxarm-device-support-additional-9.70.4.20260226111516.tar.bz2",
            "size": 68_681_728,
            "digest": "sha256:" + "a" * 64,
        }
    ],
}

ATOM = """\
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<updated>2026-03-06T12:03:28Z</updated>
<link rel="alternate" href="https://github.com/iarsystems/arm/releases/tag/9.70.4"/>
</entry></feed>
"""
ASSETS = """\
<li><a href="/iarsystems/arm/releases/download/9.70.4/cxarm-device-support-additional-9.70.4.20260226111516.tar.bz2">包</a>
<span>sha256:57f33246bd156b1baf569dc337120aac513c76a07e17975fb7495c4bcf615d5a</span></li>
"""


class IarSourceTests(unittest.TestCase):
    def test_只接受官方仓库唯一additional设备支持资产(self):
        source = MODULE.parse_release(RELEASE)

        self.assertEqual(source.version, "9.70.4")
        self.assertEqual(source.sha256, "a" * 64)
        self.assertEqual(source.size, 68_681_728)

        bad = json.loads(json.dumps(RELEASE))
        bad["assets"][0]["browser_download_url"] = "https://example.com/a.tar.bz2"
        with self.assertRaisesRegex(ValueError, "不安全"):
            MODULE.parse_release(bad)

    def test_锁定资产未变化时不重复下载(self):
        source = MODULE.parse_release(RELEASE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "iar.lock.json"
            MODULE.write_manifest(manifest, source)

            with mock.patch.object(
                MODULE.common, "cache_file_available", return_value=True
            ), mock.patch.object(
                MODULE, "materialize", side_effect=AssertionError("不应重复下载")
            ):
                record, changed = MODULE.incremental_record(source, manifest, root)

        self.assertFalse(changed)
        self.assertEqual(record.sha256, source.sha256)

    def test_IAR更新时保留旧锁历史(self):
        source = MODULE.parse_release(RELEASE)
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "iar.lock.json"
            MODULE.write_manifest(manifest, source)
            updated = source._replace(version="9.70.5", sha256="b" * 64)
            MODULE.write_manifest(manifest, updated)

            data = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["version"], "9.70.4")
        self.assertEqual(data["history"][0]["status"], "superseded")

    def test_从公开页面发现资产而不依赖限流API(self):
        source = MODULE.parse_public_release(ATOM, ASSETS, 68_681_728)

        self.assertEqual(source.version, "9.70.4")
        self.assertEqual(source.size, 68_681_728)
        self.assertEqual(
            source.sha256,
            "57f33246bd156b1baf569dc337120aac513c76a07e17975fb7495c4bcf615d5a",
        )

    def test_A7全型号按IAR设备族映射三份SVD(self):
        self.assertEqual(MODULE.svd_name_for_device("GD32A711AR"), "GD32A71x.svd")
        self.assertEqual(MODULE.svd_name_for_device("GD32A714BZ"), "GD32A714x.svd")
        self.assertEqual(MODULE.svd_name_for_device("GD32A744BX"), "GD32A72_A74x.svd")
        with self.assertRaisesRegex(ValueError, "不支持"):
            MODULE.svd_name_for_device("GD32F103C8")

    def test_解析IAR设备配置链接内存和Flash算法几何(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            toolkit = root / "cxarm-9.70.4"
            device = toolkit / "arm/config/devices/GD/GD32A7xx/GD32A711x/GD32A711AR.i79"
            ddf = toolkit / "arm/config/debugger/GD/GD32A711AR.ddf"
            linker = toolkit / "arm/config/linker/GD/GD32A711x.icf"
            board = toolkit / "arm/config/flashloader/GD/FlashGD32A711x.board"
            loader = toolkit / "arm/config/flashloader/GD/FlashGD32A711x.flash"
            executable = toolkit / "arm/config/flashloader/GD/FlashGD32A71x.out"
            for path in (device, ddf, linker, board, loader, executable):
                path.parent.mkdir(parents=True, exist_ok=True)
            device.write_text(
                """[DDF FILE]
name=GD/GD32A711AR.ddf
[LINKER FILE]
name=$TOOLKIT_DIR$/config/linker/GD/GD32A711x.icf
[FLASH LOADER]
little=$TOOLKIT_DIR$/config/flashloader/GD/FlashGD32A711x.board
""",
                encoding="utf-8",
            )
            ddf.write_text("device", encoding="utf-8")
            linker.write_text(
                """define symbol __ICFEDIT_region_IROM1_start__ = 0x08000000;
define symbol __ICFEDIT_region_IROM1_end__ = 0x08000FFF;
define symbol __ICFEDIT_region_IRAM1_start__ = 0x24000000;
define symbol __ICFEDIT_region_IRAM1_end__ = 0x240007FF;
""",
                encoding="utf-8",
            )
            board.write_text(
                """<flash_board><pass><range>CODE 0x08000000 0x08000FFF</range>
<loader>$TOOLKIT_DIR$/config/flashloader/GD/FlashGD32A711x.flash</loader>
</pass></flash_board>""",
                encoding="utf-8",
            )
            loader.write_text(
                """<flash_device>
<exe>$TOOLKIT_DIR$/config/flashloader/GD/FlashGD32A71x.out</exe>
<page>8</page><block>2 0x800</block><flash_base>0x08000000</flash_base>
</flash_device>""",
                encoding="utf-8",
            )
            executable.write_bytes(b"IAR flash algorithm")

            config = MODULE._device_configuration(root, "GD32A711AR")

        self.assertEqual(config["ddf"], "cxarm-9.70.4/arm/config/debugger/GD/GD32A711AR.ddf")
        self.assertEqual(
            config["linker"]["memory"],
            [
                {"name": "IROM1", "kind": "flash", "start": 0x08000000, "size": 0x1000},
                {"name": "IRAM1", "kind": "ram", "start": 0x24000000, "size": 0x800},
            ],
        )
        flash = config["flash"]["regions"][0]
        self.assertEqual((flash["write_size"], flash["erase_size"], flash["blocks"]), (8, 0x800, 2))
        self.assertEqual(
            flash["algorithm"]["sha256"],
            hashlib.sha256(b"IAR flash algorithm").hexdigest(),
        )

    def test_IAR配置引用不能越出工具链根目录(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside.icf"
            outside.write_text("x", encoding="utf-8")
            self.addCleanup(outside.unlink, missing_ok=True)

            with self.assertRaisesRegex(ValueError, "根目录"):
                MODULE._toolkit_path(root, "$TOOLKIT_DIR$/../outside.icf")


if __name__ == "__main__":
    unittest.main()
