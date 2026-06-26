---
name: sync-env-down
description: Pull the unified Claude environment (memory, plans, settings, skills, secrets) DOWN from the shared sync folder onto this machine, overwriting/updating local with the latest. Use when the user types /sync-env-down or says they just sat down at this machine and want to bring over the environment they were working on elsewhere / pull the latest from the sync folder. Always previews changes and confirms before any destructive deletion; never clobbers newer local work without flagging a conflict.
---

# sync-env-down — pull sync folder → local

Brings the unified environment onto this machine. The engine (`sync_engine.py`) lives in
the sibling `sync-envs` skill folder.

## How to run the engine

- **Windows:** `py -3 "<claudeSkillsDir>\sync-envs\sync_engine.py" <args>`
- **macOS/Linux:** `python3 "<claudeSkillsDir>/sync-envs/sync_engine.py" <args>`

## Procedure (always preview first, then confirm)

1. **Preview:** run with `--direction down` (no `--apply`). Show the user the summary:
   what would be pulled, any `CONFLICT` rows (local is newer than the folder — your
   unpushed work), and any `trash+DELETE local` rows (files deleted upstream that would be
   removed here; recoverable under `<syncFolder>/.trash/`).
2. **If everything is `in sync`,** say so and stop. (Re-running down is safe/idempotent.)
3. **Confirm** before applying — call out destructive deletions. Never auto-resolve
   `CONFLICT` rows; ask which side to keep (preferring local keeps your newer work).
4. **Apply:** on approval, re-run with `--direction down --apply --confirm-deletions`.
   Add `--prefer local` or `--prefer folder` to resolve conflicts the user decided on.
5. **Secrets** are pulled normally (folder → local); pulled `.env` files get restrictive
   `0600` permissions on macOS/Linux. Never print secret contents.
6. **Report** what was copied/deleted. If the engine prints a **tool-install reminder**
   (CLIs/tools that were installed during sessions on another machine and aren't on this
   machine's PATH), relay it to the user as a suggestion — it's advisory, not an action to
   take automatically. `--no-tool-hints` suppresses the scan.

## First sync

If this machine is new to the folder, the preview is a full-environment analysis. Walk the
user through it item by item — keep local, take the folder's copy, or skip — before
applying. On a first sync no deletions occur and your local-only work is kept.
