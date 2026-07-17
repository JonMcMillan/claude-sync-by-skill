---
name: sync-env-up
description: Push this machine's Claude environment (memory, plans, settings, skills) UP to the shared sync folder, updating the unified copy with version + deletion checks. Use when the user types /sync-env-up or says they're done working on this machine and want to sync their environment up / push their changes to the sync folder so another machine can pull them. Always previews changes and confirms before any destructive deletion. Secrets are not pushed unless explicitly enabled.
---

# sync-env-up — push local → sync folder

Pushes this machine's environment to the shared sync folder. The engine
(`sync_engine.py`) lives in the sibling `sync-envs` skill folder.

## How to run the engine

- **Windows:** `py -3 "<claudeSkillsDir>\sync-envs\sync_engine.py" <args>`
- **macOS/Linux:** `python3 "<claudeSkillsDir>/sync-envs/sync_engine.py" <args>`

`<claudeSkillsDir>` is the user's Claude skills directory (this skill's parent), e.g.
`~/.claude/skills`.

**Always launch it in the background** (the Bash tool's `run_in_background: true`), for
both previews and applies. The sync folder is usually a cloud drive, and when the drive
client is unwell the engine blocks in a kernel filesystem read for minutes. A foreground
run holds the whole turn hostage, so the user cannot ask you to investigate while it
hangs. Backgrounding costs nothing on a fast run and keeps you reachable on a slow one.

**The 30-second check (do this every run).** A healthy scan reaches its first output within
a few seconds, so if the run has not finished at ~30s, check on it rather than waiting. The
engine heartbeats to **stderr** while scanning (`[scan] claude-memory (up): 1840 files`).
Read the background output at ~30s and again ~10s later: if the counter is advancing it is
healthy, just large; if the last `[scan]` line is identical across both samples (or absent
entirely) it is **stalled — raise the alarm**. The last line names the phase it wedged in.
The counter only ticks in the per-file loop, so a big folder can go quiet inside the
directory walk and look stalled — a reason to ask the user to glance at the drive, never a
reason to keep silently waiting.

When stalled, it is almost certainly the cloud drive, not the engine. Note that the drive
can be running yet functionally dead (e.g. Google Drive stuck on "checking for updates"),
so a process/PID check will falsely report health, and
`Test-Path` on the folder is cached metadata that proves nothing — only an actual byte read
of a file under the sync folder is meaningful. The fix is to force-restart the drive client
(Task Manager → end the process → relaunch), then retry; waiting it out may never resolve.
Retrying is safe: a preview writes nothing. Do not chase git, tool hints, or stdin — those
have been ruled out.

## Procedure (always preview first, then confirm)

1. **Preview:** run with `--direction up` (no `--apply`). Show the user the full summary:
   what would be pushed, any `CONFLICT` rows, and especially any `trash+DELETE folder`
   rows (destructive — these remove files from the sync folder; recoverable under
   `<syncFolder>/.trash/`).
2. **If everything is `in sync`,** say so and stop.
3. **Confirm** with the user before applying — call out destructive deletions explicitly.
   Never auto-resolve `CONFLICT` rows; ask which side to keep.
4. **Apply:** on approval, re-run with `--direction up --apply --confirm-deletions`.
   - To resolve conflicts the user decided on, add `--prefer local` or `--prefer folder`.
   - Without `--confirm-deletions`, deletions are skipped (safe default).
5. **Secrets:** by default `.env` files are **not** pushed (a cloud folder is a different
   exposure than a private SAN). If the user explicitly wants to push secrets, add
   `--push-secrets`, and remind them their transport's security applies. Never print
   secret contents.
6. **Report** what was copied/deleted and where the trash went. Remind the user to run
   `/sync-env-down` on their other machine to pull these changes.

## First sync

If this machine is new to the folder, the preview is a full-environment analysis. Walk the
user through the notable differences before applying; on a first sync no deletions occur.
