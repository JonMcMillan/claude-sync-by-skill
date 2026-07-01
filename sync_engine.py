#!/usr/bin/env python3
"""
claude-sync-by-skill — engine.

Unify one Claude Code environment (memory, plans, settings, secrets, skills) across
machines through a single user-chosen *sync folder*. Transport-agnostic: the engine only
does file operations against the sync folder; mirroring that folder between machines
(Drive, OneDrive, Dropbox, iCloud, Syncthing, USB, SAN, rsync, ...) is the user's choice.

Model — one unified environment, two directional commands plus a read-only status:
  up    : local  -> sync folder  (local is authoritative)
  down  : sync folder -> local   (the folder is authoritative)
  status: read-only preview of what up/down would do (default; no changes)

Safety — overwrites and deletions are intentional and reviewed:
  * direction declares the source of truth (no two-way guessing);
  * a PER-MACHINE local baseline distinguishes "I deleted this" from "another machine
    added this that I haven't pulled yet" (the latter is never deleted);
  * everything is previewed first; deletions go to <syncRoot>/.trash/ and require
    explicit confirmation; conflicts are never auto-resolved.

Pure Python 3.8+ standard library. No third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.1.0"
SCHEMA_VERSION = 2
MTIME_TOLERANCE = 2.0  # seconds; FAT/network-drive slack
SYNC_FOLDER_TYPE = "claude-sync-by-skill"

# The sync tool's own skill folders — always excluded from the synced skills set
# (git-managed; keeps every machine on one engine version and keeps .git out of the
# cloud-mirrored folder).
TOOL_SKILL_FOLDERS = ["sync-envs", "sync-env-up", "sync-env-down"]

# Names never enumerated as syncable content.
ALWAYS_SKIP_NAMES = {
    ".git",
    ".sync-config.json",
    ".sync-baseline.json",
    ".sync-state.json",
    ".claude-sync.json",
    ".trash",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def detect_os() -> str:
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def hostname() -> str:
    return socket.gethostname() or "unknown-host"


def read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, str(path))  # atomic on the same filesystem
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def mtime_of(p: Path) -> float:
    return p.stat().st_mtime


def is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not is_tty():
        return False
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ask(prompt: str, default: str = "") -> str:
    try:
        ans = input(prompt).lstrip(chr(0xFEFF)).strip()  # tolerate a stray BOM on stdin
    except EOFError:
        ans = ""
    return ans or default


def within_root(root: Path, candidate: Path) -> bool:
    """Path-containment guard: candidate must resolve inside root."""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# --------------------------------------------------------------------------- #
# Logical project roots (per-machine path aliasing)
# --------------------------------------------------------------------------- #
# Claude Code keys each project's memory/transcripts by the sanitized absolute
# working-directory path (D:\dev -> "D--dev", C:\dev -> "C--dev"). Two machines
# working the "same" project at different paths therefore get different keys and
# never line up. A per-machine *root map* (localPrefix -> canonicalPrefix) lets the
# folder store one canonical tree while each machine materializes it under the key
# its own cwd will produce. Empty map => identity => byte-for-byte legacy behavior.
def sanitize_path_to_key(path) -> str:
    """Approximate Claude Code's project-key derivation: the working path with path
    punctuation (\\ / : . and spaces) flattened to '-'. Used only to derive the local
    key PREFIX for a configured work root; exact subpaths come from the captured cwd
    (see read_project_cwd), never by reversing a lossy key."""
    return "".join("-" if c in "\\/:. " else c for c in str(path))


def remap_first_segment(rel: str, mapping: "dict[str, str]") -> str:
    """Rewrite the first path segment (the project key) of a POSIX relpath via
    `mapping` (prefix -> prefix), matched on a segment boundary so 'D--dev' rewrites
    'D--dev' and 'D--dev-app' but never 'D--development'. Longest prefix wins. Returns
    `rel` unchanged when nothing matches."""
    if not mapping:
        return rel
    projkey, sep, rest = rel.partition("/")
    for src, dst in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        if projkey == src:
            return dst + sep + rest
        if projkey.startswith(src + "-"):
            return dst + projkey[len(src):] + sep + rest
    return rel


def build_root_map(cfg: dict) -> "dict[str, str]":
    """The local->canonical project-key map for this machine, or {} (identity)."""
    work_key = cfg.get("workKey")
    canonical = cfg.get("canonicalKey")
    if work_key and canonical:
        return {work_key: canonical}
    return {}


# --------------------------------------------------------------------------- #
# Config / baseline / definition / provenance
# --------------------------------------------------------------------------- #
def config_path(claude_home: Path) -> Path:
    return claude_home / ".sync-config.json"


def baseline_path(claude_home: Path) -> Path:
    return claude_home / ".sync-baseline.json"


def load_config(claude_home: Path):
    return read_json(config_path(claude_home))


def save_config(cfg: dict) -> None:
    atomic_write_json(config_path(Path(cfg["claudeHome"])), cfg)


def load_baseline(claude_home: Path) -> dict:
    data = read_json(baseline_path(claude_home)) or {}
    return data.get("baseline", {}) if isinstance(data, dict) else {}


def save_baseline(claude_home: Path, baseline: dict) -> None:
    atomic_write_json(
        baseline_path(claude_home),
        {"schemaVersion": SCHEMA_VERSION, "baseline": baseline},
    )


def definition_path(sync_root: Path) -> Path:
    return sync_root / ".claude-sync.json"


def load_definition(sync_root: Path):
    return read_json(definition_path(sync_root))


def save_definition(sync_root: Path, definition: dict) -> None:
    atomic_write_json(definition_path(sync_root), definition)


def state_path(sync_root: Path) -> Path:
    return sync_root / ".sync-state.json"


def load_state(sync_root: Path) -> dict:
    data = read_json(state_path(sync_root)) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("files", {})
    return data


def save_state(sync_root: Path, state: dict, device: str) -> None:
    state["lastSync"] = now_iso()
    state["machine"] = device
    # Drop any legacy deletion-driving fields — provenance is display-only now.
    state.pop("tombstones", None)
    atomic_write_json(state_path(sync_root), state)


# --------------------------------------------------------------------------- #
# Environment detection
# --------------------------------------------------------------------------- #
def default_claude_home() -> Path:
    return Path.home() / ".claude"


def detect_dev_root() -> "Path | None":
    if os.environ.get("SYNC_TEST_DEVROOT"):
        return Path(os.environ["SYNC_TEST_DEVROOT"])
    for cand in (Path("C:/dev"), Path("D:/dev"), Path.home() / "dev"):
        if (cand / ".claude").exists():
            return cand
    return None


def suggest_sync_roots() -> "list[Path]":
    """Setup-time only: best-effort candidate sync folders for the running OS."""
    cands: list[Path] = []
    home = Path.home()
    system = detect_os()

    def add(p: Path):
        if p not in cands:
            cands.append(p)

    if system == "windows":
        import string

        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:/")
            if not drive.exists():
                continue
            for sub in ("My Drive", "Google Drive", "OneDrive", "Dropbox"):
                add(drive / sub)
        if os.environ.get("OneDrive"):
            add(Path(os.environ["OneDrive"]))
        if os.environ.get("OneDriveCommercial"):
            add(Path(os.environ["OneDriveCommercial"]))
    elif system == "macos":
        cloud = home / "Library" / "CloudStorage"
        if cloud.exists():
            for child in cloud.iterdir():
                add(child)
        add(home / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
        add(home / "Dropbox")
    else:  # linux
        for sub in ("GoogleDrive", "Google Drive", "OneDrive", "Dropbox", "Sync"):
            add(home / sub)

    return [c for c in cands if c.exists()]


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
class Item:
    def __init__(self, item_id, local, sync, kind, secret=False, exclude=None,
                 rootmap=None):
        self.id = item_id
        self.local = Path(local)
        self.sync = Path(sync)
        self.kind = kind  # "dir" | "file"
        self.secret = secret
        self.exclude = set(exclude or [])
        # local->canonical project-key map; non-empty only for the memory item.
        self.rootmap = dict(rootmap or {})

    def to_canon(self, rel: str) -> str:
        """Local relpath -> canonical relpath (as stored in the sync folder)."""
        return remap_first_segment(rel, self.rootmap) if self.rootmap else rel

    def to_local(self, rel: str) -> str:
        """Canonical relpath -> this machine's local relpath."""
        if not self.rootmap:
            return rel
        inverse = {v: k for k, v in self.rootmap.items()}
        return remap_first_segment(rel, inverse)


