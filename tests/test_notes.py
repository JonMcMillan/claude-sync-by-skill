"""Tests for the cross-device notes subsystem.

Stdlib unittest only. Notes live in <syncRoot>/notes/ as write-once files so a
dumb cloud mirror can never produce a merge conflict; these tests pin that
model (origin suppression, per-device ack, idempotent tombstone resolution) and
the fact that two devices writing the same store never collide.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_engine as eng  # noqa: E402

A = "DEV-A"
B = "DEV-B"


class TestNotes(unittest.TestCase):
    def setUp(self):
        self.sync = Path(tempfile.mkdtemp(prefix="ccsync-notes-"))

    def tearDown(self):
        shutil.rmtree(str(self.sync), ignore_errors=True)

    # -- creation --------------------------------------------------------- #
    def test_add_creates_active_note_and_strips_text(self):
        n = eng.add_note(self.sync, A, "   do the thing   ")
        self.assertEqual(n["text"], "do the thing")
        self.assertEqual(n["origin"], A)
        active = eng.load_active_notes(self.sync)
        self.assertEqual([x["id"] for x in active], [n["id"]])

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            eng.add_note(self.sync, A, "   ")

    def test_ids_are_unique_even_back_to_back(self):
        ids = {eng.add_note(self.sync, A, "n{}".format(i))["id"]
               for i in range(50)}
        self.assertEqual(len(ids), 50)

    def test_active_notes_sorted_oldest_first(self):
        n1 = eng.add_note(self.sync, A, "first")
        n2 = eng.add_note(self.sync, A, "second")
        order = [n["id"] for n in eng.load_active_notes(self.sync)]
        self.assertEqual(order, [n1["id"], n2["id"]])

    # -- surfacing / origin suppression ----------------------------------- #
    def test_origin_device_is_not_reminded(self):
        eng.add_note(self.sync, A, "left on A")
        self.assertEqual(eng.notes_to_surface(self.sync, A), [])
        self.assertEqual(len(eng.notes_to_surface(self.sync, B)), 1)

    def test_ack_silences_only_that_device(self):
        n = eng.add_note(self.sync, A, "x")
        eng.ack_notes(self.sync, B, [n["id"]])
        self.assertEqual(eng.notes_to_surface(self.sync, B), [])
        # a third device still sees it; the note is still active
        self.assertEqual(len(eng.notes_to_surface(self.sync, "DEV-C")), 1)
        self.assertEqual(len(eng.load_active_notes(self.sync)), 1)

    def test_ack_is_idempotent_and_accumulates(self):
        n1 = eng.add_note(self.sync, A, "one")
        n2 = eng.add_note(self.sync, A, "two")
        eng.ack_notes(self.sync, B, [n1["id"]])
        eng.ack_notes(self.sync, B, [n1["id"], n2["id"]])
        self.assertEqual(eng.load_acked(self.sync, B), {n1["id"], n2["id"]})

    # -- resolution ------------------------------------------------------- #
    def test_resolve_tombstones_and_deactivates(self):
        n = eng.add_note(self.sync, A, "fix me")
        self.assertTrue(eng.resolve_note(self.sync, B, n["id"], task_id="T1"))
        self.assertEqual(eng.load_active_notes(self.sync), [])
        self.assertEqual(eng.notes_to_surface(self.sync, B), [])
        tomb = eng.notes_root(self.sync) / ".resolved" / (n["id"] + ".json")
        self.assertEqual(json.loads(tomb.read_text())["taskId"], "T1")

    def test_resolve_is_idempotent(self):
        n = eng.add_note(self.sync, A, "y")
        self.assertTrue(eng.resolve_note(self.sync, B, n["id"]))
        self.assertTrue(eng.resolve_note(self.sync, A, n["id"]))  # again, other dev
        # the first tombstone wins - it is not overwritten
        tomb = eng.notes_root(self.sync) / ".resolved" / (n["id"] + ".json")
        self.assertEqual(json.loads(tomb.read_text())["resolvedBy"], B)

    def test_resolve_unknown_returns_false(self):
        self.assertFalse(eng.resolve_note(self.sync, A, "no-such--x--y"))

    def test_bad_ids_are_rejected(self):
        for bad in ["../escape", "a/b", "a\\b", ".hidden", ""]:
            with self.assertRaises(ValueError):
                eng.resolve_note(self.sync, A, bad)

    # -- robustness against a dumb mirror --------------------------------- #
    def test_two_devices_writing_the_same_store_do_not_collide(self):
        # No shared mutable file: A and B each write their own note + ack files,
        # so a cloud mirror never has to merge anything.
        na = eng.add_note(self.sync, A, "from A")
        nb = eng.add_note(self.sync, B, "from B")
        eng.ack_notes(self.sync, A, [nb["id"]])
        eng.ack_notes(self.sync, B, [na["id"]])
        active = {n["id"] for n in eng.load_active_notes(self.sync)}
        self.assertEqual(active, {na["id"], nb["id"]})
        # each device is reminded only of the other's note
        self.assertEqual([n["id"] for n in eng.notes_to_surface(self.sync, A)],
                         [])  # A wrote na, acked nb
        self.assertEqual([n["id"] for n in eng.notes_to_surface(self.sync, B)],
                         [])  # B wrote nb, acked na

    def test_malformed_note_file_is_skipped_not_fatal(self):
        eng.add_note(self.sync, A, "good")
        bad = eng.notes_root(self.sync) / "garbage.json"
        bad.write_text("{not json", encoding="utf-8")
        (eng.notes_root(self.sync) / "empty.json").write_text("{}", encoding="utf-8")
        active = eng.load_active_notes(self.sync)
        self.assertEqual([n["text"] for n in active], ["good"])

    def test_empty_store_is_quiet(self):
        self.assertEqual(eng.load_active_notes(self.sync), [])
        self.assertEqual(eng.notes_to_surface(self.sync, A), [])
        self.assertEqual(eng.render_notes_surfacing([]), "")
        self.assertIn("No active notes", eng.render_notes_list([], A))


if __name__ == "__main__":
    unittest.main(verbosity=2)
