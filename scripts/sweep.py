#!/usr/bin/env python3
"""Cross-project ship sweep: find work stranded across the workspace's checkouts.

One `/ship` run takes one checkout from "task finished" to "PR open". This walks
every checkout in a VS Code multi-root workspace and reports which ones still have
work in them, so nothing sits forgotten on a branch (or, more often, forgotten on
the default branch) while attention is elsewhere.

**Committing, pushing, and opening PRs stay with `/ship`**, which runs per-repo
with the diff in context because that is what a commit message actually needs.
The split is deliberate: the mechanical half (what state is each repo in, what is
the next action) is deterministic and testable and lives here; the semantic half
(is this diff one coherent change, and what is it *for*) does not.

Modes:
  (default)   human-readable table -- the testing/inspection mode.
  --json      the same verdicts as JSON, for a driver to fan out over.
  --check     exit 1 when any repo needs action, 2 when any is blocked. For a
              root-level task that should fail loudly rather than print quietly.
  --branch    cut a `claude/...` branch under work stranded on a branch that
              cannot be shipped from. Step 1 of the sweep.
  --sync      park each worktree back on its home branch, fast-forward it to
              `origin/<default>`, and delete the task branches that have merged.
              Step 2 -- run it once the PRs from step 1 are merged.

**The reporting modes never touch a repository.** `--branch` and `--sync` do, and
both print their plan and change nothing unless `--yes` is also passed. Every step
they emit is a git command that refuses rather than destroys: `merge --ff-only`
never rewrites, `branch -d` never deletes unmerged work.

The classification contract that makes "nothing stranded" checkable: `classify()`
is total -- every repo lands in exactly one verdict -- and every verdict except
`clean`/`skipped` has a non-empty `plan_for()`. Tested in `tests/test_sweep.py`.

Pure and stdlib-only. All git access goes through an injected callable so the
decision logic is unit-testable without spawning git.
"""

from __future__ import annotations

import argparse
import datetime as _dt
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
NEEDS_BRANCH = "needs-branch"  # work sitting on a branch it cannot be shipped from
READY = "ready"  # task branch with content -- /ship it
NEEDS_PR = "needs-pr"  # task branch pushed, PR may not exist
NEEDS_PULL = "needs-pull"  # clean on its home branch, just behind
SPENT = "spent-branch"  # parked on a task branch with nothing on it -- sync it home
CLEAN = "clean"  # nothing to do
SKIPPED = "skipped"  # not a git checkout

# Verdicts that mean "there is work here". `--check` exits non-zero on these.
ACTIONABLE: frozenset[str] = frozenset({
    BLOCKED, NEEDS_BRANCH, READY, NEEDS_PR, NEEDS_PULL, SPENT
})  # fmt: skip
# Verdicts with no next action. Every *other* verdict must yield a plan.
TERMINAL: frozenset[str] = frozenset({CLEAN, SKIPPED})

# Verdicts `--branch` acts on, and the ones `--sync` acts on. Disjoint by
# construction: step 1 moves work onto task branches, step 2 tidies up once the
# resulting PRs have merged, and neither touches a repo the other owns.
BRANCHABLE: frozenset[str] = frozenset({NEEDS_BRANCH})
SYNCABLE: frozenset[str] = frozenset({SPENT, NEEDS_PULL, CLEAN})

# Per-worktree marker recording which branch this worktree calls home, written
# under the worktree's own git dir (`git rev-parse --git-path`) so two worktrees
# of one repo get separate values -- the same mechanism as `tb.SHIPPED_MARKER_NAME`.
# Needed because a *linked* worktree cannot park on the default branch: git allows
# one checkout of a branch at a time, and the primary worktree already holds it.
ANCHOR_MARKER_NAME = "agent-anchor"


