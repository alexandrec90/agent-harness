#!/usr/bin/env python3
"""Cross-project ship sweep: find work stranded across the workspace's checkouts.

One `/ship` run takes one checkout from "task finished" to "PR open". This walks
every checkout in a VS Code multi-root workspace and reports which ones still have
work in them, so nothing sits forgotten on a branch (or, more often, forgotten on
the default branch) while attention is elsewhere.

**This script never mutates a repository.** It reads git state and classifies it;
committing, pushing, and opening PRs stay with `/ship`, which runs per-repo with
the diff in context because that is what a commit message actually needs. The
split is deliberate: the mechanical half (what state is each repo in, what is the
next action) is deterministic and testable and lives here; the semantic half
(is this diff one coherent change, and what is it *for*) does not.

Modes:
  (default)   human-readable table -- the testing/inspection mode.
  --json      the same verdicts as JSON, for a driver to fan out over.
  --check     exit 1 when any repo needs action, 2 when any is blocked. For a
              root-level task that should fail loudly rather than print quietly.

The classification contract that makes "nothing stranded" checkable: `classify()`
is total -- every repo lands in exactly one verdict -- and every verdict except
`clean`/`skipped` has a non-empty `plan_for()`. Tested in `tests/test_sweep.py`.

Pure and stdlib-only. All git access goes through an injected callable so the
decision logic is unit-testable without spawning git.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]
# The workspace file lives beside the checkouts it lists, one level above devkit.
DEFAULT_WORKSPACE = REPO_ROOT.parent / "alex-projects.code-workspace"

# Checkouts in the workspace that this sweep does not manage. VanillaLand is the
# legacy Azure DevOps monolith: different host, different PR API, `develop` base,
# and it is a reference checkout rather than something we ship from.
DEFAULT_EXCLUDE: frozenset[str] = frozenset({"VanillaLand"})

Git = Callable[..., "subprocess.CompletedProcess[str]"]

# --- verdicts ---------------------------------------------------------------
# Ordered roughly by how much attention each needs.
BLOCKED = "blocked"  # a human has to look; the sweep will not guess
NEEDS_BRANCH = "needs-branch"  # work sitting on the default branch
READY = "ready"  # feature branch with content -- /ship it
NEEDS_PR = "needs-pr"  # feature branch pushed, PR may not exist
NEEDS_PULL = "needs-pull"  # clean on the default branch, just behind
CLEAN = "clean"  # nothing to do
SKIPPED = "skipped"  # not a git checkout

# Verdicts that mean "there is work here". `--check` exits non-zero on these.
ACTIONABLE: frozenset[str] = frozenset({BLOCKED, NEEDS_BRANCH, READY, NEEDS_PR, NEEDS_PULL})
# Verdicts with no next action. Every *other* verdict must yield a plan.
TERMINAL: frozenset[str] = frozenset({CLEAN, SKIPPED})


@dataclass(frozen=True)
class State:
    """Everything the classifier needs about one checkout.

    Built by `inspect()` from git, or by hand in tests. `behind`/`ahead` are
    measured against `origin/<default_branch>`; `unpushed` against the branch's
    own upstream (-1 when it has none, which is different from 0).
    """

    name: str
    path: str = ""
    is_git: bool = True
    host: str = "other"
    default_branch: str = ""
    branch: str = ""
    dirty: int = 0
    behind: int = 0
    ahead: int = 0
    upstream: str = ""
    unpushed: int = -1
    remote_url: str = ""


@dataclass
class Result:
    """A classified checkout: its state, its verdict, and what to do about it."""

    state: State
    verdict: str
    reason: str
    plan: list[str] = field(default_factory=list)


# --- pure helpers -----------------------------------------------------------


def parse_workspace(text: str, exclude: frozenset[str] = DEFAULT_EXCLUDE) -> list[str]:
    """Folder names from a `.code-workspace` file, minus `exclude`, in file order.

    Returns [] for malformed JSON rather than raising: a broken workspace file
    should make the sweep report nothing, not crash a root-level task. VS Code
    allows a trailing-comma/comment dialect, so a parse failure is plausible
    enough to handle rather than assert away.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    names: list[str] = []
    for folder in payload.get("folders", []):
        if not isinstance(folder, dict):
            continue
        path = folder.get("path")
        if not isinstance(path, str) or not path:
            continue
        # `path` is workspace-relative; its last segment is the checkout name.
        name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name and name not in exclude and name not in names:
            names.append(name)
    return names


def remote_host(url: str) -> str:
    """Which forge a remote URL points at -- decides which PR API `/ship` uses."""
    lowered = url.lower()
    if "github.com" in lowered:
        return "github"
    if "dev.azure.com" in lowered or "visualstudio.com" in lowered:
        return "azure"
    return "other" if lowered else "none"


