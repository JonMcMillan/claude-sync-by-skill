"""End-to-end CLI integration test: drives the real command-line interface via
subprocess (clean stdin) through setup -> up -> join -> down across two simulated
machines sharing one sync folder.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE = REPO / "sync_engine.py"


def run_engine(args, claude_home, stdin=None):
    return subprocess.run(
        [sys.executable, str(ENGINE), "--claude-home", str(claude_home)] + args,
        input=stdin, text=True, capture_output=True,
    )


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-cli-"))
        self.home_a = self.tmp / "machineA" / ".claude"
        self.home_b = self.tmp / "machineB" / ".claude"
        self.sync = self.tmp / "syncfolder"
        (self.home_a / "plans").mkdir(parents=True)
        self.home_b.mkdir(parents=True)
        (self.home_a / "plans" / "note.md").write_text("my note", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def setup_machine(self, home, device):
        # stdin: sync folder path, then device id
        r = run_engine(["--setup"], home, stdin="{}\n{}\n".format(self.sync, device))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((home / ".sync-config.json").exists(), r.stdout + r.stderr)

    def test_full_up_join_down_flow(self):
        # Machine A: setup + push up
        self.setup_machine(self.home_a, "MACHINE-A")
        r = run_engine(["--direction", "up", "--apply", "--confirm-deletions"], self.home_a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.sync / "claude-plans" / "note.md").exists(),
                        "note.md should be pushed to the folder")

        # Machine B: setup (join) + pull down
        self.setup_machine(self.home_b, "MACHINE-B")
        r = run_engine(["--direction", "down", "--apply", "--confirm-deletions"], self.home_b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.home_b / "plans" / "note.md").exists(),
                        "note.md should be pulled onto machine B")

        # status is read-only and must not error
        r = run_engine(["--status"], self.home_b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_preview_makes_no_changes(self):
        self.setup_machine(self.home_a, "MACHINE-A")
        # preview up (no --apply) must not create anything in the folder
        r = run_engine(["--direction", "up"], self.home_a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.sync / "claude-plans" / "note.md").exists(),
                         "preview must not move files")


if __name__ == "__main__":
    unittest.main(verbosity=2)
