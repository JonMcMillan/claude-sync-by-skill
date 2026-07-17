"""Tests for the task-connector preference.

The engine only *records* which task app (Todoist et al.) the user wants
note->task to use; the actual MCP calls live in the skill. The preference is
stored in the SHARED definition so it propagates to every machine.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_engine as eng  # noqa: E402


class TestTaskConnector(unittest.TestCase):
    def setUp(self):
        self.sync = Path(tempfile.mkdtemp(prefix="ccsync-tc-"))
        # a minimally-initialized sync folder (definition present)
        eng.save_definition(self.sync, {
            "type": eng.SYNC_FOLDER_TYPE, "schemaVersion": eng.SCHEMA_VERSION,
            "devices": {"DEV-A": {}}})

    def tearDown(self):
        shutil.rmtree(str(self.sync), ignore_errors=True)

    def test_unset_is_none(self):
        self.assertIsNone(eng.load_task_connector(self.sync))

    def test_set_and_load_roundtrip(self):
        eng.set_task_connector(self.sync, "todoist",
                               project="Claude", section="sync-env-tasks")
        conn = eng.load_task_connector(self.sync)
        self.assertEqual(conn, {"app": "todoist", "project": "Claude",
                                "section": "sync-env-tasks"})

    def test_stored_in_shared_definition_so_it_propagates(self):
        eng.set_task_connector(self.sync, "todoist", project="Claude")
        defn = json.loads(eng.definition_path(self.sync).read_text())
        self.assertEqual(defn["taskConnector"]["app"], "todoist")
        # the pre-existing roster is preserved, not clobbered
        self.assertIn("DEV-A", defn["devices"])

    def test_clear(self):
        eng.set_task_connector(self.sync, "todoist", project="Claude")
        self.assertIsNone(eng.set_task_connector(self.sync, "none"))
        self.assertIsNone(eng.load_task_connector(self.sync))
        self.assertNotIn("taskConnector",
                         json.loads(eng.definition_path(self.sync).read_text()))

    def test_app_is_lowercased_and_validated(self):
        eng.set_task_connector(self.sync, "Todoist")
        self.assertEqual(eng.load_task_connector(self.sync)["app"], "todoist")
        for bad in ["bad app", "has/slash", "-leading", "UPPER SPACE"]:
            with self.assertRaises(ValueError):
                eng.set_task_connector(self.sync, bad)
        # "" and "none" are not errors - they clear the connector
        self.assertIsNone(eng.set_task_connector(self.sync, ""))

    def test_optional_fields_omitted_when_blank(self):
        eng.set_task_connector(self.sync, "todoist", project="  ", section=None)
        self.assertEqual(eng.load_task_connector(self.sync), {"app": "todoist"})

    def test_set_on_uninitialized_folder_errors(self):
        empty = Path(tempfile.mkdtemp(prefix="ccsync-tc-empty-"))
        try:
            with self.assertRaises(ValueError):
                eng.set_task_connector(empty, "todoist")
        finally:
            shutil.rmtree(str(empty), ignore_errors=True)

    def test_malformed_connector_is_treated_as_none(self):
        eng.save_definition(self.sync, {"type": eng.SYNC_FOLDER_TYPE,
                                        "taskConnector": "not-a-dict"})
        self.assertIsNone(eng.load_task_connector(self.sync))

    def test_render(self):
        self.assertEqual(eng.render_task_connector(None), "none")
        self.assertEqual(
            eng.render_task_connector({"app": "todoist", "project": "Claude",
                                       "section": "sync-env-tasks"}),
            "todoist (Claude / sync-env-tasks)")
        self.assertEqual(
            eng.render_task_connector({"app": "todoist", "project": "Claude"}),
            "todoist (Claude)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