def count_lines(text: str) -> int:
    """Number of non-blank lines -- the shape of `git status --porcelain` output."""
    return len([line for line in text.splitlines() if line.strip()])


def parse_ahead_behind(text: str) -> tuple[int, int]:
    """(behind, ahead) from `rev-list --left-right --count base...HEAD`; (0, 0) if unparseable."""
    parts = text.split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def classify(state: State) -> tuple[str, str]:
    """(verdict, reason) for one checkout. Total: every state gets exactly one verdict.

    The branch axis is what matters. On the default branch any local work is
    *stranded* -- `/ship` refuses to open a PR from there (ship.py's
    `is_shippable`), so it needs a branch cut before anything else can happen,
    and that is the case most likely to sit unnoticed for weeks. On a feature
    branch, content means shippable and the only question is how far along it is.
    """
    if not state.is_git:
        return SKIPPED, "not a git checkout"
    if not state.default_branch:
        return BLOCKED, "cannot resolve origin/HEAD -- no base branch to ship against"
    if not state.branch:
        return BLOCKED, "detached HEAD -- check out a branch before shipping"
    if state.host == "none":
        return BLOCKED, "no origin remote -- nowhere to push"

    if state.branch == state.default_branch:
        if state.dirty and state.ahead:
            return NEEDS_BRANCH, (
                f"{state.dirty} uncommitted file(s) and {state.ahead} unpushed "
                f"commit(s) on {state.default_branch}"
            )
        if state.dirty:
            return NEEDS_BRANCH, f"{state.dirty} uncommitted file(s) on {state.default_branch}"
        if state.ahead:
            return NEEDS_BRANCH, (
                f"{state.ahead} commit(s) committed straight to {state.default_branch}, unpushed"
            )
        if state.behind:
            return NEEDS_PULL, f"{state.behind} commit(s) behind origin/{state.default_branch}"
        return CLEAN, "up to date"

    # Feature branch.
    if state.dirty:
        return READY, f"{state.dirty} uncommitted file(s) on a feature branch"
    if state.ahead == 0:
        # Nothing beyond the base: a spent branch (already merged) or a fresh one.
        return CLEAN, f"no commits beyond {state.default_branch}"
    if not state.upstream:
        return READY, f"{state.ahead} commit(s), never pushed"
    if state.unpushed > 0:
        return READY, f"{state.unpushed} commit(s) not yet pushed to {state.upstream}"
    return NEEDS_PR, f"{state.ahead} commit(s) pushed to {state.upstream} -- confirm a PR is open"


def plan_for(state: State, verdict: str) -> list[str]:
    """Ordered next actions for a verdict. Non-empty for every non-terminal verdict.

    This list is the "nothing stranded" contract: if a repo is actionable, the
    sweep says concretely what unblocks it rather than leaving the reader to
    work it out from the state columns.
    """
    if verdict in TERMINAL:
        return []
    if verdict == BLOCKED:
        return ["inspect by hand -- the sweep will not guess at this state"]
    if verdict == NEEDS_PULL:
        return [f"git -C {state.name} pull --ff-only"]

    steps: list[str] = []
    if verdict == NEEDS_BRANCH:
        # task_branch owns the naming so a swept branch is indistinguishable from
        # one the branch-per-task hook cut.
        steps.append(
            f"cut a claude/... branch off HEAD (task_branch.branch_name) so the "
            f"work leaves {state.default_branch}"
        )
    if state.behind:
        # Rebase pre-PR, merge post-PR: rebasing a branch with an open PR detaches
        # its review threads.
        rewrite = "merge" if verdict == NEEDS_PR else "rebase"
        steps.append(
            f"git fetch && git {rewrite} origin/{state.default_branch} "
            f"({state.behind} behind; conflict -> stop and report, never auto-resolve)"
        )
    if verdict == NEEDS_PR:
        steps.append("confirm an open PR exists for this branch; open one if not")
    else:
        steps.append("/ship -- review the diff, commit, push, open the PR")
    return steps


def dedupe_note(results: list[Result]) -> dict[str, list[str]]:
    """Checkout names grouped by shared remote, for the >1 case only.

    `carameli`/`carameli-b` and `ibkr_trader`/`ibkr_trader-b` are separate
    checkouts of the same GitHub repo. Both can strand work independently, so
    both are swept -- but a reader counting open PRs needs to know two rows can
    land on one repo.
    """
    by_remote: dict[str, list[str]] = {}
    for result in results:
        url = result.state.remote_url
        if url:
            by_remote.setdefault(url, []).append(result.state.name)
    return {url: names for url, names in by_remote.items() if len(names) > 1}


