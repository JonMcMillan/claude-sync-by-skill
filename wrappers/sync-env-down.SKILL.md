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

**Always launch it in the background** (the Bash tool's `run_in_background: true`), for
both previews and applies. The sync folder is usually a cloud drive, and when the drive
client is unwell the engine blocks in a kernel filesystem read for minutes. A foreground
run holds the whole turn hostage, so the user cannot ask you to investigate while it
hangs — which is exactly when they most want to. Backgrounding costs nothing on a fast run
(you are notified as soon as it exits) and keeps you reachable on a slow one.

## The 30-second check (do this every run)

A healthy scan reaches its first output within a few seconds. So: **if the run has not
finished at ~30 seconds, check on it — do not keep waiting.**

The engine emits a heartbeat to **stderr** while scanning:

```
[scan] claude-memory (down): 1840 files
```

Read the background output at ~30s, then again ~10s later, and compare:

- **Line count/counter advancing** → healthy, just large. Say so and keep waiting.
- **Last `[scan]` line identical across both samples** → **stalled. Raise the alarm now**
  (see below). The last line also names the phase it wedged in.
- **No `[scan]` lines at all** → it never got started; treat as stalled.

One caveat before crying wolf: the counter only ticks in the per-file loop, so a large
folder can sit quiet inside the directory walk for a stretch and look stalled. That is a
reason to *ask the user to glance at the drive*, not to assume the worst — it is never a
reason to keep silently waiting.

## If it is stalled (usually the sync folder, not the engine)

A run that stalls for minutes almost always means the cloud drive client is half-alive:
directory listings still return filenames whose `stat()` then fails. The engine will
eventually die with `FileNotFoundError [WinError 3]` on a path under the sync folder.

- **Do not trust the drive's process being alive** — it can be running and functionally
  dead (e.g. Google Drive stuck on "checking for updates"). A process/PID check will
  happily report everything is fine. Ask the user to look at the drive client's own UI.
- **Do not trust `Test-Path` / `os.path.exists` on the folder** — that is cached metadata
  and proves nothing. Only an actual byte read of a file under the sync folder does.
- **The fix is a force-restart of the drive client** (Task Manager → end the process →
  relaunch the app), then retry. Waiting it out may never resolve.
- **Retrying is safe.** A preview writes nothing, and an interrupted apply is resumable.
- Do not go chasing git, tool hints, or stdin — those have been investigated and ruled out.

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
6. **Report** what was copied/deleted. A few things the engine may also print, all worth
   relaying:
   - If a **main working folder** is configured, the exact local path to open for each
     synced project — so the user doesn't guess/retype a folder name. `--scaffold`
     pre-creates those empty working folders (it never writes code, only empty dirs).
   - A **tool-install reminder** (CLIs/tools installed during sessions on another machine
     and not on this machine's PATH) — relay it as an advisory suggestion, not an action
     to take automatically. `--no-tool-hints` suppresses the scan.
   - A **git-pull reminder** (project repos whose current branch is behind its remote,
     i.e. code was pushed from another machine) — relay it and offer to run the shown
     `git pull --ff-only`, but only after the user agrees; never pull automatically, and
     heed the uncommitted-changes warning. `--no-git-hints` suppresses the check.
   - **Cross-device notes** (see below). `--no-notes` suppresses them.

## Cross-device notes

After applying, the engine may print a **"Notes left on your other machines"** block —
reminders the user left elsewhere with `/sync-add-note`. Each line is `[<id>] text
(origin, age)`. They surface here because this machine didn't originate them and hasn't
handled them yet; they never surface on the machine that wrote them.

Relay each note, then for **each one** ask the user which they want (don't assume):

- **Make a task** — the note is something to do. Create a task in the user's Todoist:
  project **"Claude"**, section **"sync-env-tasks"** (resolve both by name via the Todoist
  MCP: `find-projects "Claude"` → `find-sections` for that project → `add-tasks` with
  `sectionId`; the known ids are project `6h6J2J34v2267FW5` / section `6h6J2PHfjM8Wpx4X`,
  but prefer name lookup in case they change). Use the note text as the task content. Then
  resolve the note with the task linked:
  `... sync_engine.py --resolve-note "<id>" --note-task "<taskId>"`.
- **Keep it** — still relevant, act on it later. Run `... --ack-notes "<id>"` so it won't
  nag again **on this machine** (it stays active for the user's other machines and in
  `/sync-notes`).
- **Resolve it** — done or no longer needed. **Confirm first** (this clears it on *every*
  machine), then `... --resolve-note "<id>"`. It's recoverable — a tombstone is written,
  nothing is hard-deleted.

You can batch `--ack-notes "id1,id2,..."` for several "keep" decisions in one call. Run
these engine calls in the background like the others.

## First sync

If this machine is new to the folder, the preview is a full-environment analysis. Walk the
user through it item by item — keep local, take the folder's copy, or skip — before
applying. On a first sync no deletions occur and your local-only work is kept.
