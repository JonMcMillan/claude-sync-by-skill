"""Functional tests for the claude-sync-by-skill engine.

Stdlib unittest only (no third-party deps), so CI can run it anywhere.
Drives the engine's core functions directly against temp directories.
"""
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_engine as eng  # noqa: E402


def write(path: Path, content: str, mtime: float = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(str(path), (mtime, mtime))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-test-"))
        self.home = self.tmp / "home" / ".claude"
        self.sync = self.tmp / "syncfolder"
        self.home.mkdir(parents=True)
        self.sync.mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def cfg(self, device="MACHINE-A", push_secrets=False):
        return {
            "schemaVersion": eng.SCHEMA_VERSION,
            "device": device,
            "os": "linux",
            "claudeHome": str(self.home),
            "devRoot": None,
            "syncRoot": str(self.sync),
            "pushSecrets": push_secrets,
            "skillExclude": list(eng.TOOL_SKILL_FOLDERS),
        }

    def kinds(self, actions):
        return {a.regkey: a.kind for a in actions if a.kind != "in_sync"}

    def do_apply(self, cfg, direction, baseline, confirm_deletions=True,
                 prefer=None, push_secrets=False):
        actions = eng.compute_plan(direction, cfg, baseline)
        state = eng.load_state(self.sync)
        applied, _ = eng.apply_plan(direction, cfg, actions, baseline, state,
                                    confirm_deletions=confirm_deletions,
                                    prefer=prefer, push_secrets=push_secrets)
        return actions, applied


class TestFolderInit(Base):
    def test_init_empty_folder(self):
        cfg = self.cfg()
        ok = eng.init_or_join_folder(self.sync, cfg, assume_yes=True)
        self.assertTrue(ok)
        definition = eng.load_definition(self.sync)
        self.assertEqual(definition["type"], eng.SYNC_FOLDER_TYPE)
        self.assertIn("MACHINE-A", definition["devices"])
        self.assertTrue((self.sync / "README-claude-sync.md").exists())

    def test_join_registers_second_device(self):
        eng.init_or_join_folder(self.sync, self.cfg("MACHINE-A"), assume_yes=True)
        eng.init_or_join_folder(self.sync, self.cfg("MACHINE-B"), assume_yes=True)
        definition = eng.load_definition(self.sync)
        self.assertEqual(set(definition["devices"]), {"MACHINE-A", "MACHINE-B"})


class TestDirectional(Base):
    def setUp(self):
        super().setUp()
        eng.init_or_join_folder(self.sync, self.cfg(), assume_yes=True)

    def test_up_pushes_new_file(self):
        write(self.home / "plans" / "foo.md", "hello")
        baseline = {}
        actions, applied = self.do_apply(self.cfg(), "up", baseline)
        self.assertEqual(self.kinds(actions)["claude-plans/foo.md"], "copy_up")
        self.assertTrue((self.sync / "claude-plans" / "foo.md").exists())
        self.assertIn("claude-plans/foo.md", baseline)

    def test_old_bug_repro_no_false_delete(self):
        # A file appears in the folder that this machine has never had (added by another
        # machine). With an empty baseline, `up` must LEAVE it, never delete it.
        write(self.sync / "claude-plans" / "bar.md", "from another machine")
        baseline = {}
        actions = eng.compute_plan("up", self.cfg(), baseline)
        self.assertEqual(self.kinds(actions)["claude-plans/bar.md"], "leave_upstream")
        # applying changes nothing destructive
        _, applied = self.do_apply(self.cfg(), "up", baseline)
        self.assertEqual(applied["deleted"], 0)
        self.assertTrue((self.sync / "claude-plans" / "bar.md").exists())

    def test_down_first_sync_pulls_and_keeps_local(self):
        write(self.sync / "claude-plans" / "remote.md", "remote")
        write(self.home / "plans" / "local.md", "local")
        baseline = {}
        actions = eng.compute_plan("down", self.cfg(), baseline)
        k = self.kinds(actions)
        self.assertEqual(k["claude-plans/remote.md"], "copy_down")
        self.assertEqual(k["claude-plans/local.md"], "keep_local_new")
        self.do_apply(self.cfg(), "down", baseline)
        self.assertTrue((self.home / "plans" / "remote.md").exists())
        self.assertTrue((self.home / "plans" / "local.md").exists())  # kept

    def test_up_propagates_local_deletion(self):
        # file known in baseline, present upstream, gone locally -> delete upstream
        write(self.sync / "claude-plans" / "gone.md", "x")
        baseline = {"claude-plans/gone.md": eng.now_iso()}
        actions, applied = self.do_apply(self.cfg(), "up", baseline)
        self.assertEqual(self.kinds(actions)["claude-plans/gone.md"], "del_upstream")
        self.assertFalse((self.sync / "claude-plans" / "gone.md").exists())
        self.assertNotIn("claude-plans/gone.md", baseline)

    def test_down_propagates_upstream_deletion(self):
        write(self.home / "plans" / "gone.md", "x")
        baseline = {"claude-plans/gone.md": eng.now_iso()}
        actions, applied = self.do_apply(self.cfg(), "down", baseline)
        self.assertEqual(self.kinds(actions)["claude-plans/gone.md"], "del_local")
        self.assertFalse((self.home / "plans" / "gone.md").exists())

    def test_deletion_requires_confirmation(self):
        write(self.sync / "claude-plans" / "gone.md", "x")
        baseline = {"claude-plans/gone.md": eng.now_iso()}
        actions = eng.compute_plan("up", self.cfg(), baseline)
        state = eng.load_state(self.sync)
        applied, _ = eng.apply_plan("up", self.cfg(), actions, baseline, state,
                                    confirm_deletions=False, prefer=None,
                                    push_secrets=False)
        self.assertEqual(applied["deleted"], 0)
        self.assertEqual(applied["skipped"], 1)
        self.assertTrue((self.sync / "claude-plans" / "gone.md").exists())

    def test_conflict_not_autoresolved(self):
        t = time.time()
        write(self.home / "plans" / "c.md", "local-new", mtime=t)
        write(self.sync / "claude-plans" / "c.md", "remote-newer", mtime=t + 100)
        baseline = {"claude-plans/c.md": eng.now_iso()}
        actions = eng.compute_plan("up", self.cfg(), baseline)  # folder newer than local
        self.assertEqual(self.kinds(actions)["claude-plans/c.md"], "conflict")
        # without prefer -> left alone
        _, applied = self.do_apply(self.cfg(), "up", dict(baseline))
        self.assertEqual(applied["conflicts"], 1)
        # with prefer folder -> local overwritten
        _, applied2 = self.do_apply(self.cfg(), "up", dict(baseline), prefer="folder")
        self.assertEqual((self.home / "plans" / "c.md").read_text(), "remote-newer")

    def test_idempotent_down(self):
        write(self.sync / "claude-plans" / "a.md", "a")
        baseline = {}
        self.do_apply(self.cfg(), "down", baseline)
        actions2 = eng.compute_plan("down", self.cfg(), baseline)
        self.assertEqual(self.kinds(actions2), {})  # nothing left to do

    def test_unified_memory_roundtrip(self):
        # machine A pushes a transcript; machine B (own home) pulls it
        write(self.home / "projects" / "proj1" / "sess.jsonl", "{}")
        a_baseline = {}
        self.do_apply(self.cfg("MACHINE-A"), "up", a_baseline)
        self.assertTrue((self.sync / "claude-memory" / "proj1" / "sess.jsonl").exists())

        home_b = self.tmp / "homeB" / ".claude"
        home_b.mkdir(parents=True)
        cfg_b = dict(self.cfg("MACHINE-B"))
        cfg_b["claudeHome"] = str(home_b)
        b_baseline = {}
        eng.apply_plan("down", cfg_b, eng.compute_plan("down", cfg_b, b_baseline),
                       b_baseline, eng.load_state(self.sync),
                       confirm_deletions=True, prefer=None, push_secrets=False)
        self.assertTrue((home_b / "projects" / "proj1" / "sess.jsonl").exists())


class TestSkillExclusion(Base):
    def test_tool_skill_folders_excluded(self):
        write(self.home / "skills" / "sync-envs" / "sync_engine.py", "code")
        write(self.home / "skills" / "sync-envs" / ".git" / "config", "gitdata")
        write(self.home / "skills" / "myskill" / "SKILL.md", "mine")
        enumerated = eng.enumerate_side(
            self.home / "skills", "dir", set(eng.TOOL_SKILL_FOLDERS))
        self.assertIn("myskill/SKILL.md", enumerated)
        self.assertFalse(any(k.startswith("sync-envs/") for k in enumerated))


class TestSecrets(Base):
    def setUp(self):
        super().setUp()
        eng.init_or_join_folder(self.sync, self.cfg(), assume_yes=True)
        self.dev = self.tmp / "dev"
        (self.dev / ".claude").mkdir(parents=True)
        write(self.dev / ".claude" / ".env", "TOKEN=secret")

    def cfg_secret(self, push_secrets=False):
        c = self.cfg(push_secrets=push_secrets)
        c["devRoot"] = str(self.dev)
        return c

    def test_secret_push_skipped_by_default(self):
        baseline = {}
        actions = eng.compute_plan("up", self.cfg_secret(), baseline)
        state = eng.load_state(self.sync)
        applied, _ = eng.apply_plan("up", self.cfg_secret(), actions, baseline, state,
                                    confirm_deletions=True, prefer=None,
                                    push_secrets=False)
        self.assertFalse((self.sync / "infra-env" / ".env").exists())
        self.assertGreaterEqual(applied["skipped"], 1)

    def test_secret_push_optin(self):
        baseline = {}
        cfg = self.cfg_secret(push_secrets=True)
        actions = eng.compute_plan("up", cfg, baseline)
        state = eng.load_state(self.sync)
        eng.apply_plan("up", cfg, actions, baseline, state, confirm_deletions=True,
                       prefer=None, push_secrets=True)
        self.assertTrue((self.sync / "infra-env" / ".env").exists())


class TestThreeWayConflict(Base):
    """Baseline-aware (three-way) conflict detection: a CONFLICT only when BOTH sides
    changed since the last reconcile. One-sided changes are 'ahead', not conflicts."""

    def setUp(self):
        super().setUp()
        eng.init_or_join_folder(self.sync, self.cfg(), assume_yes=True)
        self.base = 1_000_000.0

    def _both(self, rel, lcontent, fcontent, lmtime, fmtime):
        write(self.home / "plans" / rel, lcontent, mtime=lmtime)
        write(self.sync / "claude-plans" / rel, fcontent, mtime=fmtime)
        return "claude-plans/" + rel

    def test_live_transcript_local_ahead_is_not_conflict(self):
        # only local moved (e.g. the active session transcript grew); folder == baseline
        rk = self._both("t.md", "grown", "v1", self.base + 100, self.base)
        baseline = {rk: self.base}
        self.assertEqual(self.kinds(eng.compute_plan("down", self.cfg(), baseline))[rk],
                         "ahead_local")  # NOT conflict
        self.assertEqual(self.kinds(eng.compute_plan("up", self.cfg(), baseline))[rk],
                         "copy_up")

    def test_folder_ahead_is_not_conflict(self):
        rk = self._both("t.md", "v1", "newer", self.base, self.base + 100)
        baseline = {rk: self.base}
        self.assertEqual(self.kinds(eng.compute_plan("up", self.cfg(), baseline))[rk],
                         "ahead_folder")  # NOT conflict
        self.assertEqual(self.kinds(eng.compute_plan("down", self.cfg(), baseline))[rk],
                         "copy_down")

    def test_true_conflict_when_both_changed(self):
        rk = self._both("t.md", "local-edit", "folder-edit", self.base + 100, self.base + 50)
        baseline = {rk: self.base}
        for d in ("up", "down"):
            self.assertEqual(self.kinds(eng.compute_plan(d, self.cfg(), baseline))[rk],
                             "conflict")

    def test_legacy_baseline_falls_back_to_two_way(self):
        # legacy baseline stored a timestamp string, not a file mtime -> bm unknown
        rk = self._both("t.md", "v1", "newer", self.base, self.base + 100)
        baseline = {rk: eng.now_iso()}
        self.assertEqual(self.kinds(eng.compute_plan("down", self.cfg(), baseline))[rk],
                         "copy_down")  # folder newer -> pull
        self.assertEqual(self.kinds(eng.compute_plan("up", self.cfg(), baseline))[rk],
                         "conflict")  # folder newer than local on an up

    def test_apply_records_float_mtime_and_heals(self):
        # after a reconcile the baseline holds a numeric mtime, enabling three-way next time
        write(self.home / "plans" / "a.md", "x")
        baseline = {}
        eng.apply_plan("up", self.cfg(), eng.compute_plan("up", self.cfg(), baseline),
                       baseline, eng.load_state(self.sync),
                       confirm_deletions=True, prefer=None, push_secrets=False)
        self.assertIsInstance(baseline["claude-plans/a.md"], float)


class TestProgress(unittest.TestCase):
    """The heartbeat exists to tell 'slow' apart from 'stopped' in a background run."""

    class _Stream:
        def __init__(self):
            self.written = []
            self.flushes = 0

        def write(self, s):
            self.written.append(s)

        def flush(self):
            self.flushes += 1

        def text(self):
            return "".join(self.written)

    def test_disabled_by_default_writes_nothing(self):
        s = self._Stream()
        p = eng.Progress(stream=s)
        p.start("memory")
        p.tick()
        self.assertEqual(s.text(), "")

    def test_start_always_emits_and_flushes(self):
        # The phase line must appear even if the scan wedges immediately after,
        # since it is then the only clue to where it stopped. And it must be
        # flushed: piped stderr is block-buffered, so an unflushed line would
        # not reach a background reader until exit.
        s = self._Stream()
        p = eng.Progress(enabled=True, stream=s)
        p.start("claude-memory (down)")
        self.assertIn("claude-memory (down)", s.text())
        self.assertGreater(s.flushes, 0)

    def test_ticks_are_throttled_but_counted(self):
        s = self._Stream()
        p = eng.Progress(enabled=True, interval=3600, stream=s)
        p.start("memory")
        for _ in range(50):
            p.tick()
        self.assertEqual(p.count, 50)          # every file counted
        self.assertEqual(len(s.written), 1)    # but only the start line emitted

    def test_counter_advances_across_emits(self):
        s = self._Stream()
        p = eng.Progress(enabled=True, interval=0, stream=s)  # emit every tick
        p.start("memory")
        p.tick()
        p.tick()
        self.assertIn(": 1 files", s.text())
        self.assertIn(": 2 files", s.text())

    def test_start_resets_count_per_phase(self):
        s = self._Stream()
        p = eng.Progress(enabled=True, stream=s)
        p.start("a")
        p.tick(5)
        p.start("b")
        self.assertEqual(p.count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
