import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sync-gigadevice-sources.sh"


class SyncGigadeviceSourcesTests(unittest.TestCase):
    def test_拒绝未知同步阶段(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--phase", "unknown"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("未知同步阶段", result.stderr)


if __name__ == "__main__":
    unittest.main()