def build_manifest(cfg: dict) -> "list[Item]":
    home = Path(cfg["claudeHome"])
    sync = Path(cfg["syncRoot"])
    dev_root = cfg.get("devRoot")
    skill_exclude = set(cfg.get("skillExclude", TOOL_SKILL_FOLDERS))

    items = [
        Item("claude-skills", home / "skills", sync / "claude-skills", "dir",
             exclude=skill_exclude),
        Item("claude-memory", home / "projects", sync / "claude-memory", "dir",
             rootmap=build_root_map(cfg)),
        Item("claude-plans", home / "plans", sync / "claude-plans", "dir"),
        Item("claude-settings", home / "settings.json",
             sync / "claude-settings" / "settings.json", "file"),
    ]
    if dev_root:
        dev = Path(dev_root)
        items.append(Item("infra-env", dev / ".claude" / ".env",
                          sync / "infra-env" / ".env", "file", secret=True))
        items.append(Item("env-backup", dev / ".claude" / "env-backup",
                          sync / "env-backup", "dir", secret=True))
    return items


def enumerate_side(base: Path, kind: str, exclude: "set[str]") -> "dict[str, Path]":
    """relpath(POSIX) -> file Path, skipping symlinks, excluded top-level names, and
    always-skip names. Errors on individual files are ignored (best-effort)."""
    out: dict[str, Path] = {}
    if kind == "file":
        if base.exists() and not base.is_symlink():
            out[base.name] = base
        return out
    if not base.exists():
        return out
    for root, dirs, files in os.walk(base, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(base)
        top = rel_root.parts[0] if rel_root.parts else ""
        # prune excluded / skipped directories in-place
        dirs[:] = [
            d for d in dirs
            if d not in ALWAYS_SKIP_NAMES
            and not (rel_root == Path(".") and d in exclude)
        ]
        if top and top in exclude:
            continue
        for name in files:
            if name in ALWAYS_SKIP_NAMES:
                continue
            fp = root_path / name
            try:
                if fp.is_symlink():
                    continue
                rel = fp.relative_to(base).as_posix()
            except OSError:
                continue
            out[rel] = fp
    return out


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
class Action:
    def __init__(self, item, relpath, regkey, kind, local_path, sync_path,
                 local_mtime, sync_mtime, secret):
        self.item = item
        self.relpath = relpath
        self.regkey = regkey
        self.kind = kind  # see classify() return values
        self.local_path = local_path
        self.sync_path = sync_path
        self.local_mtime = local_mtime
        self.sync_mtime = sync_mtime
        self.secret = secret


# action kinds that actually mutate something
MUTATING = {"copy_up", "copy_down", "del_upstream", "del_local"}
DESTRUCTIVE = {"del_upstream", "del_local"}
# no-op kinds that are not worth flagging in the action table (one side is simply ahead)
QUIET = {"in_sync", "ahead_local", "ahead_folder"}


def classify(direction, has_local, has_sync, lm, sm, in_baseline, bm):
    """Decide the action for one file.

    bm = the file's mtime when it was last reconciled (from the baseline), or None when
    that is unknown (never reconciled, or a legacy baseline that only recorded a sync
    timestamp). in_baseline = whether the file was present in the baseline at all (used
    for deletion detection).

    Conflict is THREE-WAY: a CONFLICT only when BOTH sides changed since the baseline.
    If only one side moved, that side is simply "ahead" -- e.g. the live session
    transcript is newer locally every sync, which is expected, not a conflict.
    """
    tol = MTIME_TOLERANCE
    if has_local and has_sync:
        if abs(lm - sm) <= tol:
            return "in_sync"
        # present on both sides but differing content
        if bm is None:
            # unknown reconciled state (new-on-both, or legacy baseline) -> two-way
            # fallback by direction (self-heals once a real mtime is recorded)
            if direction == "up":
                return "copy_up" if lm > sm else "conflict"
            return "copy_down" if sm > lm else "conflict"
        local_changed = abs(lm - bm) > tol
        folder_changed = abs(sm - bm) > tol
        if local_changed and folder_changed:
            return "conflict"                       # genuine divergence
        if direction == "up":
            return "copy_up" if local_changed else "ahead_folder"
        return "copy_down" if folder_changed else "ahead_local"
    if has_local and not has_sync:
        if direction == "up":
            return "copy_up"  # new locally, or resurrect (my machine is authoritative)
        return "del_local" if in_baseline else "keep_local_new"
    if has_sync and not has_local:
        if direction == "up":
            return "del_upstream" if in_baseline else "leave_upstream"
        return "copy_down"  # new upstream, or resurrect
    return "absent"


def compute_plan(direction, cfg, baseline) -> "list[Action]":
    actions: list[Action] = []
    for item in build_manifest(cfg):
        local = enumerate_side(item.local, item.kind, item.exclude)
        upstream = enumerate_side(item.sync, item.kind, item.exclude)
        # Pair the two sides in the CANONICAL namespace. The folder side is already
        # canonical; local relpaths are mapped through the root map (identity for every
        # item except memory, and for memory too when no root map is configured).
        local_by_canon = {item.to_canon(rel): rel for rel in local}
        for canon in sorted(set(local_by_canon) | set(upstream)):
            regkey = "{}/{}".format(item.id, canon)
            has_local = canon in local_by_canon
            has_sync = canon in upstream
            local_rel = local_by_canon[canon] if has_local else item.to_local(canon)
            lm = mtime_of(local[local_rel]) if has_local else None
            sm = mtime_of(upstream[canon]) if has_sync else None
            # Canonical destination paths, valid whether or not the side has the file yet.
            if item.kind == "file":
                lp_canon, sp_canon = item.local, item.sync
            else:
                lp_canon, sp_canon = item.local / local_rel, item.sync / canon
            bval = baseline.get(regkey)
            bm = float(bval) if isinstance(bval, (int, float)) else None
            kind = classify(direction, has_local, has_sync,
                            lm or 0.0, sm or 0.0, regkey in baseline, bm)
            actions.append(Action(item.id, canon, regkey, kind, lp_canon, sp_canon,
                                  lm, sm, item.secret))
    return actions


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
LABELS = {
    "copy_up": "-> folder (push)",
    "copy_down": "<- local (pull)",
    "del_upstream": "trash+DELETE folder",
    "del_local": "trash+DELETE local",
    "conflict": "CONFLICT",
    "in_sync": "in sync",
    "ahead_local": "newer local (kept; pushes on up)",
    "ahead_folder": "newer in folder (kept; pull with down)",
    "keep_local_new": "keep (new local, unpushed)",
    "leave_upstream": "leave (added elsewhere)",
    "absent": "absent",
}


def summarize(actions):
    counts = {}
    for a in actions:
        if a.kind in QUIET:
            continue
        counts[a.kind] = counts.get(a.kind, 0) + 1
    return counts


def render_plan(direction, cfg, actions, first_sync):
    lines = []
    interesting = [a for a in actions if a.kind not in QUIET]
    ahead_l = sum(1 for a in actions if a.kind == "ahead_local")
    ahead_f = sum(1 for a in actions if a.kind == "ahead_folder")

    def ahead_notes():
        ns = []
        if ahead_l:
            ns.append("note: {} file(s) newer locally (kept, not a conflict; will push "
                      "on the next up-sync).".format(ahead_l))
        if ahead_f:
            ns.append("note: {} file(s) newer in the folder (kept; pull them with a "
                      "down-sync).".format(ahead_f))
        return ns

    header = "sync-env-{}  (device: {}  syncRoot: {})".format(
        direction, cfg["device"], cfg["syncRoot"])
    lines.append(header)
    if first_sync:
        lines.append("FIRST SYNC for this machine - analyzing the full environment; "
                     "no deletions will occur on a first sync.")
    if not interesting:
        lines.extend(ahead_notes() or ["Everything is in sync. Nothing to do."])
        return "\n".join(lines)

    width = max((len(a.regkey) for a in interesting), default=10)
    width = min(width, 80)
    for a in interesting:
        lines.append("  {:<{w}}  {}".format(a.regkey[:width], LABELS[a.kind], w=width))

    counts = summarize(actions)
    lines.append("")
    lines.append("Summary: " + ", ".join(
        "{} {}".format(v, LABELS[k]) for k, v in sorted(counts.items())))
    secret_pushes = [a for a in interesting
                     if a.kind == "copy_up" and a.secret]
    if secret_pushes and not cfg.get("pushSecrets"):
        lines.append("NOTE: {} secret file(s) would be pushed but are SKIPPED "
                     "(pushSecrets is off).".format(len(secret_pushes)))
    dels = [a for a in interesting if a.kind in DESTRUCTIVE]
    if dels:
        lines.append("WARNING: {} destructive deletion(s) - recoverable under "
                     "<syncRoot>/.trash/.".format(len(dels)))
    lines.extend(ahead_notes())
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #
def copy_preserve(src: Path, dst: Path, secret: bool):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    if secret and os.name == "posix":
        try:
            os.chmod(str(dst), 0o600)
        except OSError:
            pass


def move_to_trash(path: Path, regkey: str, trash_root: Path):
    dest = trash_root / regkey
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(path), str(dest))
    path.unlink()


