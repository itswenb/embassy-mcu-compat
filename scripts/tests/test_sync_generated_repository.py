import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "sync-generated-repository.sh"
WORKFLOW = Path(__file__).parents[2] / ".github/workflows/update-sources.yml"


class SyncGeneratedRepositoryTests(unittest.TestCase):
    def test_定时任务在提交前同步generated发布树(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        sync = (
            "scripts/sync-generated-repository.sh "
            ".cache/generated/mcu-metapac-publication-v1 "
            '"${GENERATED_REPOSITORY_DIR}"'
        )

        self.assertIn(sync, workflow)
        self.assertLess(workflow.index(sync), workflow.index("提交 generated 变化"))

    def test_保留仓库维护文件并替换生成内容(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            (target / ".git").mkdir(parents=True)
            (target / ".github").mkdir()
            (source / "generation.json").write_text("{}\n", encoding="utf-8")
            (source / "mcu-metapac-generation.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (source / "new.txt").write_text("new\n", encoding="utf-8")
            (target / ".github/ci.yml").write_text("ci\n", encoding="utf-8")
            (target / ".gitignore").write_text("target\n", encoding="utf-8")
            (target / "stale.txt").write_text("stale\n", encoding="utf-8")

            subprocess.run([SCRIPT, source, target], check=True)

            self.assertEqual(
                (target / ".gitignore").read_text(encoding="utf-8"), "target\n"
            )
            self.assertTrue((target / ".github/ci.yml").is_file())
            self.assertTrue((target / "new.txt").is_file())
            self.assertFalse((target / "stale.txt").exists())

    def test_拒绝发布厂商原始归档和文档(self):
        for filename in ("raw.7z", "raw.pack", "raw.pdf"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                target = root / "target"
                source.mkdir()
                (target / ".git").mkdir(parents=True)
                (target / ".github").mkdir()
                (source / "generation.json").write_text("{}\n", encoding="utf-8")
                (source / "mcu-metapac-generation.json").write_text(
                    "{}\n", encoding="utf-8"
                )
                (source / filename).write_bytes(b"vendor")

                result = subprocess.run(
                    [SCRIPT, source, target], capture_output=True, text=True
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("原始厂商文件", result.stderr)
                self.assertFalse((target / filename).exists())


if __name__ == "__main__":
    unittest.main()
