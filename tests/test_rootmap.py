"""Tests for logical project roots (per-machine path aliasing).

Covers the pure key-mapping helpers, cross-machine memory pairing through a canonical
root, and true-cwd capture + path reporting. Stdlib unittest only; OS-agnostic (string
project keys and POSIX cwd paths so the matrix CI passes on Windows/macOS/Linux).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_engine as eng  # noqa: E402


def write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestKeyHelpers(unittest.TestCase):
    def test_sanitize_matches_claude_keys(self):
        self.assertEqual(eng.sanitize_path_to_key("D:\\dev"), "D--dev")
        self.assertEqual(eng.sanitize_path_to_key("C:\\dev"), "C--dev")
        self.assertEqual(eng.sanitize_path_to_key("/home/me/dev"), "-home-me-dev")

    def test_remap_segment_boundary(self):
        m = {"C--dev": "D--dev"}
        # exact key and child keys remap...
        self.assertEqual(eng.remap_first_segment("C--dev", m), "D--dev")
        self.assertEqual(eng.remap_first_segment("C--dev/sess.jsonl", m),
                         "D--dev/sess.jsonl")
        self.assertEqual(eng.remap_first_segment("C--dev-app/m/MEMORY.md", m),
                         "D--dev-app/m/MEMORY.md")
        # ...but a longer unrelated key sharing a textual prefix does NOT
        self.assertEqual(eng.remap_first_segment("C--development/x", m),
                         "C--development/x")

    def test_remap_longest_prefix_wins(self):
        m = {"C--dev": "ROOT", "C--dev-special": "SPECIAL"}
        self.assertEqual(eng.remap_first_segment("C--dev-special/x", m), "SPECIAL/x")
        self.assertEqual(eng.remap_first_segment("C--dev-other/x", m), "ROOT-other/x")

    def test_empty_map_is_identity(self):
        self.assertEqual(eng.remap_first_segment("C--dev/x", {}), "C--dev/x")

    def test_item_roundtrip(self):
        item = eng.Item("claude-memory", "/l", "/s", "dir",
                        rootmap={"C--dev": "D--dev"})
        canon = item.to_canon("C--dev-app/sess.jsonl")
        self.assertEqual(canon, "D--dev-app/sess.jsonl")
        self.assertEqual(item.to_local(canon), "C--dev-app/sess.jsonl")


class TestCrossMachineMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-rootmap-"))
        self.sync = self.tmp / "syncfolder"
        self.sync.mkdir(parents=True)
        self.home_a = self.tmp / "desktop" / ".claude"   # D:\dev  -> D--dev
        self.home_b = self.tmp / "laptop" / ".claude"    # C:\dev  -> D--dev (canonical)
        self.home_a.mkdir(parents=True)
        self.home_b.mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def cfg(self, device, home, work_key, canonical):
        return {
            "schemaVersion": eng.SCHEMA_VERSION, "device": device, "os": "linux",
            "claudeHome": str(home), "devRoot": None, "syncRoot": str(self.sync),
            "workRoot": "/" + work_key, "workKey": work_key,
            "canonicalKey": canonical,
            "pushSecrets": False, "skillExclude": list(eng.TOOL_SKILL_FOLDERS),
        }

    def apply(self, cfg, direction, baseline):
        actions = eng.compute_plan(direction, cfg, baseline)
        state = eng.load_state(self.sync)
        eng.apply_plan(direction, cfg, actions, baseline, state,
                       confirm_deletions=True, prefer=None, push_secrets=False)
        return actions

    def test_desktop_up_laptop_down_unifies_under_canonical(self):
        cfg_a = self.cfg("DESKTOP", self.home_a, "D--dev", "D--dev")  # anchors canonical
        cfg_b = self.cfg("LAPTOP", self.home_b, "C--dev", "D--dev")   # maps onto it
        eng.init_or_join_folder(self.sync, cfg_a, assume_yes=True)

        # Desktop has the existing project AND a brand-new one it just started.
        write(self.home_a / "projects" / "D--dev" / "sess.jsonl", "{}")
        write(self.home_a / "projects" / "D--dev-newapp" / "s2.jsonl", "{}")
        self.apply(cfg_a, "up", {})

        # Folder stores them under the canonical (== desktop) key.
        self.assertTrue((self.sync / "claude-memory" / "D--dev" / "sess.jsonl").exists())
        self.assertTrue(
            (self.sync / "claude-memory" / "D--dev-newapp" / "s2.jsonl").exists())

        # Laptop pulls down: memory materializes under ITS local key (C--dev*),
        # including the new project it has never seen, with no manual registration.
        self.apply(cfg_b, "down", {})
        self.assertTrue((self.home_b / "projects" / "C--dev" / "sess.jsonl").exists())
        self.assertTrue(
            (self.home_b / "projects" / "C--dev-newapp" / "s2.jsonl").exists())
        # And nothing leaked under the desktop's literal key on the laptop.
        self.assertFalse((self.home_b / "projects" / "D--dev").exists())

    def test_same_project_pairs_as_in_sync_across_machines(self):
        cfg_a = self.cfg("DESKTOP", self.home_a, "D--dev", "D--dev")
        cfg_b = self.cfg("LAPTOP", self.home_b, "C--dev", "D--dev")
        eng.init_or_join_folder(self.sync, cfg_a, assume_yes=True)

        write(self.home_a / "projects" / "D--dev" / "sess.jsonl", "{}")
        self.apply(cfg_a, "up", {})
        self.apply(cfg_b, "down", {})

        # A second down on the laptop has nothing left to do (idempotent under mapping).
        actions = eng.compute_plan("down", cfg_b, {"claude-memory/D--dev/sess.jsonl": eng.now_iso()})
        live = {a.regkey: a.kind for a in actions if a.kind != "in_sync"}
        self.assertEqual(live, {})


class TestCwdCaptureAndReport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-cwd-"))
        self.sync = self.tmp / "syncfolder"
        self.home = self.tmp / "desktop" / ".claude"
        self.sync.mkdir(parents=True)
        self.home.mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def cfg(self, device, work_root, work_key, canonical):
        return {
            "schemaVersion": eng.SCHEMA_VERSION, "device": device, "os": "linux",
            "claudeHome": str(self.home), "devRoot": None, "syncRoot": str(self.sync),
            "workRoot": work_root, "workKey": work_key, "canonicalKey": canonical,
            "pushSecrets": False, "skillExclude": list(eng.TOOL_SKILL_FOLDERS),
        }

    def test_read_project_cwd(self):
        proj = self.home / "projects" / "-work"
        write(proj / "a.jsonl",
              json.dumps({"type": "x"}) + "\n" + json.dumps({"cwd": "/work"}) + "\n")
        self.assertEqual(eng.read_project_cwd(proj), "/work")

    def test_read_project_cwd_tolerates_bom(self):
        proj = self.home / "projects" / "-bom"
        write(proj / "a.jsonl", "﻿" + json.dumps({"cwd": "/work/x"}) + "\n")
        self.assertEqual(eng.read_project_cwd(proj), "/work/x")

    def test_capture_then_report_rebuilds_exact_paths(self):
        cfg_a = self.cfg("DESKTOP", "/work", "-work", "-work")
        eng.init_or_join_folder(self.sync, cfg_a, assume_yes=True)
        eng.ensure_main_work_root(self.sync, cfg_a)

        # Two projects with their true cwds recorded in transcripts.
        write(self.home / "projects" / "-work" / "s.jsonl",
              json.dumps({"cwd": "/work"}) + "\n")
        write(self.home / "projects" / "-work-newapp" / "s.jsonl",
              json.dumps({"cwd": "/work/newapp"}) + "\n")
        eng.update_project_registry(cfg_a)

        reg = eng.load_definition(self.sync)["projects"]
        self.assertEqual(reg["-work"]["subpath"], "")
        self.assertEqual(reg["-work-newapp"]["subpath"], "newapp")

        # A laptop with a DIFFERENT root rebuilds the exact local paths from subpaths.
        cfg_b = self.cfg("LAPTOP", "/elsewhere/code", "-elsewhere-code", "-work")
        paths = dict(eng.local_project_paths(cfg_b))
        self.assertEqual(paths["-work"], str(Path("/elsewhere/code")))
        self.assertEqual(paths["-work-newapp"],
                         str(Path("/elsewhere/code") / "newapp"))

    def test_main_work_root_anchored_and_schema_bumped(self):
        cfg_a = self.cfg("DESKTOP", "/work", "-work", "-work")
        eng.init_or_join_folder(self.sync, cfg_a, assume_yes=True)
        eng.ensure_main_work_root(self.sync, cfg_a)
        definition = eng.load_definition(self.sync)
        self.assertEqual(definition["mainWorkRoot"]["canonicalKey"], "-work")
        self.assertEqual(definition["mainWorkRoot"]["establishedBy"], "DESKTOP")
        self.assertGreaterEqual(definition["schemaVersion"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
