# Installing claude-sync-by-skill — instructions for an AI agent

You are an AI coding agent (e.g. Claude Code) that the user asked to install this skill
from its repository: `https://github.com/JonMcMillan/claude-sync-by-skill`.

Follow these steps on the user's machine. Be transparent about each action, and use the
platform-appropriate Python (`python3` on macOS/Linux; `py -3` or `python` on Windows).

## Prerequisites

- **git** and **Python 3.8+**. Check both first. If either is missing, stop and tell the
  user to install it. The engine itself needs no pip packages (pure standard library).

## Steps

1. **Find the Claude skills directory** — default `<home>/.claude/skills`
   (e.g. `~/.claude/skills`). Call it `<skills>`.

2. **Place the engine** at `<skills>/sync-envs`:
   - If `<skills>/sync-envs` already exists **and is a git clone of this repo**, update it:
     `git -C "<skills>/sync-envs" pull --ff-only`
   - If it exists but is **not** this repo (e.g. an older/different `sync-envs` skill),
     **stop and ask the user to back it up / move it first** — do not overwrite it.
   - Otherwise clone it:
     `git clone https://github.com/JonMcMillan/claude-sync-by-skill.git "<skills>/sync-envs"`

3. **Create the wrapper skills.** The simplest way is to run the bundled installer, which
   clones (if needed) and materializes the wrappers:
   `python3 "<skills>/sync-envs/install.py" --skills-dir "<skills>" --no-setup`
   It creates `<skills>/sync-env-up/SKILL.md` and `<skills>/sync-env-down/SKILL.md` from the
   templates in `<skills>/sync-envs/wrappers/`. (You may also copy those two files by hand.)

4. **Ask the user for their SYNC FOLDER** — the folder they will mirror between machines
   (Google Drive, OneDrive, Dropbox, iCloud, Syncthing, a USB stick, a personal/company
   SAN or UNC share, …). This tool does **not** mirror the folder; the user's own tool
   does. If they don't have one yet, help them pick a path.

5. **Run setup (non-interactive):**
   `python3 "<skills>/sync-envs/sync_engine.py" --setup --sync-root "<the folder>"`
   - Add `--device "<name>"` to override the device id (defaults to the hostname).
   - If it reports the folder *has files but is not a recognized claude-sync folder*,
     confirm with the user, then re-run adding `--yes` to initialize it there.
   - On the **first** machine this creates the folder's identity; on **later** machines it
     joins the existing folder.

6. **Tell the user to reload/restart Claude Code** so the new slash commands appear:
   `/sync-envs` (read-only status), `/sync-env-up`, `/sync-env-down`. Newly added skills are
   not picked up by the client's slash menu until it reloads.

7. **Recommend the next action:**
   - First/primary machine → run `/sync-env-up` to push the environment to the folder.
   - A new machine joining → run `/sync-env-down`; the first sync analyzes the whole
     environment and performs **no deletions**, keeping local-only work.

## Important

- Do **not** pass `--push-secrets` unless the user explicitly wants `.env` secret files
  pushed to the folder (exposure depends on their chosen transport).
- The engine never uploads anywhere itself and bundles no credentials; it only does file
  operations against the chosen folder.
- Everything is previewed before changes; deletions go to `<syncFolder>/.trash/` and are
  confirmed first.
