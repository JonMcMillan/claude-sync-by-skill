# claude-sync-by-skill

**Unify your Claude Code environment across machines — memory, plans, settings, secrets,
and your skills — using just three skill commands.**

You work on one machine, run **`/sync-env-up`** to push your environment to a shared
**sync folder**, then run **`/sync-env-down`** on another machine to bring it over. The
two machines stay one unified environment. **`/sync-envs`** shows a read-only preview of
what would change, in either direction, without touching anything.

> **What it does *not* do:** it does **not** sync the contents of your project folders.
> Your code, files, repos, and `node_modules` are never touched — that's what git is for.
> This skill syncs only *Claude's* knowledge of a project (memory and transcripts), plus
> your plans, settings, and skills — never the project itself.

> **Status:** early, private development. Cross-platform (Windows/macOS/Linux), pure
> Python 3 standard library, no dependencies.

---

## The idea: point it at a folder, sync that folder however you like

The engine is **transport-agnostic**. It only does file operations against a single
**sync folder** that *you* choose at setup. How that folder gets mirrored between your
machines is entirely up to you:

- a cloud drive — Google Drive, OneDrive, Dropbox, iCloud
- a peer-to-peer tool — Syncthing, Resilio
- a USB stick you carry between machines
- a personal or company **SAN / network share** (for a fully private, high-security setup)
- `rsync`/`scp` to your own server

The tool never assumes a backend, never uploads anywhere on its own, and bundles no
credentials. Your choice of folder *is* your choice of security and transport.

---

## How it works

There is **one unified environment**. The sync folder holds the shared canonical copy;
each machine holds a working copy. Three commands:

| Command | Direction | What it does |
|---|---|---|
| `/sync-env-up` | local → folder | Push your current machine's environment to the sync folder (overwrite/update the unified copy), with version + deletion checks. |
| `/sync-env-down` | folder → local | Bring the unified environment onto this machine (overwrite/update local). |
| `/sync-envs` | — | Read-only preview of what up *would* push and down *would* pull, plus the device roster. Changes nothing. |

What's unified: **memory** (incl. session transcripts — resume a conversation on any
machine), **plans**, **settings**, your **skills**, and (opt-in) **secrets**. The sync
tool's *own* skill folders are the only thing managed by git instead — see *Updates*.

### Your main working folder (different paths on different machines)

Claude ties a project's memory to the **folder you work in** — the absolute path becomes
the project's identity (`D:\dev` → `D--dev`, `C:\dev` → `C--dev`). So the *same* project
worked at different paths on two machines would otherwise look like two unrelated
projects and never line up.

To fix this, at setup you name your **main working folder**: the parent your projects
live under (e.g. `C:\dev`). Each machine can use its own path — a different drive **or a
different folder name** — and the tool keeps a per-machine map so the folder holds one
canonical copy while each machine materializes it under the key its own paths produce.
New projects you create under that folder are matched automatically; you never register
them by hand.

Because the working folder doesn't need to exist to *receive* a sync (memory lives under
`~/.claude`, separate from your code), after a `down` the tool prints the **exact local
path** to open for each synced project — so you never mistype a folder name to "find" your
context. `/sync-env-down --scaffold` pre-creates those empty folders for you. Naming a main
working folder is optional; skip it and every project syncs under its literal per-machine
key, exactly as before.

### Tool-install reminders

When you `sync-env-down`, the engine scans the transcripts it just pulled for evidence
that a **CLI or tool was installed during sessions on another machine** — both commands
actually run (`npm i -g`, `winget install`, `brew install`, `gh extension install`, …) and
your own notes about installing something manually in a terminal. If it finds any that
**aren't already on this machine's PATH**, it prints a short, advisory reminder so you can
install the same here. It's a best-effort nudge from data already synced — never an
automated install — and only looks at the transcripts pulled that run, so it won't re-nag.
Suppress it with `--no-tool-hints`.

### Git-pull reminders

`sync-env-down` syncs *Claude's* environment, not your code — so after a down, the engine
also checks whether your **project repos** are behind their git remote (someone committed
or pushed from another machine). For each registered project folder that's a git repo it
does a best-effort `git fetch`, and if your current branch is behind its upstream it prints
a one-line nudge with the exact `git -C … pull --ff-only` command. It **never pulls for
you**, warns when a repo has uncommitted changes, and skips repos with no upstream or no
network. Suppress it with `--no-git-hints`.