def apply_plan(direction, cfg, actions, baseline, state, confirm_deletions,
               prefer, push_secrets):
    sync_root = Path(cfg["syncRoot"])
    trash_root = sync_root / ".trash" / run_stamp()
    device = cfg["device"]
    files = state["files"]
    applied = {"copied": 0, "deleted": 0, "conflicts": 0, "skipped": 0}

    for a in actions:
        kind = a.kind

        # Resolve conflicts if a preference was given (direction-independent:
        # prefer local -> the folder receives the local copy; prefer folder -> the
        # local receives the folder copy). Otherwise leave the conflict untouched.
        if kind == "conflict":
            if prefer == "local":
                kind = "copy_up"
            elif prefer == "folder":
                kind = "copy_down"
            else:
                applied["conflicts"] += 1
                continue

        if kind == "copy_up":
            if a.secret and not push_secrets:
                applied["skipped"] += 1
                continue
            copy_preserve(a.local_path, a.sync_path, a.secret)
            baseline[a.regkey] = mtime_of(a.sync_path)  # reconciled mtime (both sides now match)
            files[a.regkey] = {"host": device, "mtime": now_iso()}
            applied["copied"] += 1

        elif kind == "copy_down":
            copy_preserve(a.sync_path, a.local_path, a.secret)
            baseline[a.regkey] = mtime_of(a.local_path)  # reconciled mtime
            files.setdefault(a.regkey, {"host": "unknown", "mtime": now_iso()})
            applied["copied"] += 1

        elif kind in DESTRUCTIVE:
            if not confirm_deletions:
                applied["skipped"] += 1
                continue
            target = a.sync_path if kind == "del_upstream" else a.local_path
            if target and target.exists():
                move_to_trash(target, a.regkey, trash_root)
            baseline.pop(a.regkey, None)
            files.pop(a.regkey, None)
            applied["deleted"] += 1

        elif kind == "in_sync":
            # record the reconciled mtime (upgrades legacy timestamp entries too) so
            # future syncs can do three-way change detection
            baseline[a.regkey] = mtime_of(a.local_path)

        # ahead_local / ahead_folder / keep_local_new / leave_upstream / absent -> no-op

    save_baseline(Path(cfg["claudeHome"]), baseline)
    save_state(sync_root, state, device)
    touch_device(sync_root, cfg)
    return applied, trash_root


