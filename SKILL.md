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

**Always launch it in the background** (the Bash tool's `run_in_background: true`). The
sync folder is usually a cloud drive, and when the drive client is unwell the engine blocks
in a kernel filesystem read for minutes; a foreground run holds the whole turn hostage so
the user cannot ask you to investigate while it hangs. Backgrounding costs nothing on a
fast run and keeps you reachable on a slow one.

**The 30-second check (do this every run).** A healthy scan reaches its first output within
a few seconds, so if the run has not finished at ~30s, check on it rather than waiting. The
engine heartbeats to **stderr** while scanning (`[scan] claude-memory (up): 1840 files`);
`--no-progress` suppresses it. Read the background output at ~30s and again ~10s later: if
the counter is advancing it is healthy, just large; if the last `[scan]` line is identical
across both samples (or absent entirely) it is **stalled — raise the alarm**. The last line
names the phase it wedged in. The counter only ticks in the per-file loop, so a big folder
can go quiet inside the directory walk and look stalled — a reason to ask the user to
glance at the drive, never a reason to keep silently waiting.

When stalled, suspect the drive rather than the engine. The
drive can be running yet functionally dead (e.g. Google Drive stuck on "checking for
updates"), so a process/PID check will falsely report health, and `Test-Path` on the folder
is only cached metadata — an actual byte read of a file under the sync folder is the one
meaningful check. The fix is to force-restart the drive client (Task Manager → end the
process → relaunch), then retry; waiting it out may never resolve. Retrying is safe — this
mode writes nothing. Do not chase git, tool hints, or stdin; those have been ruled out.

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
