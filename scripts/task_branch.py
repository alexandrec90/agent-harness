"""Shared helpers for starting a task on a fresh `claude/<slug>` branch.

Used by two callers that start work the same way but trigger differently:
  - `scripts/hooks/branch-per-task.py` -- the UserPromptSubmit hook, automatic,
    fires only when sitting on `master` (the primary-checkout case).
  - `scripts/start-task.py` -- the explicit `/task` command, for worktrees where
    you are never on `master` and always sit on a stale (shipped/merged) branch.

Pure and stdlib-only so the hook can import it before the venv is active. Tested
in `scripts/hooks/tests/test_task_branch.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess

# Fallback default branch. The real default is resolved per-repo by
# `detect_default_branch()` (callers pass it in), so this is only the value the
# pure helpers assume when a caller does not override -- keeping the module
# project-agnostic while the branch name stays out of the logic.
DEFAULT_BRANCH = "master"
BRANCH_PREFIX = "claude/"
SLUG_MAX_LEN = 40
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Per-worktree marker file (under the worktree's git dir) recording the branch
# `/ship` last shipped. The branch-per-task hook uses it to auto-cut a fresh
# branch on the next prompt: once a branch is shipped it is "spent", so starting
# new work should leave it. Resolve its path with `git rev-parse --git-path`.
# Project-agnostic name so the harness vendors unchanged.
SHIPPED_MARKER_NAME = "agent-shipped"


def detect_default_branch(
    git: Callable[..., subprocess.CompletedProcess[str]], fallback: str = "main"
) -> str:
    """The remote's default branch, project-agnostically. Replaces a hardcoded name.

    Resolves `origin/HEAD` (the symbolic ref set at clone time); if that is not set,
    probes `origin/main` then `origin/master`; if neither exists, returns `fallback`.
    `git(*args)` must return a CompletedProcess capturing stdout -- injected so this
    stays pure and unit-testable without spawning git.
    """
    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = (head.stdout or "").strip()
    if head.returncode == 0 and ref.startswith("refs/remotes/origin/"):
        return ref.rsplit("/", 1)[1]
    for candidate in ("main", "master"):
        if (
            git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}").returncode
            == 0
        ):
            return candidate
    return fallback


def parse_prompt(raw_stdin: str) -> str:
    """Extract the prompt text from a UserPromptSubmit payload; '' when absent."""
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("prompt", "")
    return value if isinstance(value, str) else ""


def slugify(text: str, max_len: int = SLUG_MAX_LEN) -> str:
    """Turn free text into a branch-safe slug (lowercase, hyphenated)."""
    slug = _SLUG_STRIP_RE.sub("-", text.strip().lower()).strip("-")
    if len(slug) > max_len:
        # Trim at a word boundary so the slug stays readable, not mid-token.
        slug = slug[:max_len].rsplit("-", 1)[0] if "-" in slug[:max_len] else slug[:max_len]
        slug = slug.strip("-")
    return slug or "task"


def should_branch(current_branch: str, default_branch: str = DEFAULT_BRANCH) -> bool:
    """True only when sitting on the default branch (the hook's auto-trigger).

    A blank branch means detached HEAD -- never branch from there automatically.
    """
    return bool(current_branch) and current_branch == default_branch


def branch_name(slug: str, existing: set[str], today: _dt.date | None = None) -> str:
    """Unique `claude/<slug>-<mmdd>` name, disambiguated with -N against existing."""
    today = today or _dt.date.today()
    base = f"{BRANCH_PREFIX}{slug}-{today:%m%d}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def checkout_base(tree_dirty: bool, default_branch: str = DEFAULT_BRANCH) -> str | None:
    """Which ref to base a new branch on when auto-branching from the default branch.

    Clean tree -> branch from `origin/<default_branch>` (start current). Dirty tree ->
    None: branch from the current HEAD carrying the uncommitted changes, because
    resetting onto the default branch could clobber them.
    """
    return None if tree_dirty else f"origin/{default_branch}"


def auto_branch_decision(
    current_branch: str,
    shipped_branch: str,
    tree_dirty: bool,
    default_branch: str = DEFAULT_BRANCH,
) -> tuple[bool, str | None]:
    """Should the hook auto-branch now, and on which base?

    Returns (should_branch, base_ref). base_ref None means "branch from the
    current HEAD carrying uncommitted changes" (only for the dirty-master case).

    Two triggers, both safe against branching mid-task:
      - On the default branch (primary checkout): always start fresh work here.
        Carry a dirty tree rather than clobber it.
      - On the branch `/ship` just shipped, with a clean tree (worktree case):
        the branch is spent, so cut the next task off origin/master. The clean-
        tree guard means unshipped edits are never yanked; the caller clears the
        marker after, so an empty fresh branch never re-triggers (no loop).
    """
    if should_branch(current_branch, default_branch):
        return True, checkout_base(tree_dirty, default_branch)
    if shipped_branch and current_branch == shipped_branch and not tree_dirty:
        return True, f"origin/{default_branch}"
    return False, None


def platform_manages_branch(env: Mapping[str, str]) -> bool:
    """True in a cloud/remote session (Claude Code on the web / mobile), where the
    platform owns the branch lifecycle -- so the auto-branch hook must stand down.

    On the web/mobile the platform checks out and tracks a `claude/...` branch and
    handles commit/PR. Cutting a *new* local branch here -- which the shipped-marker
    path would do on the prompt after `/ship` -- diverges HEAD from the branch the
    mobile app tracks, so later work lands on a branch its UI never shows. The
    primary-checkout (`master`) trigger never fires remotely anyway (the platform
    never leaves you on `master`), so standing the whole hook down is a no-op there
    and closes the shipped-marker hole. Mirrors the `CLAUDE_CODE_REMOTE` guard in
    `.claude/hooks/session-start.sh`.
    """
    return env.get("CLAUDE_CODE_REMOTE") == "true"