### Safety: overwrites and deletions are intentional and reviewed

Syncing means overwriting and propagating deletions — but never by accident:

- **Direction declares the source of truth.** `up` trusts your local machine; `down`
  trusts the folder. No guessing who's authoritative.
- **A per-machine baseline** records what *you* last reconciled with the folder. A file
  that's in the folder but **not in your baseline** is something another machine added
  that you simply haven't pulled yet — it is **never** mistaken for a deletion.
- **Everything is previewed first.** Every run prints a summary; any deletion is flagged
  as destructive and requires confirmation. **Conflict detection is three-way:** a
  `CONFLICT` is raised only when *both* sides changed since you last reconciled, and is
  never auto-resolved — you decide. A file changed on only *one* side (e.g. the session
  transcript you're actively writing, which is always newer locally) is shown as *"newer
  local"* / *"newer in folder"* and kept — it is **not** flagged as a conflict.
- **Deletions go to trash**, under `<syncFolder>/.trash/<timestamp>/`, and are
  recoverable.

### First sync (joining a folder)

The first time a machine points at a populated sync folder, the engine **analyzes the
entire environment first** for full context, then walks **item by item**, showing each
difference and asking whether to keep your local copy, take the folder's copy, or skip.
That's also where you can exclude anything you don't want unified. After that, ongoing
`up`/`down` use the quick summary-and-confirm flow.

---

## Install

Requires **Python 3.8+** and **git**.

```bash
python3 install.py
```

The installer clones this repo into your Claude skills directory, runs setup (where you
pick the sync folder and join/initialize it), and materializes the `sync-env-up` /
`sync-env-down` skills. Then, in Claude Code, run `/sync-env-down` (or `/sync-env-up` on
your first/primary machine) to begin.

### Install with Claude (AI-assisted)

You don't have to run anything yourself — just ask your AI agent. In Claude Code, say:

> Install the skill from https://github.com/JonMcMillan/claude-sync-by-skill — follow its AI-INSTALL.md.

Claude reads [`AI-INSTALL.md`](AI-INSTALL.md), checks prerequisites, clones the repo into
your skills folder, creates the wrapper skills, asks you for your sync folder, and runs
setup (non-interactively via `--sync-root`). Reload Claude Code afterward so the new slash
commands appear.

For a scripted one-shot install yourself:

```bash
python3 install.py --sync-root "/path/to/your/sync/folder"
```

---

## Updates

Only the **sync tool's own skill folders** (`sync-envs`, `sync-env-up`, `sync-env-down`)
are managed by git — so every machine runs the same engine version, and the tool's `.git`
never ends up inside your cloud-mirrored folder. Each run checks GitHub and, if a newer
version exists, offers to `git pull` before continuing. Everything *else* about your
environment — including all your other skills — syncs through the sync folder.

---

## Security

- **Pure Python standard library — zero third-party dependencies**, so there is no
  dependency supply chain to trust.
- The engine **runs from its git-managed folder, never from the sync folder** — synced
  data is only ever *copied*, never executed.
- External commands (only `git`) are invoked with argument lists, never a shell.
- State is plain JSON (no `pickle`/`eval`).
- **Secrets** (`.env` files) are never printed; pushing them to the folder is **opt-in**
  with a warning (because exposure depends on *your* transport), and pulled secrets are
  written with `0600` permissions on POSIX.
- State writes are atomic; symlinks are skipped by default.

---

## Offered to Anthropic / prior art

Native cross-machine sync for Claude Code is a long-standing, popular request
([anthropics/claude-code#22648](https://github.com/anthropics/claude-code/issues/22648),
the dedup target for #6037, #19634, #13461, #12119, #57678). Its proposed
`sync push` / `sync pull` / `sync status` UX matches this tool's up/down/status model.
This project is released under **Apache-2.0** and **offered freely for Anthropic** (or
anyone) to adopt, adapt, or use as a reference implementation. The goal is simply to help
others with the same problem.

## License

Completely free to use! If you'd like to support me, you can buy me a coffee at https://buymeacoffee.com/jonmcmillan.

[Apache License 2.0](LICENSE).