# --------------------------------------------------------------------------- #
# Tool-install hints (advisory)
# --------------------------------------------------------------------------- #
# On `down`, scan the transcripts just pulled from the folder for evidence that a CLI/tool
# was installed during sessions on another machine, and surface a soft reminder so the user
# can install the same here. Two signals, both from data we already sync:
#   * commands actually run in Bash/PowerShell tool calls (high confidence), and
#   * the USER's own text where they mention a manual terminal install (lower confidence).
# Assistant prose is deliberately NOT scanned — it merely *discusses* installs and would
# produce false positives. This is a best-effort nudge, never an automated install.
TOOL_COMMAND_TOOLS = {"Bash", "PowerShell"}

# package-manager invocations -> the (?P<pkg>...) being installed
_PKG_PATTERNS = [
    (r"npm\s+(?:i|install|add)\b[^\n;|&]*?(?:-g|--global)\s+(?P<pkg>[@\w][\w@/.\-]*)",
     "npm -g"),
    (r"pnpm\s+(?:add|install)\b[^\n;|&]*?(?:-g|--global)\s+(?P<pkg>[@\w][\w@/.\-]*)",
     "pnpm -g"),
    (r"yarn\s+global\s+add\s+(?P<pkg>[@\w][\w@/.\-]*)", "yarn global"),
    (r"winget\s+install\s+(?:--id\s+)?(?P<pkg>[\w][\w.\-]*)", "winget"),
    (r"(?:choco|scoop)\s+install\s+(?P<pkg>[\w][\w.\-]*)", "choco/scoop"),
    (r"brew\s+install\s+(?P<pkg>[@\w][\w@/.\-]*)", "brew"),
    (r"pipx\s+install\s+(?P<pkg>[\w][\w.\-]*)", "pipx"),
    (r"cargo\s+install\s+(?P<pkg>[\w][\w.\-]*)", "cargo"),
    (r"uv\s+tool\s+install\s+(?P<pkg>[\w][\w.\-]*)", "uv tool"),
    (r"go\s+install\s+(?P<pkg>[@\w][\w@/.\-]*)", "go install"),
    (r"gh\s+extension\s+install\s+(?P<pkg>[@\w][\w@/.\-]*)", "gh extension"),
    (r"(?:sudo\s+)?(?:apt|apt-get|dnf|yum)\s+install\s+(?:-y\s+)?(?P<pkg>[\w][\w.+\-]*)",
     "apt/dnf"),
]
# user prose: "...installed the wrangler CLI..." (only with a terminal/manual cue nearby)
_NL_PATTERN = r"\binstalled\s+(?:the\s+)?(?P<pkg>[A-Za-z][\w.\-]{1,})(?:\s+CLI)?\b"
_TERMINAL_CUES = ("terminal", "manually", "command line", "command-line", "powershell",
                  "my shell", "outside claude", "outside of claude", "globally")
# tokens that are never a tool worth flagging
_TOOL_STOPWORDS = {
    "the", "it", "them", "this", "that", "all", "everything", "stuff", "again",
    "dependencies", "dependency", "deps", "packages", "package", "modules", "module",
    "node", "npm", "pip", "python", "tool", "tools", "cli", "my", "your", "locally",
    "globally", "latest", "version", "and", "a", "an", "some", "new",
}


_PKG_COMPILED = [(re.compile(rx, re.I), label) for rx, label in _PKG_PATTERNS]
_NL_COMPILED = re.compile(_NL_PATTERN, re.I)
_TOOL_TOKEN = re.compile(r"^[@A-Za-z0-9][\w@/.\-]*$")


def _looks_like_tool(pkg: str) -> bool:
    if not pkg or len(pkg) < 2 or pkg.lower() in _TOOL_STOPWORDS:
        return False
    return bool(_TOOL_TOKEN.match(pkg))


def _binary_guess(pkg: str) -> str:
    name = pkg.rsplit("/", 1)[-1].lstrip("@")
    return name.split("@", 1)[0] or pkg


def _tool_present(pkg: str) -> bool:
    """Best-effort: is this tool already on PATH here? (package name ~ binary name)."""
    return shutil.which(_binary_guess(pkg)) is not None


