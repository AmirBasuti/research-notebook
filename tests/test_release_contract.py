"""Public CLI defaults and synthetic example checks."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from scripts import research

class ReleaseContractTests(unittest.TestCase):
    def test_default_notebook_is_generic(self):
        args = research.parser().parse_args(["check"])
        self.assertEqual(args.notebook, "notebook")

    def test_notebook_override_remains_available(self):
        args = research.parser().parse_args(["--notebook", "notes", "check"])
        self.assertEqual(args.notebook, "notes")

    def test_shipped_example_completes_documented_cycle(self):
        package = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "demo"
            shutil.copytree(package / "examples/demo", root)
            commands = [
                ["record", "--id", "first-state", "--type", "state", "--title", "Synthetic sensor study",
                 "--provenance", "user", "--evidence-status", "proposed", "--outcome", "pending",
                 "--body-file", str(root / "body.md")],
                ["build"], ["check"], ["context"], ["search", "sensor"],
            ]
            for command in commands:
                result = subprocess.run([sys.executable, "-B", str(package / "scripts/research.py"),
                                         "--root", str(root), *command],
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("first-state", result.stdout)
            self.assertTrue((root / "notebook/CURRENT.md").is_file())
