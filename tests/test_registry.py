"""Project registry (PR #1) must be non-destructive: a machine processing a project
whose local transcript came from ANOTHER machine (foreign cwd) must not clobber the good
subpath recorded by the machine that actually works that project."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_engine as eng  # noqa: E402


class TestRegistryNonDestructive(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsync-reg-"))
        self.home = self.tmp / "home" / ".claude"
        self.sync = self.tmp / "sync"
        (self.home / "projects").mkdir(parents=True)
        self.sync.mkdir(parents=True)
        self.dev = self.tmp / "dev"
        self.dev.mkdir()
        self.work_key = eng.sanitize_path_to_key(str(self.dev))
        self.cfg = {
            "schemaVersion": eng.SCHEMA_VERSION, "device": "DESKTOP", "os": "linux",
            "claudeHome": str(self.home), "syncRoot": str(self.sync),
            "workRoot": str(self.dev), "workKey": self.work_key,
            "canonicalKey": self.work_key,  # this machine anchors the canonical root
        }
        eng.init_or_join_folder(self.sync, self.cfg, assume_yes=True)

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _seed_registry(self, canon_key, entry):
        d = eng.load_definition(self.sync)
        d.setdefault("projects", {})[canon_key] = entry
        eng.save_definition(self.sync, d)

    def _make_project(self, key, cwd):
        pdir = self.home / "projects" / key
        pdir.mkdir(parents=True)
        (pdir / "s.jsonl").write_text(json.dumps({"cwd": cwd}), encoding="utf-8")

    def _registry(self):
        return eng.load_definition(self.sync).get("projects", {})

    def test_foreign_cwd_does_not_clobber_good_subpath(self):
        key = self.work_key + "-Project-Keyring"
        # good value recorded by the laptop (its own C:\ path)
        self._seed_registry(key, {"subpath": "Project-Keyring",
                                  "sourceCwd": "C:/dev/Project-Keyring",
                                  "updatedBy": "LAPTOP", "updatedAt": "x"})
        # locally the transcript is the pulled laptop copy -> foreign cwd
        self._make_project(key, "C:/dev/Project-Keyring")
        eng.update_project_registry(self.cfg)
        self.assertEqual(self._registry()[key]["subpath"], "Project-Keyring")  # preserved

    def test_good_local_cwd_fills_stale_empty_subpath(self):
        key = self.work_key + "-identity-service"
        self._seed_registry(key, {"subpath": "", "sourceCwd": "",
                                  "updatedBy": "old", "updatedAt": "x"})
        self._make_project(key, str(self.dev / "identity-service"))
        eng.update_project_registry(self.cfg)
        self.assertEqual(self._registry()[key]["subpath"], "identity-service")  # re-filled

    def test_missing_cwd_preserves_existing(self):
        key = self.work_key + "-ridgewill-site"
        self._seed_registry(key, {"subpath": "ridgewill-site",
                                  "sourceCwd": "D:/dev/ridgewill-site",
                                  "updatedBy": "OTHER", "updatedAt": "x"})
        # memory-only namespace locally: a project dir with no .jsonl -> cwd None
        (self.home / "projects" / key / "memory").mkdir(parents=True)
        eng.update_project_registry(self.cfg)
        self.assertEqual(self._registry()[key]["subpath"], "ridgewill-site")  # kept


if __name__ == "__main__":
    unittest.main(verbosity=2)
