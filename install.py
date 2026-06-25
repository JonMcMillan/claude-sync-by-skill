#!/usr/bin/env python3
"""
claude-sync-by-skill — installer.

Cross-platform. Places the engine (this repo) into your Claude skills directory as the
`sync-envs` skill, materializes the `sync-env-up` / `sync-env-down` wrapper skills, and
launches interactive setup.

Usage:
    python3 install.py                 # clone/refresh + materialize wrappers + setup
    python3 install.py --no-setup      # skip the interactive setup step
    python3 install.py --skills-dir X  # override the Claude skills directory

Requires Python 3.8+ and git.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/JonMcMillan/claude-sync-by-skill.git"
TOOL_FOLDER = "sync-envs"
WRAPPERS = {
    "sync-env-up": "wrappers/sync-env-up.SKILL.md",
    "sync-env-down": "wrappers/sync-env-down.SKILL.md",
}


def run(cmd, **kw):
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd, **kw)


def have_git() -> bool:
    try:
        return run(["git", "--version"], capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def is_git_clone(path: Path) -> bool:
    return (path / ".git").exists()


def ensure_engine(skills_dir: Path) -> Path:
    target = skills_dir / TOOL_FOLDER
    skills_dir.mkdir(parents=True, exist_ok=True)

    if target.exists() and not is_git_clone(target):
        print("ERROR: {} already exists and is not a git clone of this tool.".format(target))
        print("Back it up / remove it first, then re-run the installer.")
        sys.exit(2)

    if is_git_clone(target):
        print("Updating existing install at {}".format(target))
        run(["git", "-C", str(target), "pull", "--ff-only"])
    else:
        print("Cloning engine into {}".format(target))
        if run(["git", "clone", REPO_URL, str(target)]).returncode != 0:
            print("ERROR: git clone failed.")
            sys.exit(2)
    return target


def materialize_wrappers(skills_dir: Path, engine_dir: Path) -> None:
    for folder, rel in WRAPPERS.items():
        src = engine_dir / rel
        if not src.exists():
            print("WARNING: wrapper template missing: {}".format(src))
            continue
        dest_dir = skills_dir / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src), str(dest_dir / "SKILL.md"))
        print("Materialized skill: {}".format(dest_dir / "SKILL.md"))


def main(argv=None):
    p = argparse.ArgumentParser(description="Install claude-sync-by-skill.")
    p.add_argument("--no-setup", action="store_true", help="skip interactive setup")
    p.add_argument("--skills-dir", help="override the Claude skills directory")
    args = p.parse_args(argv)

    if not have_git():
        print("ERROR: git is required but was not found on PATH.")
        sys.exit(2)

    skills_dir = (Path(args.skills_dir) if args.skills_dir
                  else Path.home() / ".claude" / "skills")
    print("Claude skills directory: {}".format(skills_dir))

    engine_dir = ensure_engine(skills_dir)
    materialize_wrappers(skills_dir, engine_dir)

    print("\nInstalled. Skills available: /sync-envs (status), /sync-env-up, /sync-env-down")

    if args.no_setup:
        print("Run setup later with:")
        print("  {} \"{}\" --setup".format(Path(sys.executable).name, engine_dir / "sync_engine.py"))
        return 0

    print("\nLaunching setup...\n")
    rc = run([sys.executable, str(engine_dir / "sync_engine.py"), "--setup"])
    return rc.returncode


if __name__ == "__main__":
    sys.exit(main())
