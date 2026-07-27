#!/usr/bin/env python3
"""Vendor the shared agent-harness scripts into this project, and drift-check them.

The hook/harness scripts are meant to be identical across projects (see
`.agent-harness.toml` for the per-project seam). Rather than fork them per repo,
one **shared harness repo is the source of truth** and each project commits a
**vendored copy** -- so cloning a single project still gets everything, with no
submodule. This script keeps that copy honest:

  - `--check` (default): fail (exit 1) if any vendored file drifts from the shared
    repo. Wired into the PR gate. **No-ops clean (exit 0) when the shared repo is
    not configured**, so CI is safe before a project adopts the shared repo.
  - `--pull`: copy the shared repo's version into this project (adopt upstream).
  - `--push`: copy this project's version into the shared repo (author a change /
    seed a fresh shared repo from the project that currently owns the code).
  - `--list`: print the manifest and resolved source, then exit.

The shared-repo path resolves from `--src` or `$AGENT_HARNESS_DIR`. `.agent-harness.toml`
itself is **never** synced -- it is the per-project config the shared code reads.

`MANIFEST` is the reviewed, portable subset (config loader + the scripts audited so
far); extend it as more scripts are decoupled. Pure helpers (`resolve_src`,
`classify`) are unit-tested in `scripts/hooks/tests/test_sync_harness.py`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = (Path(__file__).parent / "..").resolve()
SRC_ENV = "AGENT_HARNESS_DIR"
# Records which shared-repo commit this project's vendored copy corresponds to.
# Committed, per-project (like .agent-harness.toml), so NOT in MANIFEST.
VERSION_FILE = "HARNESS_VERSION"

# Repo-relative paths of the shared harness files (source of truth = shared repo).
# Every entry ships with its test; keep both in the manifest so a vendored copy is
# verifiable in isolation. NB: `.agent-harness.toml` is intentionally absent -- it
# is per-project config, not shared code.
#
# NOT yet included: the `.claude -> .agents/.codex` mirror scripts
# (`sync-agents-context.py`, `sync-codex-hooks.py`) are portable but their tests
# want auditing before vendoring -- add them next.
MANIFEST: tuple[str, ...] = (
    # Test plumbing: the load_module() loader every vendored test imports.
    "scripts/hooks/tests/conftest.py",
    # Config loader (the per-project seam) + the Stop dispatcher it drives.
    "scripts/hooks/harness_config.py",
    "scripts/hooks/tests/test_harness_config.py",
    "scripts/hooks/stop.py",
    "scripts/hooks/tests/test_stop.py",
    # Auto-fix-on-edit PostToolUse hook (repo-relative ruff path fix).
    "scripts/hooks/lint-fix.py",
    "scripts/hooks/tests/test_lint_fix.py",
    # Known-fixes normalizer (project-agnostic; operates on .claude/skills).
    "scripts/hooks/normalize-known-fixes.py",
    "scripts/hooks/tests/test_normalize_known_fixes.py",
    # Branch lifecycle: default-branch auto-detected (detect_default_branch), so
    # these vendor unchanged. session-start.sh is the SessionStart entrypoint.
    "scripts/task_branch.py",
    "scripts/hooks/tests/test_task_branch.py",
    "scripts/hooks/session-sync.py",
    "scripts/hooks/tests/test_session_sync.py",
    "scripts/hooks/branch-per-task.py",
    ".claude/hooks/session-start.sh",
    "scripts/hooks/tests/test_session_start.py",
    # The vendoring tool itself, so a project can drift-check / pull / push.
    "scripts/sync-harness.py",
    "scripts/hooks/tests/test_sync_harness.py",
)


def resolve_src(arg: str | None, env: dict[str, str]) -> Path | None:
    """The shared-repo root from `--src` or `$AGENT_HARNESS_DIR`, or None when unset."""
    raw = arg or env.get(SRC_ENV)
    return Path(raw).expanduser().resolve() if raw else None


def read_version(root: Path) -> str | None:
    """The stamped harness commit this project vendored, or None if never pulled."""
    try:
        return (root / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


def git_head(path: Path) -> str | None:
    """Short HEAD SHA of the git repo at `path`, or None (not a repo / no git)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def classify(
    src: Path, repo_root: Path, manifest: tuple[str, ...]
) -> tuple[list[str], list[str], list[str]]:
    """Partition the manifest into (drifted, missing_in_src, ok) by byte comparison.

    `missing_in_src` are files absent from the shared repo (it does not have them
    yet -- e.g. before a first `--push`); they are reported, never silently OK.
    """
    drifted: list[str] = []
    missing: list[str] = []
    ok: list[str] = []
    for rel in manifest:
        src_bytes = _read(src / rel)
        if src_bytes is None:
            missing.append(rel)
            continue
        if _read(repo_root / rel) == src_bytes:
            ok.append(rel)
        else:
            drifted.append(rel)
    return drifted, missing, ok


def _copy(rel: str, from_root: Path, to_root: Path) -> bool:
    """Copy one manifest file from_root -> to_root. False when the source is absent."""
    source = from_root / rel
    if not source.exists():
        return False
    dest = to_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on drift (default)")
    mode.add_argument("--pull", action="store_true", help="copy shared repo -> this project")
    mode.add_argument("--push", action="store_true", help="copy this project -> shared repo")
    mode.add_argument("--list", action="store_true", help="print manifest + source, then exit")
    parser.add_argument("--src", help=f"shared harness repo root (else ${SRC_ENV})")
    args = parser.parse_args(argv)

    src = resolve_src(args.src, os.environ)

    if args.list:
        print(f"source: {src or '(unset)'}")
        print(f"vendored version: {read_version(REPO_ROOT) or '(never pulled)'}")
        for rel in MANIFEST:
            print(f"  {rel}")
        return 0

    if src is None:
        # Unconfigured: every mode is a clean no-op so the PR gate passes pre-adoption.
        print(f"sync-harness: ${SRC_ENV} unset and no --src; nothing to do (skipping).")
        return 0

    if args.pull or args.push:
        from_root, to_root = (src, REPO_ROOT) if args.pull else (REPO_ROOT, src)
        copied = [rel for rel in MANIFEST if _copy(rel, from_root, to_root)]
        skipped = [rel for rel in MANIFEST if rel not in copied]
        verb = "pulled" if args.pull else "pushed"
        print(f"sync-harness: {verb} {len(copied)} file(s); skipped {len(skipped)} absent.")
        for rel in skipped:
            print(f"  (absent) {rel}")
        if args.pull:
            # Stamp which shared-repo commit this vendored copy now corresponds to.
            (REPO_ROOT / VERSION_FILE).write_text(f"{git_head(src) or 'unknown'}\n")
        return 0

    # Default: --check
    vendored, available = read_version(REPO_ROOT), git_head(src)
    if available and vendored and vendored != available:
        # Informational only -- drift is decided by content below, not version.
        print(f"sync-harness: vendored {vendored}, shared repo at {available} (newer available).")
    drifted, missing, _ = classify(src, REPO_ROOT, MANIFEST)
    if not drifted and not missing:
        print(f"sync-harness: all {len(MANIFEST)} vendored files in sync with {src}.")
        return 0
    for rel in drifted:
        print(f"DRIFT   {rel}", file=sys.stderr)
    for rel in missing:
        print(f"MISSING {rel} (not in shared repo)", file=sys.stderr)
    print(
        "sync-harness: vendored harness drifted from the shared repo. "
        "Run `python scripts/sync-harness.py --pull` to adopt upstream, "
        "or `--push` if this project authored the change.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