def exit_code(results: list[Result]) -> int:
    """0 all clear, 1 something needs action, 2 something is blocked."""
    verdicts = {result.verdict for result in results}
    if BLOCKED in verdicts:
        return 2
    return 1 if verdicts & ACTIONABLE else 0


# --- git IO -----------------------------------------------------------------


def git_for(path: Path) -> Git:
    """A `git(*args)` callable bound to one checkout."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    return git


def _out(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout.strip() if result.returncode == 0 else ""


def inspect(name: str, path: Path, git: Git | None = None, fetch: bool = True) -> State:
    """Read one checkout's git state. `fetch=False` skips the network (stale counts)."""
    if not (path / ".git").exists():
        return State(name=name, path=str(path), is_git=False)
    git = git or git_for(path)

    if fetch:
        git("fetch", "--quiet", "origin")

    remote_url = _out(git("remote", "get-url", "origin"))
    default_branch = tb.detect_default_branch(git, fallback="")
    branch = _out(git("branch", "--show-current"))
    dirty = count_lines(git("status", "--porcelain").stdout or "")

    behind = ahead = 0
    if default_branch:
        behind, ahead = parse_ahead_behind(
            _out(git("rev-list", "--left-right", "--count", f"origin/{default_branch}...HEAD"))
        )

    upstream = _out(git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"))
    unpushed = -1
    if upstream:
        raw = _out(git("rev-list", "--count", "@{u}..HEAD"))
        unpushed = int(raw) if raw.isdigit() else -1

    return State(
        name=name,
        path=str(path),
        is_git=True,
        host=remote_host(remote_url),
        default_branch=default_branch,
        branch=branch,
        dirty=dirty,
        behind=behind,
        ahead=ahead,
        upstream=upstream,
        unpushed=unpushed,
        remote_url=remote_url,
    )


def sweep(root: Path, names: list[str], fetch: bool = True) -> list[Result]:
    """Inspect and classify every named checkout under `root`."""
    results: list[Result] = []
    for name in names:
        state = inspect(name, root / name, fetch=fetch)
        verdict, reason = classify(state)
        results.append(Result(state, verdict, reason, plan_for(state, verdict)))
    return results


# --- reporting --------------------------------------------------------------


def _base_column(state: State) -> str:
    if not state.default_branch:
        return "?"
    return f"-{state.behind}/+{state.ahead}"


def render(results: list[Result]) -> str:
    """The human-readable report: one row per checkout, then the plans."""
    rows = [("PROJECT", "BRANCH", "DIRTY", "vs BASE", "VERDICT")]
    for result in results:
        state = result.state
        rows.append(
            (
                state.name,
                state.branch or ("-" if state.is_git else "n/a"),
                str(state.dirty) if state.is_git else "-",
                _base_column(state) if state.is_git else "-",
                result.verdict,
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))

    actionable = [r for r in results if r.verdict in ACTIONABLE]
    if actionable:
        lines.append("")
        lines.append(f"{len(actionable)} checkout(s) need action:")
        for result in actionable:
            lines.append(f"\n  {result.state.name} [{result.verdict}] -- {result.reason}")
            for i, step in enumerate(result.plan, 1):
                lines.append(f"    {i}. {step}")
    else:
        lines.append("")
        lines.append("Nothing stranded -- every checkout is clean.")

    shared = dedupe_note(results)
    if shared:
        lines.append("")
        lines.append("Note -- checkouts sharing a remote (one repo, two rows):")
        for url, group in sorted(shared.items()):
            lines.append(f"  {', '.join(group)} -> {url}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help=f"the .code-workspace file listing the checkouts (default: {DEFAULT_WORKSPACE.name})",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="checkout name to skip; repeatable (default: VanillaLand)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when any checkout needs action, 2 when any is blocked",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip `git fetch` (fast, but ahead/behind counts may be stale)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.workspace.is_file():
        print(f"sweep: no workspace file at {args.workspace}", file=sys.stderr)
        return 2
    exclude = frozenset(args.exclude) if args.exclude is not None else DEFAULT_EXCLUDE
    names = parse_workspace(args.workspace.read_text(encoding="utf-8"), exclude)
    if not names:
        print(f"sweep: no checkouts listed in {args.workspace.name}", file=sys.stderr)
        return 2

    results = sweep(args.workspace.parent, names, fetch=not args.no_fetch)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": r.state.name,
                        "verdict": r.verdict,
                        "reason": r.reason,
                        "plan": r.plan,
                        "state": asdict(r.state),
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        print(render(results))

    return exit_code(results) if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