def detect_install_hints_in_text(text: str, source: str):
    """Yield (tool, label, evidence) install hints found in one command/text string."""
    for rx, label in _PKG_COMPILED:
        for m in rx.finditer(text):
            pkg = m.group("pkg")
            if _looks_like_tool(pkg):
                yield (pkg, label, m.group(0).strip()[:80])
    if source == "user-text" and any(cue in text.lower() for cue in _TERMINAL_CUES):
        for m in _NL_COMPILED.finditer(text):
            pkg = m.group("pkg")
            if _looks_like_tool(pkg):
                yield (pkg, "you mentioned", m.group(0).strip()[:80])


def _iter_scannable_parts(obj):
    """Yield (source, text) from a transcript line: ('command', cmd) for Bash/PowerShell
    tool calls, ('user-text', text) for user-authored prose. Assistant prose is skipped."""
    if not isinstance(obj, dict) or obj.get("type") not in ("user", "assistant"):
        return
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if isinstance(content, str):
        if obj.get("type") == "user":
            yield ("user-text", content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use" and block.get("name") in TOOL_COMMAND_TOOLS:
            cmd = (block.get("input") or {}).get("command")
            if isinstance(cmd, str):
                yield ("command", cmd)
        elif btype == "text" and obj.get("type") == "user":
            t = block.get("text")
            if isinstance(t, str):
                yield ("user-text", t)


def scan_transcript_for_install_hints(path: Path):
    hints = []
    try:
        with Path(path).open("r", encoding="utf-8-sig") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for source, text in _iter_scannable_parts(obj):
                    hints.extend(detect_install_hints_in_text(text, source))
    except OSError:
        pass
    return hints


def gather_install_hints(paths, max_files: int = 500):
    """Dedup install hints across the given transcripts, dropping tools already on PATH.
    Returns a list of {tool, via, evidence} plus a flag if file scanning was capped."""
    seen = {}
    capped = len(list(paths)) > max_files
    for p in list(paths)[:max_files]:
        for pkg, via, evidence in scan_transcript_for_install_hints(p):
            key = pkg.lower()
            if key in seen or _tool_present(pkg):
                continue
            seen[key] = {"tool": pkg, "via": via, "evidence": evidence}
    return list(seen.values()), capped


def render_install_hints(hints, capped: bool) -> str:
    if not hints:
        return ""
    out = [
        "",
        "It looks like some tools were installed during your previous sessions on",
        "another machine. Based on what I could find, you may need to install these here:",
    ]
    for h in sorted(hints, key=lambda x: x["tool"].lower()):
        out.append("  - {:<22} ({}: {})".format(h["tool"], h["via"], h["evidence"]))
    out.append("(best-effort scan of synced transcripts - verify before installing; "
               "some may not apply.)")
    if capped:
        out.append("(note: only the first batch of transcripts was scanned.)")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Definition file / device roster
# --------------------------------------------------------------------------- #
def touch_device(sync_root: Path, cfg: dict):
    definition = load_definition(sync_root)
    if not definition:
        return
    dev = definition.setdefault("devices", {}).setdefault(cfg["device"], {})
    dev["os"] = cfg["os"]
    dev["lastSync"] = now_iso()
    dev["engineVersion"] = VERSION
    dev.setdefault("joinedAt", now_iso())
    save_definition(sync_root, definition)


# --------------------------------------------------------------------------- #
# Main working folder + project registry (true-cwd capture, report, scaffold)
# --------------------------------------------------------------------------- #
def get_main_work_root(sync_root: Path):
    """The canonical main-working-folder record a machine established, or None."""
    return (load_definition(sync_root) or {}).get("mainWorkRoot")


def ensure_main_work_root(sync_root: Path, cfg: dict) -> None:
    """First machine to configure a work root anchors the canonical key for the folder
    and bumps it to schema v2 so stale engines refuse it rather than mis-mapping."""
    if not cfg.get("canonicalKey"):
        return
    definition = load_definition(sync_root)
    if not definition or "mainWorkRoot" in definition:
        return
    definition["mainWorkRoot"] = {
        "canonicalKey": cfg["canonicalKey"],
        "establishedBy": cfg["device"],
        "examplePath": cfg.get("workRoot", ""),
    }
    definition["schemaVersion"] = max(definition.get("schemaVersion", 1), 2)
    save_definition(sync_root, definition)


def read_project_cwd(project_dir: Path):
    """The true absolute cwd recorded in a project's transcripts (Claude Code writes a
    'cwd' field on each event), or None. This is exact — never reverse the lossy key."""
    try:
        jsonls = sorted(project_dir.glob("*.jsonl"))
    except OSError:
        return None
    for jf in jsonls:
        try:
            with jf.open("r", encoding="utf-8-sig") as fh:  # tolerate a leading BOM
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("cwd"):
                        return obj["cwd"]
        except OSError:
            continue
    return None


def _under_root(key: str, root_prefix: str) -> bool:
    return key == root_prefix or key.startswith(root_prefix + "-")


def update_project_registry(cfg: dict) -> None:
    """On `up`: record each local project under the work root into the folder's project
    registry, keyed by canonical project key, storing the cwd's subpath relative to the
    work root so any machine can rebuild the exact local path on `down`."""
    work_root = cfg.get("workRoot")
    work_key = cfg.get("workKey")
    canonical = cfg.get("canonicalKey")
    if not (work_root and work_key and canonical):
        return
    sync_root = Path(cfg["syncRoot"])
    definition = load_definition(sync_root)
    if not definition:
        return
    projects_dir = Path(cfg["claudeHome"]) / "projects"
    if not projects_dir.exists():
        return
    registry = definition.setdefault("projects", {})
    for child in sorted(projects_dir.iterdir()):
        if not child.is_dir() or child.name in ALWAYS_SKIP_NAMES:
            continue
        if not _under_root(child.name, work_key):
            continue  # only projects beneath this machine's work root
        canon_key = remap_first_segment(child.name, {work_key: canonical})
        cwd = read_project_cwd(child)
        subpath = ""
        if cwd:
            try:
                rp = Path(cwd).relative_to(Path(work_root)).as_posix()
                subpath = "" if rp == "." else rp
            except ValueError:
                subpath = ""  # cwd is another machine's path (pulled transcript); can't
                #              localize it here -- fall through to preserve any good value
        # Non-destructive: never overwrite a good subpath/cwd (recorded by the machine that
        # actually works this project) with an empty one derived from a missing or foreign
        # cwd. This both re-fills a stale empty entry and protects another machine's value.
        existing = registry.get(canon_key, {})
        if not subpath and existing.get("subpath"):
            subpath = existing["subpath"]
        if not cwd and existing.get("sourceCwd"):
            cwd = existing["sourceCwd"]
        registry[canon_key] = {
            "subpath": subpath,
            "sourceCwd": cwd or "",
            "updatedBy": cfg["device"],
            "updatedAt": now_iso(),
        }
    save_definition(sync_root, definition)


def local_project_paths(cfg: dict) -> "list[tuple[str, str]]":
    """For `down`: (canonical key, exact local working path) for every registered
    project under this machine's canonical root, rebuilt from work root + stored
    subpath. Empty unless a work root is configured."""
    work_root = cfg.get("workRoot")
    canonical = cfg.get("canonicalKey")
    if not (work_root and canonical):
        return []
    registry = (load_definition(Path(cfg["syncRoot"])) or {}).get("projects", {})
    out = []
    for canon_key, meta in sorted(registry.items()):
        if not _under_root(canon_key, canonical):
            continue
        subpath = meta.get("subpath", "")
        path = Path(work_root) / subpath if subpath else Path(work_root)
        out.append((canon_key, str(path)))
    return out


def init_or_join_folder(sync_root: Path, cfg: dict, assume_yes: bool) -> bool:
    """Returns True if it is OK to proceed, False to abort."""
    definition = load_definition(sync_root)
    if definition:
        if definition.get("type") != SYNC_FOLDER_TYPE:
            print("ERROR: {} exists but is not a claude-sync folder."
                  .format(definition_path(sync_root)))
            return False
        schema = definition.get("schemaVersion", 1)
        if schema > SCHEMA_VERSION:
            print("ERROR: this folder uses schema v{} but the engine understands v{}. "
                  "Run 'git pull' to update the engine first."
                  .format(schema, SCHEMA_VERSION))
            return False
        others = [d for d in definition.get("devices", {}) if d != cfg["device"]]
        print("Joining existing claude-sync folder (schema v{}).".format(schema))
        if others:
            print("  Existing devices: " + ", ".join(sorted(others)))
        definition.setdefault("devices", {})[cfg["device"]] = {
            "os": cfg["os"], "joinedAt": now_iso(),
            "lastSync": "", "engineVersion": VERSION,
        }
        save_definition(sync_root, definition)
        return True

    # No definition file present.
    sync_root.mkdir(parents=True, exist_ok=True)
    non_meta = [p for p in sync_root.iterdir() if p.name not in ALWAYS_SKIP_NAMES]
    if non_meta:
        print("This folder has files but is not a recognized claude-sync folder:")
        print("  " + str(sync_root))
        if not confirm("Initialize it as a claude-sync folder anyway?", assume_yes):
            print("Aborted.")
            return False
    definition = {
        "type": SYNC_FOLDER_TYPE,
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": now_iso(),
        "createdBy": cfg["device"],
        "devices": {cfg["device"]: {
            "os": cfg["os"], "joinedAt": now_iso(),
            "lastSync": "", "engineVersion": VERSION,
        }},
    }
    save_definition(sync_root, definition)
    write_folder_readme(sync_root)
    print("Initialized new claude-sync folder at {}".format(sync_root))
    return True


def write_folder_readme(sync_root: Path):
    readme = sync_root / "README-claude-sync.md"
    if readme.exists():
        return
    readme.write_text(
        "# This is a claude-sync-by-skill folder\n\n"
        "It holds a unified Claude Code environment (memory, plans, settings, skills, "
        "and optionally secrets) shared across machines.\n\n"
        "To join from another machine:\n"
        "1. Install the skill: https://github.com/JonMcMillan/claude-sync-by-skill\n"
        "2. Run setup and point it at THIS folder.\n"
        "3. Run `/sync-env-down` to bring the unified environment onto that machine.\n\n"
        "Then use `/sync-env-up` to push your changes here and `/sync-env-down` on your "
        "other machines to pull them. `/sync-envs` previews changes without touching "
        "anything.\n\n"
        "Managed files in this folder: `.claude-sync.json` (identity + device roster), "
        "`.sync-state.json` (provenance), `.trash/` (recoverable deletions). Do not edit "
        "these by hand.\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# GitHub version check
# --------------------------------------------------------------------------- #
def git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd),
                          capture_output=True, text=True)