@dataclass(frozen=True)
class State:
    """Everything the classifier needs about one checkout.

    Built by `inspect()` from git, or by hand in tests. `behind`/`ahead` are
    measured against `origin/<default_branch>`; `unpushed` against the branch's
    own upstream (-1 when it has none, which is different from 0).

    The last four fields exist for `--sync`, which has to decide where a worktree
    goes *after* its task branch is spent -- a question the report modes never ask.
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
    # True for a linked worktree (`.git` is a file, not a directory).
    linked: bool = False
    # This worktree's recorded home branch, from ANCHOR_MARKER_NAME; "" when unset.
    anchor: str = ""
    # Local branch names, and the `claude/...` ones already merged into
    # origin/<default_branch> -- what `--sync` deletes.
    local_branches: tuple[str, ...] = ()
    merged_task_branches: tuple[str, ...] = ()
    # Branches checked out in *any* worktree of this repo, this one included.
    # Reaping is repo-wide but a checkout is per-worktree, so without this a
    # sibling's live branch looks like a merged branch nobody is using.
    worktree_branches: tuple[str, ...] = ()


@dataclass
class Result:
    """A classified checkout: its state, its verdict, and what to do about it."""

    state: State
    verdict: str
    reason: str
    plan: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Plan:
    """A mutation `--branch`/`--sync` would perform on one checkout.

    `steps` are git argv fragments (no `git`, no `-C <path>` -- the runner binds
    those), run in order, stopping at the first failure. `anchor` is the branch to
    record in ANCHOR_MARKER_NAME afterwards, "" for none. A non-empty `refusal`
    means do nothing at all and say why: an empty `steps` with no refusal is the
    already-correct case, which is not the same thing.
    """

    steps: tuple[tuple[str, ...], ...] = ()
    anchor: str = ""
    refusal: str = ""


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


def parse_worktree_branches(text: str) -> tuple[str, ...]:
    """Branch names held by the repo's worktrees, from `git worktree list --porcelain`.

    Detached worktrees emit `detached` instead of a `branch` line and are simply
    absent from the result -- they hold no branch, so they block no delete.
    """
    prefix = "branch refs/heads/"
    return tuple(
        line[len(prefix) :].strip()
        for line in text.splitlines()
        if line.startswith(prefix) and line[len(prefix) :].strip()
    )


def parse_ahead_behind(text: str) -> tuple[int, int]:
    """(behind, ahead) from `rev-list --left-right --count base...HEAD`; (0, 0) if unparseable."""
    parts = text.split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def is_task_branch(branch: str) -> bool:
    """True for a `claude/...` branch -- the only kind a PR is opened from.

    Everything else is a *home* branch: the default branch, or a long-lived
    worktree anchor like `carameli-b`. The distinction is the whole basis of the
    branch axis below, so it is one predicate rather than a repeated startswith.
    """
    return branch.startswith(tb.BRANCH_PREFIX)


def home_ref(state: State) -> str:
    """The branch this worktree parks on between tasks; "" when it cannot be resolved.

    In order: the recorded anchor; the current branch if it is already a home
    branch; the default branch for a primary worktree; and for a linked worktree
    with none of those, a local branch named after the checkout directory. The
    last fallback is what lets a never-before-synced worktree find its way home
    (`carameli-b` the directory -> `carameli-b` the branch) without guessing at a
    branch that does not exist.

    A linked worktree cannot fall back to the default branch the way a primary can
    -- the primary already has it checked out and git permits only one holder --
    so exhausting the list returns "" and `sync_plan` refuses rather than guesses.
    """
    if state.anchor:
        return state.anchor
    if state.branch and not is_task_branch(state.branch):
        return state.branch
    if not state.linked:
        return state.default_branch
    return state.name if state.name in state.local_branches else ""


def classify(state: State) -> tuple[str, str]:
    """(verdict, reason) for one checkout. Total: every state gets exactly one verdict.

    The branch axis is what matters, and it splits on `is_task_branch`, not on
    "is this the default branch". Work on *any* home branch is stranded: `/ship`
    would happily open a PR from a long-lived worktree anchor like `carameli-b`
    (ship.py's `is_shippable` only refuses the default branch), which quietly
    turns a permanent worktree branch into a one-off PR branch. Both cases need
    the same fix -- a task branch cut underneath the work -- so both get
    `needs-branch` and `--branch` handles them identically.

    On a task branch, content means shippable and the only question is how far
    along it is. *No* content means the branch is spent -- merged, or cut and
    never used -- which is `spent-branch` rather than `clean` because the worktree
    is still parked on it and `--sync` has something to do.
    """
    if not state.is_git:
        return SKIPPED, "not a git checkout"
    if not state.default_branch:
        return BLOCKED, "cannot resolve origin/HEAD -- no base branch to ship against"
    if not state.branch:
        return BLOCKED, "detached HEAD -- check out a branch before shipping"
    if state.host == "none":
        return BLOCKED, "no origin remote -- nowhere to push"

    if not is_task_branch(state.branch):
        # An anchor is not the default branch, so say which branch is affected --
        # "on carameli-b" and "on main" need different fixes from the reader.
        where = state.branch
        if state.branch != state.default_branch:
            where += f" (not a {tb.BRANCH_PREFIX} task branch)"
        if state.dirty and state.ahead:
            return NEEDS_BRANCH, (
                f"{state.dirty} uncommitted file(s) and {state.ahead} unpushed commit(s) on {where}"
            )
        if state.dirty:
            return NEEDS_BRANCH, f"{state.dirty} uncommitted file(s) on {where}"
        if state.ahead:
            return NEEDS_BRANCH, f"{state.ahead} commit(s) committed straight to {where}, unpushed"
        if state.behind:
            return NEEDS_PULL, f"{state.behind} commit(s) behind origin/{state.default_branch}"
        return CLEAN, "up to date"

    # Task branch.
    if state.dirty:
        return READY, f"{state.dirty} uncommitted file(s) on a task branch"
    if state.ahead == 0:
        # Nothing beyond the base: merged and spent, or cut and never used. Either
        # way the worktree should not still be sitting here.
        return SPENT, f"no commits beyond {state.default_branch} -- branch is spent"
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
        # On the default branch this is a plain pull; on an anchor there is nothing
        # to pull *from* (its upstream is not origin/<default>), so it fast-forwards
        # onto the default branch instead. Both are what `--sync` emits.
        if state.branch == state.default_branch:
            return [f"git -C {state.name} pull --ff-only", "or: sweep.py --sync --yes"]
        return [
            f"git -C {state.name} merge --ff-only origin/{state.default_branch}",
            "or: sweep.py --sync --yes",
        ]
    if verdict == SPENT:
        home = home_ref(state)
        if not home:
            return [
                f"cannot resolve a home branch for the linked worktree {state.name} -- "
                f"check out its anchor branch by hand, then sweep.py --sync"
            ]
        return [
            f"git -C {state.name} checkout {home} && git merge --ff-only "
            f"origin/{state.default_branch}",
            f"git -C {state.name} branch -d {state.branch} (spent)",
            "or: sweep.py --sync --yes",
        ]

    steps: list[str] = []
    if verdict == NEEDS_BRANCH:
        # task_branch owns the naming so a swept branch is indistinguishable from
        # one the branch-per-task hook cut.
        steps.append(
            f"cut a {tb.BRANCH_PREFIX}... branch off HEAD (task_branch.branch_name) "
            f"so the work leaves {state.branch} -- sweep.py --branch --yes"
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


# --- mutation plans ---------------------------------------------------------
# Both are pure: they turn a State into git argv and nothing else, so every
# destructive-looking step is asserted in a test without a repo on disk.


def branch_plan(state: State, slug: str = "sweep", today: _dt.date | None = None) -> Plan:
    """Step 1: get stranded work onto a `claude/...` branch it can be shipped from.

    The new branch is cut from HEAD, not from `origin/<default>`, so a dirty tree
    comes along untouched (`tb.checkout_base` makes the same call for the same
    reason: resetting onto the base could clobber uncommitted work).

    When the home branch carried commits, it is reset to `origin/<default>` here
    and only here. This is the one moment that is provably safe -- the task branch
    was just cut from that exact HEAD, so the commits have a second home before
    the first one moves. Leaving it would strand the home branch permanently
    diverged: once the PR merges as a squash or a merge commit, those commits are
    not ancestors of `origin/<default>` and `--sync`'s fast-forward can never
    succeed again.
    """
    if is_task_branch(state.branch):
        return Plan(refusal=f"already on a task branch ({state.branch})")
    if not state.branch or not state.default_branch:
        return Plan(refusal="no branch to cut from -- resolve the blocked state first")

    name = tb.branch_name(tb.slugify(slug), set(state.local_branches), today)
    steps: list[tuple[str, ...]] = [("checkout", "-b", name)]
    if state.ahead:
        steps.append(("branch", "-f", state.branch, f"origin/{state.default_branch}"))
    # Remember where the work came from so `--sync` can put the worktree back.
    return Plan(steps=tuple(steps), anchor=state.branch)


def sync_plan(state: State, verdict: str, fetch: bool = True) -> Plan:
    """Step 2: park a checkout on its home branch, current, with the spent branches gone.

    Refuses outright on anything holding unshipped work. That is the ordering the
    two steps depend on -- syncing a checkout that still has a PR in flight would
    move it off the branch under review -- and it is why `--sync` is safe to run
    over the whole workspace while some repos are mid-flight.
    """
    if verdict == SKIPPED:
        return Plan()
    if verdict == BLOCKED:
        return Plan(refusal="blocked -- inspect by hand")
    if verdict not in SYNCABLE:
        return Plan(refusal=f"{verdict} -- unshipped work here; /ship it before syncing")

    home = home_ref(state)
    if not home:
        return Plan(
            refusal=(
                f"cannot resolve a home branch: {state.name} is a linked worktree on a "
                f"task branch with no recorded anchor and no local branch named "
                f"{state.name}. Check out its anchor by hand once and re-run."
            )
        )

    steps: list[tuple[str, ...]] = []
    if fetch:
        steps.append(("fetch", "--prune", "origin"))
    if state.branch != home:
        steps.append(("checkout", home))
    # Unconditional: `behind` was measured against the *old* HEAD, so after a
    # checkout it says nothing about where `home` sits. `--ff-only` makes an
    # already-current branch a no-op and a diverged one an error, never a merge.
    steps.append(("merge", "--ff-only", f"origin/{state.default_branch}"))

    # Everything merged, plus the spent branch we are standing on (trivially
    # merged, so `-d` takes it). Never `home` itself, and never `-D`: an unmerged
    # branch surviving this is the correct outcome, not a failure to force past.
    #
    # Never a branch a *sibling* worktree is on either. Reaping is repo-wide while
    # a checkout is per-worktree, so `carameli-b` sees the branch `carameli` is
    # working on, merged into origin/master, and reads it as abandoned. Git would
    # refuse the delete, but a plan that proposes it is a plan that reports a
    # failure on a healthy workspace -- and the dry run stops being trustworthy.
    live_elsewhere = {b for b in state.worktree_branches if b != state.branch}
    doomed: list[str] = []
    for candidate in (*state.merged_task_branches, state.branch if verdict == SPENT else ""):
        if not is_task_branch(candidate) or candidate in doomed:
            continue
        if candidate != home and candidate not in live_elsewhere:
            doomed.append(candidate)
    steps.extend(("branch", "-d", branch) for branch in doomed)

    # Only linked worktrees need the record: a primary can always fall back to the
    # default branch, so storing it there would just be a value that can go stale.
    return Plan(steps=tuple(steps), anchor=home if state.linked else "")


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


def anchor_path(git: Git, path: Path) -> Path | None:
    """Where this worktree's anchor marker lives, or None if git will not say.

    `rev-parse --git-path` resolves to the *worktree's* git dir
    (`.git/worktrees/<name>/`) for a linked checkout and to `.git/` for a primary,
    which is exactly the per-worktree scoping the marker needs. The returned path
    is relative to the repo, and we run git with `-C path`, so join it back onto
    `path` when it is not absolute.
    """
    raw = _out(git("rev-parse", "--git-path", ANCHOR_MARKER_NAME))
    if not raw:
        return None
    marker = Path(raw)
    return marker if marker.is_absolute() else path / marker


def read_anchor(git: Git, path: Path) -> str:
    """The recorded home branch for this worktree; "" when unset or unreadable."""
    marker = anchor_path(git, path)
    if marker is None or not marker.is_file():
        return ""
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_anchor(git: Git, path: Path, branch: str) -> None:
    """Record `branch` as this worktree's home. Best-effort: a failure to write
    only costs the dirname fallback in `home_ref`, never correctness."""
    marker = anchor_path(git, path)
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{branch}\n", encoding="utf-8")
    except OSError:
        pass


def inspect(name: str, path: Path, git: Git | None = None, fetch: bool = True) -> State:
    """Read one checkout's git state. `fetch=False` skips the network (stale counts)."""
    dot_git = path / ".git"
    if not dot_git.exists():
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

    local_branches = tuple(
        _out(git("for-each-ref", "--format=%(refname:short)", "refs/heads/")).splitlines()
    )
    merged: tuple[str, ...] = ()
    if default_branch:
        merged = tuple(
            line
            for line in _out(
                git(
                    "branch",
                    "--merged",
                    f"origin/{default_branch}",
                    "--format=%(refname:short)",
                )
            ).splitlines()
            if is_task_branch(line)
        )

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
        # A linked worktree's `.git` is a file pointing at the primary's git dir.
        linked=dot_git.is_file(),
        anchor=read_anchor(git, path),
        local_branches=local_branches,
        merged_task_branches=merged,
        worktree_branches=parse_worktree_branches(_out(git("worktree", "list", "--porcelain"))),
    )


