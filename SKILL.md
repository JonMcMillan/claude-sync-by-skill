---
name: sync-envs
description: Read-only status/preview for claude-sync-by-skill — show what would change between this machine and the shared sync folder in both directions, without modifying anything. Use when the user types /sync-envs or asks to preview/check their Claude environment sync status. To actually move files, use /sync-env-up (push local → folder) or /sync-env-down (pull folder → local). For first-time configuration, run the engine with --setup.
---

# sync-envs — status / preview (read-only)

This skill **previews** the unified-environment sync without changing anything. The engine
is `sync_engine.py` in this skill's folder. It never prints secret file contents.

## How to run

The engine lives next to this file. Invoke it with the platform's Python 3:

- **Windows:** `py -3 "<thisSkillDir>\sync_engine.py" --status`
- **macOS/Linux:** `python3 "<thisSkillDir>/sync_engine.py" --status`

(A launcher is also provided: `run.cmd` on Windows, `run.sh` on POSIX — e.g.
`run.sh --status`.) `--status` is the default, so running with no arguments does the same.

## What to do

1. Run the engine in status mode and show the user the output. It prints, for **both**
   directions:
   - what `sync-env-up` *would* push (local → folder),
   - what `sync-env-down` *would* pull (folder → local),
   - any `CONFLICT` rows (changed on both sides), and the device roster on the folder.
2. **Make no changes** — this is read-only. If the user wants to apply changes, point
   them to `/sync-env-up` or `/sync-env-down`.
3. If the engine reports "No configuration found," tell the user to run setup first:
   `py -3 "<thisSkillDir>\sync_engine.py" --setup` (Windows) /
   `python3 "<thisSkillDir>/sync_engine.py" --setup` (macOS/Linux).
4. If it prints a "newer version available" notice, mention they can update with
   `git -C "<thisSkillDir>" pull --ff-only`.

## Notes

- The sync folder is whatever the user chose at setup (Google Drive, OneDrive, Dropbox,
  iCloud, Syncthing, USB, SAN, …). The engine is transport-agnostic and uploads nothing
  on its own.
- Only this tool's own skill folders (`sync-envs`, `sync-env-up`, `sync-env-down`) are
  git-managed and excluded from the synced skills; all the user's other skills sync via
  the folder.
