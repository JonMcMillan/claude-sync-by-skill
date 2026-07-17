---
name: sync-notes
description: Show all currently active cross-device reminder notes held in the sync folder (from every machine, including this one). Read-only. Use when the user types /sync-notes or asks to see their outstanding sync notes / what reminders are pending across their devices. To leave a new note use /sync-add-note; notes are surfaced and resolved during /sync-env-down.
---

# sync-notes — list active cross-device notes

Read-only. Shows every active note across all the user's machines. The engine
(`sync_engine.py`) lives in the sibling `sync-envs` skill folder.

## How to run the engine

- **Windows:** `py -3 "<claudeSkillsDir>\sync-envs\sync_engine.py" --list-notes`
- **macOS/Linux:** `python3 "<claudeSkillsDir>/sync-envs/sync_engine.py" --list-notes`

**Run it in the background** (the Bash tool's `run_in_background: true`) — it reads
the sync folder, usually a cloud drive; see the timing note in `/sync-env-down` if
it stalls.

## What to do

1. Run the engine with `--list-notes` and show the user the output. Each active
   note lists its id, text, origin machine, and age; notes from the current
   machine are marked `(from this device)`.
2. **Make no changes** — this is read-only. Notes are surfaced for action (make a
   task / keep / resolve) during `/sync-env-down`; adding is `/sync-add-note`.
3. If there are none, say so plainly.
4. If the user wants to clear one from here, you can resolve it directly (confirm
   first — it clears on every machine):
   `... sync_engine.py --resolve-note "<id>"`.