def version_check(skill_dir: Path):
    """Best-effort: warn if the local clone is behind origin. Never blocks."""
    if not (skill_dir / ".git").exists():
        return  # not a git clone (e.g. dev tree) — skip silently
    try:
        fetch = git(["fetch", "--quiet"], skill_dir)
        if fetch.returncode != 0:
            print("(version check: could not reach GitHub - continuing offline)")
            return
        head = git(["rev-parse", "HEAD"], skill_dir).stdout.strip()
        upstream = git(["rev-parse", "@{u}"], skill_dir).stdout.strip()
        if head and upstream and head != upstream:
            base = git(["merge-base", "HEAD", "@{u}"], skill_dir).stdout.strip()
            if base == head:
                print("A newer version of claude-sync-by-skill is available.")
                print("  Run: git -C \"{}\" pull --ff-only".format(skill_dir))
    except OSError:
        pass  # git not installed — non-fatal


# --------------------------------------------------------------------------- #
# Git-pull reminders (advisory) — on `down`, check registered project repos
# --------------------------------------------------------------------------- #
# After pulling the Claude environment, surface a nudge when a project's CODE repo is
# behind its remote (someone committed/pushed on another machine). This is read-only:
# best-effort `git fetch` + behind-count. It NEVER runs `pull` — the user decides.
GIT_HINT_CAP = 50  # cap fetches per run, in case of a large project set


