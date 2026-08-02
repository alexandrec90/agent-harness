"""Sync the current branch onto its remote's default branch, keeping unstaged work.

Repo-agnostic on purpose: it detects ``origin``'s default branch (master vs main)
instead of hardcoding one, and operates on the current working directory so a single
workspace-level VS Code task works across every repo and worktree. That task picks the
checkout and `devkit_project.py` sets the cwd -- previously it inferred one from
``${fileWorkspaceFolder}``, which silently needed an editor tab open to anchor on.

Flow: fetch -> (stash if dirty) -> rebase onto origin/<default> -> pop stash.
Safe to run from a terminal too: ``python <this file>`` from inside any repo.
"""

from __future__ import annotations

import subprocess
import sys

STASH_MSG = "vscode-sync-autostash"


def git(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run git in the process cwd (VS Code sets it to the active repo)."""
    return subprocess.run(["git", *args], text=True, capture_output=capture, check=False)


def out(*args: str) -> tuple[int, str]:
    r = git(*args, capture=True)
    if r.returncode != 0 and r.stderr:
        print(r.stderr.rstrip(), file=sys.stderr)
    return r.returncode, (r.stdout or "").strip()


def default_branch() -> str | None:
    """Resolve origin's default branch name (e.g. 'master' or 'main')."""
    code, ref = out("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if code == 0 and ref:
        return ref.rsplit("/", 1)[-1]
    # origin/HEAD not populated yet — ask the remote, then retry.
    git("remote", "set-head", "origin", "--auto", capture=True)
    code, ref = out("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if code == 0 and ref:
        return ref.rsplit("/", 1)[-1]
    # Last resort: probe the usual suspects.
    for name in ("main", "master"):
        if out("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}")[0] == 0:
            return name
    return None


def main() -> int:
    if out("rev-parse", "--show-toplevel")[0] != 0:
        print("Not inside a git repository.", file=sys.stderr)
        return 1

    _, branch = out("branch", "--show-current")
    if not branch:
        print("HEAD is detached. Check out a branch first.", file=sys.stderr)
        return 1

    if git("fetch", "--prune", "origin").returncode != 0:
        return 1

    base = default_branch()
    if base is None:
        print("Could not determine origin's default branch.", file=sys.stderr)
        return 1
    target = f"origin/{base}"

    _, dirty = out("status", "--porcelain")
    stashed = False
    if dirty:
        print("Stashing local changes (including untracked)...")
        if git("stash", "push", "--include-untracked", "-m", STASH_MSG).returncode != 0:
            print("Stash failed; aborting so nothing is lost.", file=sys.stderr)
            return 1
        stashed = True

    print(f"Rebasing {branch!r} onto {target}...")
    if git("rebase", target).returncode != 0:
        print(
            "\nRebase hit conflicts. Resolve them and 'git rebase --continue', "
            "or 'git rebase --abort' to back out.",
            file=sys.stderr,
        )
        if stashed:
            print(
                f"Your changes are safe in the stash ('{STASH_MSG}'). "
                "After the rebase settles, run 'git stash pop'.",
                file=sys.stderr,
            )
        return 1

    if stashed:
        print("Restoring your local changes...")
        if git("stash", "pop").returncode != 0:
            print(
                "\nStash pop hit conflicts — your changes are preserved in the "
                "working tree; resolve the conflicts to finish.",
                file=sys.stderr,
            )
            return 1

    print(f"Done. {branch!r} is now synced with {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
