#!/usr/bin/env python3
"""Vendor the shared agent-harness scripts into this project, and drift-check them.

The hook/harness scripts are meant to be identical across projects (see
`.devkit.toml` for the per-project seam). Rather than fork them per repo,
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

The shared-repo path resolves from `--src` or `$DEVKIT_DIR`. `.devkit.toml`
itself is **never** synced -- it is the per-project config the shared code reads.

`MANIFEST` is the reviewed, portable subset (config loader + the scripts audited so
far); extend it as more scripts are decoupled. Pure helpers (`resolve_src`,
`classify`) are unit-tested in `scripts/hooks/tests/test_sync_devkit.py`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = (Path(__file__).parent / "..").resolve()
SRC_ENV = "DEVKIT_DIR"
# Records which shared-repo commit this project's vendored copy corresponds to.
# Committed, per-project (like .devkit.toml), so NOT in MANIFEST.
VERSION_FILE = "DEVKIT_VERSION"

# Repo-relative paths of the shared harness files (source of truth = shared repo).
# Every entry ships with its test; keep both in the manifest so a vendored copy is
# verifiable in isolation. NB: `.devkit.toml` is intentionally absent -- it
# is per-project config, not shared code.
#
# NOT yet included: the `.claude -> .agents/.codex` mirror scripts
# (`sync-agents-context.py`, `sync-codex-hooks.py`) are portable but their tests
# want auditing before vendoring -- add them next.
MANIFEST: tuple[str, ...] = (
    # Test plumbing: the load_module() loader every vendored test imports.
    "scripts/hooks/tests/conftest.py",
    # Repo-shape contract: the vendored scripts' unvendored dependencies exist, and
    # the manifest that selects them is spelled right. No script of its own -- it
    # asserts against whatever the consuming repo already has.
    "scripts/hooks/tests/test_repo_contract.py",
    # Config loader (the per-project seam) + the Stop dispatcher it drives.
    "scripts/hooks/harness_config.py",
    "scripts/hooks/tests/test_harness_config.py",
    "scripts/hooks/stop.py",
    "scripts/hooks/tests/test_stop.py",
    # Auto-fix-on-edit PostToolUse hook (repo-relative ruff path fix).
    "scripts/hooks/lint-fix.py",
    "scripts/hooks/tests/test_lint_fix.py",
    # Bash output cap: the PreToolUse gate and the wrapper it demands. They ship
    # together because the gate's allow-list matches the wrapper's path -- vendoring
    # one without the other yields a hook that blocks every Bash call and names a
    # remedy the repo does not have. Cap size is `[bash]` in the manifest.
    "scripts/hooks/enforce-capped-bash.py",
    "scripts/hooks/tests/test_enforce_capped_bash.py",
    "scripts/hooks/invoke-capped.py",
    "scripts/hooks/tests/test_invoke_capped.py",
    # Known-fixes normalizer (project-agnostic; operates on .claude/skills).
    "scripts/hooks/normalize-known-fixes.py",
    "scripts/hooks/tests/test_normalize_known_fixes.py",
    # The three scripts `stop.py` dispatches to. They ship WITH it, deliberately:
    # stop.py resolves all three by path, sends both streams to DEVNULL and never
    # reads the exit code, so a missing one is the quietest failure in the harness --
    # every configured skill just stops being finalized and no session archive is
    # written. devkit shipped stop.py without them for several releases and
    # `test_repo_contract.py` asserted for one it did not have.
    #
    # `state-engine.py` is the only writer of a skill's state.json. Its `modules` and
    # `files` schemas are pure merges; its `audit` schema needs the project to declare
    # its checks in `.claude/skills/<skill>/check-specs.json`, which is NOT vendored --
    # that file is one project's source layout.
    "scripts/hooks/finalize-state.py",
    "scripts/hooks/tests/test_finalize_state.py",
    ".claude/skills/state-tools/state-engine.py",
    ".claude/skills/state-tools/README.md",
    "scripts/hooks/tests/test_state_engine.py",
    "scripts/hooks/archive-session.py",
    "scripts/hooks/tests/test_archive_session_outcomes.py",
    "scripts/hooks/tests/test_archive_session_stdin.py",
    "scripts/hooks/tests/test_archive_session_tokens.py",
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
    "scripts/sync-devkit.py",
    "scripts/hooks/tests/test_sync_devkit.py",
    # --- The shared instruction tier -----------------------------------------
    # Same argument as the scripts, applied to the prose that steers the agent. These
    # paragraphs used to live inline in each repo's CLAUDE.md, get copied forward by
    # hand, and drift: devkit's own template had already lost a clause of the testing
    # mandate that carameli still had, and nothing could detect it. A project's
    # CLAUDE.md now points at these instead of restating them.
    #
    # Only genuinely portable files belong here. A rule or skill that names one
    # project's paths, services, or default branch is that project's own -- vendoring
    # it repeats the mistake that made every generated project fail 12 tests on its
    # first CI run.
    ".claude/rules/engineering.md",
    ".claude/rules/authoring.md",
    # Skills with no project coupling. `ship` and `task` drive the branch lifecycle
    # and had `master` written through them; the prose now defers to the default
    # branch that `task_branch.detect_default_branch()` resolves at runtime, which is
    # `main` in every generated project.
    ".claude/skills/ship/SKILL.md",
    ".claude/skills/task/SKILL.md",
    ".claude/skills/retro/SKILL.md",
    ".claude/skills/retro/extract.py",
    "scripts/hooks/tests/test_retro_extract.py",
    ".claude/skills/test-skill/SKILL.md",
    ".claude/skills/test-skill/write-artifacts.py",
    "scripts/hooks/tests/test_write_artifacts.py",
    ".claude/skills/audit-claude-md/SKILL.md",
    ".claude/skills/audit-gitignore/SKILL.md",
    ".claude/skills/audit-dockerignore/SKILL.md",
    # Skills whose *prose* is portable but whose sibling state file is not. Only the
    # SKILL.md is vendored; `known-fixes.md` and `state.json` are this repo's own
    # accumulated learning, seeded empty by the generator. Vendoring those would reset
    # every project's hit counts on each --pull -- and hit counts are exactly what
    # normalize-known-fixes.py prunes against.
    ".claude/skills/plan-handoff/SKILL.md",
    ".claude/skills/fix-pre-commit/SKILL.md",
    ".claude/skills/refactor/SKILL.md",
    # The .claude -> AGENTS.md/.agents mirror, so a project's Codex-facing tree is
    # generated rather than hand-maintained. `sync-codex-hooks.py` only fires when the
    # project has a `.codex/` directory, so this is inert until a repo opts in.
    # NB: carameli's test_codex_hooks_contract.py is deliberately NOT vendored -- it
    # pins that repo's exact hook topology, which is the coupling this tier exists to
    # avoid. It belongs in a project's own non-vendored suite.
    "scripts/sync-agents-context.py",
    "scripts/hooks/tests/test_sync_agents_context.py",
    "scripts/sync-codex-hooks.py",
    "scripts/hooks/tests/test_sync_codex_hooks.py",
)


def resolve_src(arg: str | None, env: Mapping[str, str]) -> Path | None:
    """The shared-repo root from `--src` or `$DEVKIT_DIR`, or None when unset."""
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
            (REPO_ROOT / VERSION_FILE).write_text(
                f"{git_head(src) or 'unknown'}\n", encoding="utf-8", newline="\n"
            )
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
        "Run `python scripts/sync-devkit.py --pull` to adopt upstream, "
        "or `--push` if this project authored the change.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