def gather_git_pull_hints(project_paths):
    """For each registered project path that is a git repo, fetch (best-effort) and check
    whether the current branch is behind its upstream. Returns (hints, capped); each hint
    is {path, branch, behind, dirty}. Skips detached HEAD, no-upstream, and offline repos
    quietly. Never runs `pull`."""
    hints = []
    capped = False
    checked = 0
    for _key, path in project_paths:
        p = Path(path)
        if not (p / ".git").exists():
            continue
        if checked >= GIT_HINT_CAP:
            capped = True
            break
        checked += 1
        try:
            br = git(["rev-parse", "--abbrev-ref", "HEAD"], p)
            if br.returncode != 0:
                continue
            branch = br.stdout.strip()
            if branch == "HEAD":
                continue  # detached HEAD — nothing to track
            if git(["rev-parse", "--abbrev-ref", "@{u}"], p).returncode != 0:
                continue  # no upstream tracking branch
            if git(["fetch", "--quiet"], p).returncode != 0:
                continue  # offline / auth failure — skip quietly
            counts = git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], p)
            if counts.returncode != 0:
                continue
            parts = counts.stdout.split()
            behind = int(parts[1]) if len(parts) == 2 else 0
            if behind <= 0:
                continue
            dirty = bool(git(["status", "--porcelain"], p).stdout.strip())
            hints.append({"path": str(p), "branch": branch,
                          "behind": behind, "dirty": dirty})
        except (OSError, ValueError):
            continue
    return hints, capped


