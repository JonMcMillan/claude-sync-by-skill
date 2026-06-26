"""Tests for the tool-install hint scanner (advisory nudges on sync-down).

Stdlib unittest only. shutil.which is patched so results don't depend on what CLIs the CI
runner happens to have installed.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_engine as eng  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ENGINE = REPO / "sync_engine.py"


def cmd_line(tool, command):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": tool, "input": {"command": command}}]}})


def user_text(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text}]}})


def assistant_text(text):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}})


# Nothing is "already installed" unless a test says so.
@mock.patch("sync_engine.shutil.which", lambda name: None)
class TestDetection(unittest.TestCase):
    def hints(self, text, source):
        return list(eng.detect_install_hints_in_text(text, source))

    def test_npm_global(self):
        tools = [h[0] for h in self.hints("npm install -g wrangler", "command")]
        self.assertIn("wrangler", tools)

    def test_npm_local_is_ignored(self):
        # plain `npm install` (no -g) is a project dep, not a tool
        self.assertEqual(self.hints("npm install", "command"), [])

    def test_winget_and_brew_and_gh_ext(self):
        self.assertIn("GitHub.cli",
                      [h[0] for h in self.hints("winget install GitHub.cli", "command")])
        self.assertIn("gh", [h[0] for h in self.hints("brew install gh", "command")])
        self.assertIn("gh-copilot", [h[0] for h in self.hints(
            "gh extension install gh-copilot", "command")])

    def test_user_mention_with_terminal_cue(self):
        tools = [h[0] for h in self.hints(
            "btw I installed the wrangler CLI manually in my terminal", "user-text")]
        self.assertIn("wrangler", tools)

    def test_user_mention_without_cue_is_ignored(self):
        self.assertEqual(
            self.hints("I installed everything we needed for the build", "user-text"), [])

    def test_stopword_not_flagged(self):
        # even with a cue, "dependencies" must not be flagged as a tool
        self.assertEqual(
            self.hints("I installed the dependencies in my terminal", "user-text"), [])


@mock.patch("sync_engine.shutil.which", lambda name: None)
class TestScanAndGather(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-hints-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def write_transcript(self, name, lines):
        p = self.tmp / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_assistant_prose_is_not_scanned(self):
        # assistant merely *discussing* an install must not produce a hint
        p = self.write_transcript("t.jsonl", [
            assistant_text("You could run `brew install gh` to get the GitHub CLI."),
        ])
        self.assertEqual(eng.scan_transcript_for_install_hints(p), [])

    def test_command_in_assistant_tooluse_is_scanned(self):
        # but a command actually RUN (tool_use) counts, regardless of role
        p = self.write_transcript("t.jsonl", [
            cmd_line("Bash", "brew install gh"),
        ])
        self.assertIn("gh", [h[0] for h in eng.scan_transcript_for_install_hints(p)])

    def test_gather_dedupes_across_files(self):
        a = self.write_transcript("a.jsonl", [cmd_line("PowerShell",
                                                        "winget install Cloudflare.Wrangler")])
        b = self.write_transcript("b.jsonl", [cmd_line("Bash",
                                                        "npm i -g wrangler")])
        hints, capped = eng.gather_install_hints([a, b])
        tools = sorted(h["tool"].lower() for h in hints)
        # two distinct package spellings, both surfaced once each (no cross-file dupes)
        self.assertEqual(len(hints), len(set(tools)))
        self.assertFalse(capped)

    def test_already_installed_is_filtered(self):
        p = self.write_transcript("t.jsonl", [cmd_line("Bash", "npm i -g wrangler")])
        with mock.patch("sync_engine.shutil.which",
                        lambda name: "/usr/bin/wrangler" if name == "wrangler" else None):
            hints, _ = eng.gather_install_hints([p])
        self.assertEqual(hints, [])

    def test_render_is_empty_when_no_hints(self):
        self.assertEqual(eng.render_install_hints([], False), "")


class TestDownReportsHints(unittest.TestCase):
    """End-to-end: a tool installed on machine A is surfaced when machine B syncs down."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-hints-cli-"))
        self.home_a = self.tmp / "A" / ".claude"
        self.home_b = self.tmp / "B" / ".claude"
        self.sync = self.tmp / "folder"
        (self.home_a / "projects" / "proj").mkdir(parents=True)
        self.home_b.mkdir(parents=True)
        (self.home_a / "projects" / "proj" / "s.jsonl").write_text(
            cmd_line("Bash", "npm install -g wrangler") + "\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def run_engine(self, args, home, stdin=None):
        return subprocess.run(
            [sys.executable, str(ENGINE), "--claude-home", str(home)] + args,
            input=stdin, text=True, capture_output=True)

    def test_wrangler_surfaced_on_down(self):
        self.run_engine(["--setup", "--sync-root", str(self.sync), "--device", "A"],
                        self.home_a)
        self.run_engine(["--direction", "up", "--apply", "--confirm-deletions"],
                        self.home_a)
        self.run_engine(["--setup", "--sync-root", str(self.sync), "--device", "B"],
                        self.home_b)
        r = self.run_engine(["--direction", "down", "--apply", "--confirm-deletions"],
                            self.home_b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # wrangler is very unlikely to be on a CI runner's PATH, so it should surface
        self.assertIn("wrangler", r.stdout)

    def test_no_tool_hints_flag_suppresses(self):
        self.run_engine(["--setup", "--sync-root", str(self.sync), "--device", "A"],
                        self.home_a)
        self.run_engine(["--direction", "up", "--apply", "--confirm-deletions"],
                        self.home_a)
        self.run_engine(["--setup", "--sync-root", str(self.sync), "--device", "B"],
                        self.home_b)
        r = self.run_engine(
            ["--direction", "down", "--apply", "--confirm-deletions", "--no-tool-hints"],
            self.home_b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("wrangler", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