# --- executing a plan -------------------------------------------------------


@dataclass
class Applied:
    """What running one checkout's plan actually did."""

    name: str
    plan: Plan
    ran: list[str] = field(default_factory=list)
    failed: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed and not self.plan.refusal


def apply_plan(name: str, path: Path, plan: Plan, git: Git | None = None) -> Applied:
    """Run a plan's steps in order, stopping at the first failure.

    Stopping matters: the steps are ordered so that a later one is only safe once
    the earlier ones succeeded (nothing is deleted before the checkout that moved
    off it), so continuing past a failure is how a safe plan turns unsafe.
    """
    result = Applied(name=name, plan=plan)
    if plan.refusal:
        return result
    git = git or git_for(path)
    for step in plan.steps:
        completed = git(*step)
        rendered = "git " + " ".join(step)
        if completed.returncode != 0:
            result.failed = rendered
            result.error = (completed.stderr or completed.stdout or "").strip()
            return result
        result.ran.append(rendered)
    if plan.anchor:
        write_anchor(git, path, plan.anchor)
    return result


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


def render_plans(mode: str, planned: list[tuple[Result, Plan]], applied: bool) -> str:
    """The `--branch`/`--sync` report -- the same text whether or not `--yes` ran it.

    Deliberately identical in both modes: a dry run is only trustworthy if what it
    prints is what the real run does, so the plan is rendered from the same `Plan`
    objects the runner consumes.
    """
    verb = "Applied" if applied else "Would run"
    lines = [f"{mode}: {verb.lower()} the following."]
    acting = [(r, p) for r, p in planned if p.steps]
    refused = [(r, p) for r, p in planned if p.refusal]

    if acting:
        for result, plan in acting:
            lines.append(f"\n  {result.state.name} [{result.verdict}] -- {result.reason}")
            for i, step in enumerate(plan.steps, 1):
                lines.append(f"    {i}. git -C {result.state.name} {' '.join(step)}")
            if plan.anchor:
                lines.append(f"    -> record home branch '{plan.anchor}' for this worktree")
    else:
        lines.append("\n  Nothing to do -- no checkout is in a state this mode acts on.")

    if refused:
        lines.append(f"\n{len(refused)} checkout(s) skipped:")
        for result, plan in refused:
            lines.append(f"  {result.state.name} -- {plan.refusal}")
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