def render_git_pull_hints(hints, capped):
    if not hints:
        return ""
    lines = ["", "Some project repos are behind their remote (likely pushed from another "
             "machine). Pulling is your call - this tool never pulls for you:"]
    for h in hints:
        warn = "  [has uncommitted changes - commit/stash first]" if h["dirty"] else ""
        lines.append("  - {} ({}): {} commit(s) behind origin{}".format(
            h["path"], h["branch"], h["behind"], warn))
        lines.append("      git -C \"{}\" pull --ff-only".format(h["path"]))
    if capped:
        lines.append("  (checked the first {} repos only)".format(GIT_HINT_CAP))
    lines.append("  Suppress these with --no-git-hints.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def require_config(args):
    home = Path(args.claude_home) if args.claude_home else default_claude_home()
    cfg = load_config(home)
    if not cfg:
        print("No configuration found. Run setup first:")
        print("  python3 sync_engine.py --setup")
        sys.exit(2)
    return cfg


def cmd_status(args):
    cfg = require_config(args)
    version_check(Path(__file__).resolve().parent)
    baseline = load_baseline(Path(cfg["claudeHome"]))
    first_sync = not baseline
    for direction in ("up", "down"):
        actions = compute_plan(direction, cfg, baseline)
        print(render_plan(direction, cfg, actions, first_sync))
        print()
    definition = load_definition(Path(cfg["syncRoot"]))
    if definition and definition.get("devices"):
        print("Devices on this folder: " + ", ".join(sorted(definition["devices"])))


def cmd_sync(args, direction):
    cfg = require_config(args)
    version_check(Path(__file__).resolve().parent)
    sync_root = Path(cfg["syncRoot"])
    if not load_definition(sync_root):
        print("This sync folder is not initialized. Run --setup first.")
        sys.exit(2)

    home = Path(cfg["claudeHome"])
    baseline = load_baseline(home)
    first_sync = not baseline
    actions = compute_plan(direction, cfg, baseline)
    print(render_plan(direction, cfg, actions, first_sync))

    if not args.apply:
        print("\n(preview only - re-run with --apply to perform these changes)")
        return

    has_dels = any(a.kind in DESTRUCTIVE for a in actions)
    confirm_dels = args.confirm_deletions
    if has_dels and not confirm_dels:
        confirm_dels = confirm("\nApply the destructive deletions above?", args.yes)

    state = load_state(sync_root)
    push_secrets = bool(cfg.get("pushSecrets")) or args.push_secrets
    applied, trash_root = apply_plan(
        direction, cfg, actions, baseline, state,
        confirm_deletions=confirm_dels, prefer=args.prefer, push_secrets=push_secrets)

    print("\nApplied: {copied} copied, {deleted} deleted, "
          "{conflicts} conflicts left, {skipped} skipped.".format(**applied))
    if applied["deleted"]:
        print("Trashed to: {}".format(trash_root))
    if applied["conflicts"]:
        print("Conflicts were NOT changed. Re-run with --prefer local|folder to resolve.")

    if direction == "up":
        # Capture each project's true cwd so other machines can rebuild the exact path.
        update_project_registry(cfg)
    else:
        # Tell the user exactly where to work to use each synced project — no typing the
        # path from memory (avoids fat-fingering the folder name). --scaffold pre-creates.
        paths = local_project_paths(cfg)
        if paths:
            print("\n{}:".format(
                "Created these working folders for your synced projects"
                if args.scaffold
                else "Open these working folders to use your synced projects"))
            for canon_key, path in paths:
                if args.scaffold:
                    try:
                        Path(path).mkdir(parents=True, exist_ok=True)
                    except OSError:
                        pass
                print("  {:<24}  {}".format(canon_key, path))
            if not args.scaffold:
                print("  (re-run with --scaffold to pre-create these empty folders)")

        # Scan only the transcripts pulled THIS run (i.e. other-machine activity we didn't
        # have) for tool-install reminders, so the nudge is scoped and never re-nags.
        if not args.no_tool_hints:
            pulled = [a.local_path for a in actions
                      if a.item == "claude-memory" and a.kind == "copy_down"
                      and str(a.local_path).endswith(".jsonl")]
            hints, capped = gather_install_hints(pulled)
            note = render_install_hints(hints, capped)
            if note:
                print(note)

        # Advisory: if a project's code repo is behind its remote (committed/pushed from
        # another machine), nudge the user to pull. Read-only; never pulls automatically.
        if not args.no_git_hints:
            ghints, gcapped = gather_git_pull_hints(local_project_paths(cfg))
            gnote = render_git_pull_hints(ghints, gcapped)
            if gnote:
                print(gnote)


def prompt_work_root(args, existing, sync_root, device):
    """Resolve this machine's main working folder. --work-root wins; a scripted/AI
    install (--sync-root present) is non-interactive; otherwise prompt, with a clear
    first-machine vs joining distinction. Returns a path string or None (feature off)."""
    if args.work_root:
        return args.work_root
    if args.sync_root:  # non-interactive scripted/AI install: don't prompt
        return existing.get("workRoot")

    candidate = existing.get("workRoot") or (
        str(detect_dev_root()) if detect_dev_root() else "")
    print("\n--- Main working folder ---")
    print("Claude ties its memory to the folder you work in. To match a project across")
    print("machines, name your MAIN working folder: the parent your projects live under")
    print("(e.g. C:\\dev) - NOT a single project inside it (e.g. C:\\dev\\my-app).")
    print("Note: this only syncs Claude's memory of your projects, never the code/files")
    print("inside these folders.")

    existing_mwr = get_main_work_root(sync_root)
    if existing_mwr and existing_mwr.get("establishedBy") != device:
        example = existing_mwr.get("examplePath") or existing_mwr.get("canonicalKey")
        print("\nYour other machine '{}' uses its main working folder at: {}".format(
            existing_mwr.get("establishedBy", "?"), example))
        print("Enter THIS machine's matching folder. It can be a different drive or")
        print("name; projects beneath it are paired up automatically.")
    else:
        print("\nIf you're currently inside a specific project, enter its PARENT instead.")

    raw = ask("Main working folder [{}] (blank to skip): ".format(candidate or "none"),
              candidate)
    return raw or None


def cmd_setup(args):
    home = Path(args.claude_home) if args.claude_home else default_claude_home()
    existing = load_config(home) or {}

    print("=== claude-sync-by-skill setup ===")
    print("Claude home: {}".format(home))

    dev_root = existing.get("devRoot") or (
        str(detect_dev_root()) if detect_dev_root() else None)

    # Choose the sync folder. --sync-root makes setup non-interactive (AI-driven /
    # scripted installs); otherwise prompt with detected suggestions.
    if args.sync_root:
        sync_root = Path(args.sync_root)
    else:
        suggestions = suggest_sync_roots()
        if suggestions:
            print("\nDetected possible sync folders:")
            for i, s in enumerate(suggestions, 1):
                print("  {}. {}".format(i, s))
            print("  (or type any path, including a USB/SAN/UNC path)")
        chosen = existing.get("syncRoot", "")
        raw = ask("Sync folder [{}]: ".format(chosen or "enter a path"), chosen)
        if raw.isdigit() and 1 <= int(raw) <= len(suggestions):
            sync_root = suggestions[int(raw) - 1] / "Claude"
        elif raw:
            sync_root = Path(raw)
        else:
            print("No sync folder given. Aborting.")
            sys.exit(2)

    device = args.device or existing.get("device") or hostname()
    if not args.sync_root and not args.device:
        device = ask("Device id [{}]: ".format(device), device)

    # Main working folder -> per-machine project-key map. A machine adopts the folder's
    # canonical key if one is already established; otherwise it anchors it.
    work_root = prompt_work_root(args, existing, sync_root, device)
    work_key = sanitize_path_to_key(work_root) if work_root else None
    if work_key:
        existing_mwr = get_main_work_root(sync_root)
        canonical_key = existing_mwr["canonicalKey"] if existing_mwr else work_key
    else:
        canonical_key = None

    cfg = {
        "schemaVersion": SCHEMA_VERSION,
        "device": device,
        "os": detect_os(),
        "claudeHome": str(home),
        "devRoot": dev_root,
        "syncRoot": str(sync_root),
        "workRoot": work_root,
        "workKey": work_key,
        "canonicalKey": canonical_key,
        "pushSecrets": bool(existing.get("pushSecrets", False)),
        "skillExclude": existing.get("skillExclude", list(TOOL_SKILL_FOLDERS)),
    }

    if not init_or_join_folder(sync_root, cfg, args.yes):
        sys.exit(2)
    ensure_main_work_root(sync_root, cfg)

    save_config(cfg)
    print("\nWrote {}".format(config_path(home)))
    print("Device '{}' ({}) registered on {}".format(device, cfg["os"], sync_root))
    if work_root:
        print("Main working folder: {}".format(work_root))
        if work_key != canonical_key:
            print("  Projects here map to the shared canonical root '{}' "
                  "(local '{}').".format(canonical_key, work_key))
        else:
            print("  This machine anchors the canonical root '{}'.".format(canonical_key))
    print("\nNext: run a preview with")
    print("  python3 sync_engine.py --direction down")
    print("then apply with --apply once you're happy.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="sync_engine.py",
        description="Unify a Claude Code environment across machines (up/down/status).")
    p.add_argument("--setup", action="store_true", help="interactive first-time setup")
    p.add_argument("--direction", choices=["up", "down"],
                   help="up: local->folder; down: folder->local")
    p.add_argument("--status", action="store_true",
                   help="read-only preview of up and down (default)")
    p.add_argument("--apply", action="store_true",
                   help="perform the changes (otherwise preview only)")
    p.add_argument("--confirm-deletions", action="store_true",
                   help="perform destructive deletions without prompting")
    p.add_argument("--yes", action="store_true",
                   help="assume yes for confirmation prompts")
    p.add_argument("--prefer", choices=["local", "folder"],
                   help="resolve conflicts in favor of one side")
    p.add_argument("--push-secrets", action="store_true",
                   help="allow pushing secret files (.env) to the folder")
    p.add_argument("--no-tool-hints", action="store_true",
                   help="down: skip scanning pulled transcripts for tool-install reminders")
    p.add_argument("--no-git-hints", action="store_true",
                   help="down: skip checking whether project repos are behind their remote")
    p.add_argument("--claude-home", help="override the Claude home directory")
    p.add_argument("--sync-root", help="setup: sync folder path (non-interactive)")
    p.add_argument("--work-root",
                   help="setup: main working folder (parent your projects live under, "
                        "e.g. C:\\dev); enables cross-machine project matching")
    p.add_argument("--device", help="setup: device id (defaults to hostname)")
    p.add_argument("--scaffold", action="store_true",
                   help="down: pre-create the empty local working folder for each "
                        "synced project")
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.version:
        print("claude-sync-by-skill {} (schema {})".format(VERSION, SCHEMA_VERSION))
        return 0
    if args.setup:
        cmd_setup(args)
        return 0
    if args.direction:
        cmd_sync(args, args.direction)
        return 0
    cmd_status(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
