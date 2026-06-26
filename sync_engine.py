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


def classify(direction, has_local, has_sync, lm, sm, in_baseline):
    tol = MTIME_TOLERANCE
    if has_local and has_sync:
        if abs(lm - sm) <= tol:
            return "in_sync"
        if direction == "up":
            return "copy_up" if lm > sm else "conflict"
        return "copy_down" if sm > lm else "conflict"
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
            kind = classify(direction, has_local, has_sync,
                            lm or 0.0, sm or 0.0, regkey in baseline)
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
    "keep_local_new": "keep (new local, unpushed)",
    "leave_upstream": "leave (added elsewhere)",
    "absent": "absent",
}


def summarize(actions):
    counts = {}
    for a in actions:
        if a.kind == "in_sync":
            continue
        counts[a.kind] = counts.get(a.kind, 0) + 1
    return counts


def render_plan(direction, cfg, actions, first_sync):
    lines = []
    interesting = [a for a in actions if a.kind != "in_sync"]
    header = "sync-env-{}  (device: {}  syncRoot: {})".format(
        direction, cfg["device"], cfg["syncRoot"])
    lines.append(header)
    if first_sync:
        lines.append("FIRST SYNC for this machine - analyzing the full environment; "
                     "no deletions will occur on a first sync.")
    if not interesting:
        lines.append("Everything is in sync. Nothing to do.")
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
            baseline[a.regkey] = now_iso()
            files[a.regkey] = {"host": device, "mtime": now_iso()}
            applied["copied"] += 1

        elif kind == "copy_down":
            copy_preserve(a.sync_path, a.local_path, a.secret)
            baseline[a.regkey] = now_iso()
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
            baseline.setdefault(a.regkey, now_iso())

        # keep_local_new / leave_upstream / absent -> no-op

    save_baseline(Path(cfg["claudeHome"]), baseline)
    save_state(sync_root, state, device)
    touch_device(sync_root, cfg)
    return applied, trash_root


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
                subpath = ""
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
