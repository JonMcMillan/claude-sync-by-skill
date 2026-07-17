"""Tests for the advisory git-pull reminders (read-only behind-check on project repos)."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_engine as eng  # noqa: E402

HAVE_GIT = shutil.which("git") is not None

GIT_CFG = ["-c", "user.email=t@t.t", "-c", "user.name=t",
           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]


def run_git(args, cwd):
    return subprocess.run(["git"] + GIT_CFG + args, cwd=str(cwd),
                          capture_output=True, text=True)


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestGitHints(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-git-"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _behind_repo(self):
        """Create a local clone that is 1 commit behind its bare remote."""
        remote = self.tmp / "remote.git"
        run_git(["init", "--bare", str(remote)], self.tmp)
        run_git(["clone", str(remote), "work"], self.tmp)
        work = self.tmp / "work"
        (work / "f.txt").write_text("1")
        run_git(["add", "."], work)
        run_git(["commit", "-m", "c1"], work)
        run_git(["push", "origin", "main"], work)
        run_git(["clone", str(remote), "local"], self.tmp)  # our machine, up to date
        local = self.tmp / "local"
        (work / "f.txt").write_text("2")  # advance the remote
        run_git(["add", "."], work)
        run_git(["commit", "-m", "c2"], work)
        run_git(["push", "origin", "main"], work)
        return local

    def test_detects_behind(self):
        local = self._behind_repo()
        hints, capped = eng.gather_git_pull_hints([("proj", str(local))])
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["behind"], 1)
        self.assertFalse(hints[0]["dirty"])
        self.assertFalse(capped)

    def test_dirty_flagged(self):
        local = self._behind_repo()
        (local / "uncommitted.txt").write_text("wip")
        hints, _ = eng.gather_git_pull_hints([("proj", str(local))])
        self.assertTrue(hints[0]["dirty"])

    def test_up_to_date_repo_no_hint(self):
        local = self._behind_repo()
        run_git(["pull", "--ff-only"], local)  # now current
        hints, _ = eng.gather_git_pull_hints([("proj", str(local))])
        self.assertEqual(hints, [])

    def test_no_upstream_skipped(self):
        solo = self.tmp / "solo"
        solo.mkdir()
        run_git(["init", str(solo)], self.tmp)
        (solo / "x").write_text("x")
        run_git(["add", "."], solo)
        run_git(["commit", "-m", "x"], solo)
        hints, _ = eng.gather_git_pull_hints([("proj", str(solo))])
        self.assertEqual(hints, [])  # no tracking branch -> skipped

    def test_non_git_dir_skipped(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        hints, _ = eng.gather_git_pull_hints([("proj", str(plain))])
        self.assertEqual(hints, [])

    def test_render_is_advisory_and_suppressible(self):
        out = eng.render_git_pull_hints(
            [{"path": "/x", "branch": "main", "behind": 3, "dirty": True}], False)
        self.assertIn("behind origin", out)
        self.assertIn("pull --ff-only", out)
        self.assertIn("--no-git-hints", out)
        self.assertIn("uncommitted", out)
        self.assertEqual(eng.render_git_pull_hints([], False), "")


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestGitTimeout(unittest.TestCase):
    """git() must never block forever on a hung network op or credential prompt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-gto-"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_hung_git_is_killed_at_the_timeout(self):
        # ext:: runs an arbitrary command as a transport helper, giving a
        # deterministic offline hang with a grandchild holding the pipes.
        start = time.time()
        r = eng.git(["-c", "protocol.ext.allow=always",
                     "ls-remote", "ext::sleep 30"], self.tmp, timeout=2)
        elapsed = time.time() - start
        if r.returncode != eng.GIT_TIMEOUT_RC and elapsed < 1:
            self.skipTest("ext:: helper unavailable; git exited early")
        self.assertEqual(r.returncode, eng.GIT_TIMEOUT_RC)
        # Killing only the direct child would leave `sleep` holding the pipes
        # and stall the follow-up read for the full 30s.
        self.assertLess(elapsed, 15, "timeout did not bound wall-clock time")
        self.assertEqual(r.stdout, "")
        self.assertIn("timed out", r.stderr)

    def test_normal_git_still_works(self):
        run_git(["init", str(self.tmp)], self.tmp)
        r = eng.git(["rev-parse", "--is-inside-work-tree"], self.tmp)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "true")

    def test_env_is_not_leaked_to_parent(self):
        # git() sets GIT_TERMINAL_PROMPT=0 (and friends) for the child only.
        # Use --version: rc is 0 on every platform and needs no repo, editor,
        # or config - unlike `git var GIT_EDITOR`, whose rc depends on whether
        # the host has an editor configured (it does not on CI runners).
        before = os.environ.get("GIT_TERMINAL_PROMPT")
        r = eng.git(["--version"], self.tmp)
        self.assertEqual(r.returncode, 0)
        self.assertIn("git version", r.stdout)
        self.assertEqual(os.environ.get("GIT_TERMINAL_PROMPT"), before,
                         "git() must not leak env changes into the parent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