def render_applied(results: list[Applied]) -> str:
    """What actually happened, including the failures. Failures never abort the
    sweep: one repo that will not fast-forward should not stop the other four."""
    lines: list[str] = []
    failures = [r for r in results if r.failed]
    for result in results:
        if result.failed:
            lines.append(f"  {result.name}: FAILED at `{result.failed}`")
            for line in result.error.splitlines():
                lines.append(f"      {line}")
        elif result.ran:
            lines.append(f"  {result.name}: {len(result.ran)} step(s) ok")
    if failures:
        lines.append(
            f"\n{len(failures)} checkout(s) failed. Nothing was forced -- git refused, "
            f"which means the state needs a human. Fix and re-run."
        )
    return "\n".join(lines)


def run_mode(
    root: Path,
    results: list[Result],
    mode: str,
    apply: bool,
    fetch: bool = True,
    slug: str = "sweep",
) -> tuple[str, int]:
    """Plan (and optionally apply) `--branch` or `--sync` across the swept checkouts.

    Returns the report and the exit code: 0 clean, 1 something still needs action,
    2 a step failed.
    """
    planned: list[tuple[Result, Plan]] = []
    for result in results:
        if mode == "branch":
            plan = (
                branch_plan(result.state, slug=slug)
                if result.verdict in BRANCHABLE
                else Plan(refusal=f"{result.verdict} -- not stranded on a home branch")
            )
        else:
            plan = sync_plan(result.state, result.verdict, fetch=fetch)
        # A skipped non-git directory is noise in this report, not a refusal.
        if result.verdict == SKIPPED:
            continue
        planned.append((result, plan))

    report = render_plans(mode, planned, applied=apply)
    if not apply:
        return report, 1 if any(p.steps for _, p in planned) else 0

    ran = [
        apply_plan(result.state.name, root / result.state.name, plan)
        for result, plan in planned
        if plan.steps
    ]
    detail = render_applied(ran)
    if detail:
        report = f"{report}\n\nResults:\n{detail}"
    return report, 2 if any(r.failed for r in ran) else 0


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
    # Paired for the same reason as --dry-run/--yes below: the VS Code picker has
    # to emit a real token on both branches.
    fetch_mode = parser.add_mutually_exclusive_group()
    fetch_mode.add_argument(
        "--fetch",
        dest="fetch",
        action="store_true",
        default=True,
        help="fetch every remote before reading its state (the default)",
    )
    fetch_mode.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="skip `git fetch` (fast, but ahead/behind counts may be stale)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--branch",
        action="store_true",
        help="step 1: cut a claude/... branch under work stranded on a home branch",
    )
    mode.add_argument(
        "--sync",
        action="store_true",
        help="step 2: park each worktree on its home branch, fast-forward, drop merged branches",
    )
    # `--dry-run` is redundant with the default and exists anyway: the VS Code task
    # picks one of these two strings, and passing "" instead would reach argparse as
    # a stray positional and be rejected. Same reason new-project.py carries it.
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print what --branch/--sync would do, and change nothing (the default)",
    )
    apply_mode.add_argument(
        "--yes",
        dest="dry_run",
        action="store_false",
        help="actually run --branch/--sync",
    )
    parser.add_argument(
        "--slug",
        default="sweep",
        help=(
            "topic for the branch names --branch cuts (default: sweep -> "
            "claude/sweep-<mmdd>). There is no prompt to derive one from here, so "
            "pass what the stranded work is about when it is worth naming"
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.dry_run and not (args.branch or args.sync):
        parser.error("--yes has no effect without --branch or --sync")

    if not args.workspace.is_file():
        print(f"sweep: no workspace file at {args.workspace}", file=sys.stderr)
        return 2
    exclude = frozenset(args.exclude) if args.exclude is not None else DEFAULT_EXCLUDE
    names = parse_workspace(args.workspace.read_text(encoding="utf-8"), exclude)
    if not names:
        print(f"sweep: no checkouts listed in {args.workspace.name}", file=sys.stderr)
        return 2

    results = sweep(args.workspace.parent, names, fetch=args.fetch)

    if args.branch or args.sync:
        report, code = run_mode(
            args.workspace.parent,
            results,
            "branch" if args.branch else "sync",
            apply=not args.dry_run,
            fetch=args.fetch,
            slug=args.slug,
        )
        print(report)
        return code

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
