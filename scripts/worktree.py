#!/usr/bin/env python3
"""Ephemeral worktrees: spawn a disposable box, ship out of it, destroy it.

The static tier — `carameli`, `carameli-b`, one permanent slot each in `ports.toml`
— caps concurrency at two per project and, worse, makes every checkout *outlive* the
task that used it. That is where `sweep.py`'s workload comes from: `needs-branch`,
`needs-rebranch`, `spent-branch`, the anchor marker, `home_ref`, `dedupe_reaps` are
all states a checkout can only reach by surviving its task. A box cut fresh off
`origin/<default>` onto a `claude/...` branch and destroyed at the end cannot reach
any of them.

So this is not "sweep, but faster". It is the other half of the model:

| | Static checkout | Ephemeral box |
| --- | --- | --- |
| Lives in | `<workspace>/<project>` | `<workspace>/.worktrees/<box>` |
| Listed in the workspace file | yes | **no** — invisible to `sweep.py` by design |
| Port slot | pinned in `ports.toml` `[slots]` | leased on spawn, released on reap |
| Ends by | `sweep --sync` parking it home | `reap` deleting it |
| Stranded work is | found afterwards | **impossible**: reap refuses until it ships |

That last row is the point. `sweep.py` searches for work that got left behind;
`reap` simply will not free the box until the work has left it. Same guarantee,
enforced at the only moment it is cheap to enforce.

Modes:
  new <project>   cut a worktree on a fresh task branch off `origin/<default>`,
                  lease a port slot, seed its `.env`. Prints the path.
  list            every live box, its branch, its verdict, and whether it can be
                  reaped. Reuses `sweep.inspect`/`sweep.classify` — one classifier
                  for both tiers, so the two can never disagree about "has work".
  reap <box>      tear the stack down, remove the worktree, delete the branch,
                  release the lease. **Refuses while the box still holds work.**

`new` and `reap` print their plan and change nothing unless `--yes` is passed, the
same contract `sweep.py`'s mutating modes keep.

The decision logic is pure and stdlib-only: every planner turns a `Box` plus a
`sweep.State` into argv and nothing else, so the destructive steps are asserted in
`tests/test_worktree.py` without a repo, a daemon, or a network.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_ports
import devkit_project
import sweep
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = REPO_ROOT.parent / "alex-projects.code-workspace"

# Ephemeral boxes live *beside* the checkouts, never inside one: a worktree nested in
# a project would show up as untracked files in that project's `git status`, which is
# the `needs-branch` verdict this whole tier exists to stop manufacturing.
BOXES_DIR_NAME = ".worktrees"
LEASE_FILE_NAME = "leases.json"

# Separates the project from the branch topic in a box name. Two hyphens rather than
# one because project names already contain hyphens (`apt-finder`) and the box name is
# parsed back apart by `list`.
NAME_SEP = "--"

# Verdicts that mean the work has left the box, so the box is free to destroy.
#   spent-branch  nothing beyond the base — nothing to lose
#   needs-pr      pushed, nothing unpushed — the remote has every commit
#   clean         nothing to do (a box that never got used)
# Everything else — `ready` above all — means work is still only here.
SAFE_TO_REAP: frozenset[str] = frozenset({sweep.SPENT, sweep.NEEDS_PR, sweep.CLEAN})

# Marks the block `new` writes into a box's `.env`. Docker Compose's dotenv parser
# takes the LAST assignment of a key, so appending is what lets the block win over a
# seeded copy of the project's own `.env` without editing the lines it came with.
MANAGED_BEGIN = "# --- devkit worktree: managed block (rewritten on every spawn) ---"
MANAGED_END = "# --- end devkit worktree block ---"


class WorktreeError(ValueError):
    """The request names a project, box, or state this tool will not act on."""


@dataclass(frozen=True)
class Box:
    """One ephemeral worktree, as recorded in the lease file.

    `slot` is -1 for a project with no Docker tier: there is nothing to publish, so
    nothing is leased and the registry's ceiling is not spent on it.

    `session` is the agent session that spawned it, and is what makes the guard hook
    idempotent — the second edit into a project during one session finds this box
    instead of cutting a second one.
    """

    name: str
    project: str
    branch: str
    slot: int = -1
    session: str = ""
    created: str = ""


@dataclass(frozen=True)
class SpawnPlan:
    """What `new` would do: git argv run in the *source checkout*, plus the env to seed."""

    box: Box
    path: str
    steps: tuple[tuple[str, ...], ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReapPlan:
    """What `reap` would do.

    `stack_down` is a separate flag rather than another entry in `steps` for the same
    reason `sweep.Plan.pr_title` is: `steps` stays homogeneous git argv, which is what
    lets one safety test read every step in the file and mean it.
    """

    box: str
    path: str = ""
    project: str = ""
    steps: tuple[tuple[str, ...], ...] = ()
    stack_down: bool = False
    slot: int = -1
    refusal: str = ""
    warning: str = ""

    @property
    def acts(self) -> bool:
        return bool(self.steps or self.stack_down)


# --- pure helpers -----------------------------------------------------------


def boxes_root(workspace_root: Path) -> Path:
    """Where every ephemeral worktree for this workspace lives."""
    return workspace_root / BOXES_DIR_NAME


def lease_file(workspace_root: Path) -> Path:
    return boxes_root(workspace_root) / LEASE_FILE_NAME


def box_name(project: str, branch: str) -> str:
    """`carameli` + `claude/voicemail-0806` -> `carameli--voicemail-0806`.

    Also the box's `COMPOSE_PROJECT_NAME`, which is what namespaces its containers,
    network and volumes — the same identity `ports.toml` requires of a static
    checkout, so the two tiers can never collide in the Docker daemon. Compose
    accepts `[a-z0-9][a-z0-9_-]*`, which both halves already satisfy: project names
    come from directory names and the topic from `tb.slugify`.
    """
    topic = branch[len(tb.BRANCH_PREFIX) :] if sweep.is_task_branch(branch) else branch
    return f"{project}{NAME_SEP}{topic}"


def box_path(workspace_root: Path, name: str) -> Path:
    return boxes_root(workspace_root) / name


def project_of(name: str) -> str:
    """The project a box name was cut from; "" when the name is not a box name."""
    return name.split(NAME_SEP, 1)[0] if NAME_SEP in name else ""


def parse_leases(text: str) -> dict[str, Box]:
    """Boxes from the lease file's contents. Unreadable content is no boxes, not a crash.

    Falling back to empty is safe in one direction only, and it is the right one: a
    lost lease means a slot is re-offered and a `docker compose up` fails loudly on a
    taken port. Refusing to spawn because a JSON file got truncated would take the
    whole tier down instead.
    """
    try:
        payload = json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    entries = payload.get("boxes") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return {}
    boxes: dict[str, Box] = {}
    for name, raw in entries.items():
        if not isinstance(raw, dict):
            continue
        boxes[name] = Box(
            name=name,
            project=str(raw.get("project", project_of(name))),
            branch=str(raw.get("branch", "")),
            slot=raw.get("slot", -1) if isinstance(raw.get("slot"), int) else -1,
            session=str(raw.get("session", "")),
            created=str(raw.get("created", "")),
        )
    return boxes


def render_leases(boxes: Mapping[str, Box]) -> str:
    """The lease file's contents for `boxes`, stable-ordered so diffs stay readable."""
    payload = {
        "boxes": {
            name: {k: v for k, v in asdict(boxes[name]).items() if k != "name"}
            for name in sorted(boxes)
        }
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def next_lease_slot(registry: devkit_ports.Registry, boxes: Mapping[str, Box]) -> int:
    """The lowest port slot free across BOTH tiers.

    `registry.next_free_slot()` only knows `[slots]` — the pinned checkouts — because
    that file is hand-maintained and a box is not in it. Handing out a slot that a
    live box already holds produces the exact "port is already allocated" failure the
    registry exists to prevent, so the two claim sets are unioned here.
    """
    taken = set(registry.slots.values()) | {b.slot for b in boxes.values() if b.slot >= 0}
    for candidate in range(registry.max_slots):
        if candidate not in taken:
            return candidate
    raise devkit_ports.RegistryError(
        f"all {registry.max_slots} port slots are in use ({len(registry.slots)} pinned "
        f"checkouts, {sum(1 for b in boxes.values() if b.slot >= 0)} live boxes). Reap a "
        f"box, raise registry.max_slots, or stop the project's stack publishing to the "
        f"host — a box that runs its tests inside the compose network needs no slot."
    )


def find_session_box(boxes: Mapping[str, Box], project: str, session: str) -> Box | None:
    """The box this session already has for `project`, if any.

    What makes the guard hook cheap to fire on every edit: one box per (session,
    project), not one per edit.
    """
    if not session:
        return None
    for box in boxes.values():
        if box.project == project and box.session == session:
            return box
    return None


def managed_env(box: str, registry: devkit_ports.Registry | None, slot: int) -> dict[str, str]:
    """The env a box's stack needs to stand apart from every other stack.

    `COMPOSE_PROJECT_NAME` is the load-bearing one: it namespaces containers, network
    and volumes, and it is what makes the `-v` in `reap` safe (see `reap_plan`).
    The port variables only matter for a project whose compose file publishes to the
    host; one that does not still gets the project name.
    """
    env = {"COMPOSE_PROJECT_NAME": box}
    if registry is not None and slot >= 0:
        env.update(registry.env_for_slot(slot))
    return env


def render_env(source: str, managed: Mapping[str, str]) -> str:
    """`source` with the managed block appended, replacing an earlier one.

    A fresh worktree checks out **tracked files only**, so a project whose stack reads
    a gitignored `.env` gets none and its compose run fails on missing variables. The
    fix is to seed a copy of the source checkout's file and then override the handful
    of keys that must differ — which works because compose's dotenv parser takes the
    last assignment of a duplicated key, so nothing in the seeded half needs editing.
    """
    kept: list[str] = []
    skipping = False
    for line in source.splitlines():
        if line.strip() == MANAGED_BEGIN:
            skipping = True
        elif line.strip() == MANAGED_END:
            skipping = False
        elif not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [
        MANAGED_BEGIN,
        *[f"{key}={value}" for key, value in sorted(managed.items())],
        MANAGED_END,
    ]
    return "\n".join([*kept, "", *block]) + "\n"


def spawn_plan(
    project: str,
    workspace_root: Path,
    slug: str,
    default_branch: str,
    existing_branches: set[str],
    boxes: Mapping[str, Box],
    registry: devkit_ports.Registry | None = None,
    session: str = "",
    fetch: bool = True,
    today: _dt.date | None = None,
) -> SpawnPlan:
    """Everything `new` will run, decided without touching git.

    The branch is cut from `origin/<default_branch>`, **not** from the source
    checkout's HEAD. That is the one place this differs from `sweep.branch_plan`, and
    the reason is the whole difference between the tiers: sweep is rescuing work that
    already exists in a dirty tree, so it must branch from HEAD or clobber it. A box
    starts empty, so starting anywhere but the tip of the default branch would hand
    the task a stale base for no benefit.

    `--no-track` for the reason `tb.checkout_argv` documents at length: branching off
    a remote-tracking ref makes `origin/<default>` the new branch's upstream, and a
    later bare `git push` then lands the task's commits straight on the default
    branch.
    """
    branch = tb.branch_name(tb.slugify(slug), existing_branches, today)
    name = box_name(project, branch)
    path = box_path(workspace_root, name)
    slot = next_lease_slot(registry, boxes) if registry is not None else -1

    steps: list[tuple[str, ...]] = []
    if fetch:
        steps.append(("fetch", "--quiet", "origin"))
    steps.append(
        ("worktree", "add", "--no-track", "-b", branch, str(path), f"origin/{default_branch}")
    )
    box = Box(
        name=name,
        project=project,
        branch=branch,
        slot=slot,
        session=session,
        created=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    )
    return SpawnPlan(
        box=box,
        path=str(path),
        steps=tuple(steps),
        env=managed_env(name, registry, slot),
    )


def reap_decision(verdict: str, reason: str, force: bool) -> tuple[bool, str]:
    """`(allowed, note)` — may this box be destroyed, and what to say about it.

    This is the inversion that replaces sweeping. Work cannot be stranded in a box
    because the only way to free the box is to have got the work out of it first, and
    that is checked here rather than discovered by a sweep days later.

    `--force` is deliberately available and deliberately narrow. It discards the
    *worktree*, so uncommitted edits go; it never upgrades `branch -d` to `-D`
    (`branch_delete_flag`), so committed work survives as a local branch even when the
    box it was made in does not. Uncommitted junk should not need a human; commits
    should never be destroyed by a cleanup command.
    """
    if verdict in SAFE_TO_REAP:
        return True, ""
    if force:
        return True, f"forced past `{verdict}` ({reason}) — uncommitted changes will be discarded"
    return False, (
        f"{verdict} — {reason}. The work is still only in this box: /ship it, or pass "
        f"--force to discard the uncommitted part (commits survive on the branch)."
    )


def branch_delete_flag(state: sweep.State, pr_merged: bool) -> str:
    """`-d` or `-D` for the box's branch — `-D` only when the remote already has it.

    `-d` refuses a branch that is not an ancestor of the default branch, which is the
    correct default and also wrong for the two commonest ways a box legitimately ends:
    a squash-merged PR (the content is on the default branch but the commits are not
    ancestors of anything) and a PR still open (pushed, not merged at all). In both,
    every commit exists on the remote, so the local ref is a copy and `-D` destroys
    nothing. Anywhere else, `-d` is left to refuse — that refusal is the last guard
    between a cleanup command and someone's only copy.
    """
    if pr_merged:
        return "-D"
    if state.upstream and state.unpushed == 0:
        return "-D"
    return "-d"


def reap_plan(
    box: Box,
    workspace_root: Path,
    state: sweep.State,
    verdict: str,
    reason: str,
    pr_merged: bool = False,
    force: bool = False,
    keep_stack: bool = False,
    has_stack: bool = False,
) -> ReapPlan:
    """Everything `reap` will run, in the only order that is safe.

    The stack comes down first, while the box's compose file still exists to describe
    it; then the worktree; then the branch, which has to be deleted from the *source*
    checkout because the worktree that held it is gone by then.

    **This is the one place in the workspace that passes `-v` to `compose down`**, and
    the exception is narrow enough to state precisely: `docker-maint.py` must never do
    it because its target is a static checkout whose named volumes hold a real dev
    database costing hours to re-ingest. A box's volumes were created minutes ago by
    the box, are namespaced to `COMPOSE_PROJECT_NAME`, and leaking one per task is how
    the WSL2 VHDX becomes the next bottleneck. `-p <box>` is passed explicitly so the
    scope cannot silently widen to the source project if the box's `.env` is missing.
    """
    path = str(box_path(workspace_root, box.name))
    allowed, note = reap_decision(verdict, reason, force)
    if not allowed:
        return ReapPlan(box=box.name, path=path, project=box.project, refusal=note)

    remove: tuple[str, ...] = ("worktree", "remove", path)
    if force:
        remove = (*remove, "--force")
    steps: list[tuple[str, ...]] = [remove]
    if box.branch:
        steps.append(("branch", branch_delete_flag(state, pr_merged), box.branch))
    return ReapPlan(
        box=box.name,
        path=path,
        project=box.project,
        steps=tuple(steps),
        stack_down=has_stack and not keep_stack,
        slot=box.slot,
        warning=note,
    )


# --- IO ---------------------------------------------------------------------


def _compose_files() -> tuple[str, ...]:
    """The compose filenames, from `docker-maint.py` rather than a second copy.

    Loaded by path because the file is hyphenated, and through the shared loader
    because this repo has been bitten twice by hand-rolled ones (see
    `scripts/precommit/_loader.py`). A missing sibling degrades to a stated fallback,
    never to silently deciding a box has no stack — that would leak its containers.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit"))
        from _loader import load_by_path

        module = load_by_path("docker_maint", REPO_ROOT / "scripts" / "docker-maint.py")
        return tuple(module.COMPOSE_FILES)
    except (ImportError, OSError, AttributeError):
        print(
            "worktree: cannot read docker-maint.py's compose filenames; falling back to "
            "the built-in list. Stack teardown may miss a non-standard filename.",
            file=sys.stderr,
        )
        return ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def has_stack(path: Path) -> bool:
    """True when there is a compose stack in `path` to bring up and tear down."""
    return any((path / name).is_file() for name in _compose_files())


def read_leases(workspace_root: Path) -> dict[str, Box]:
    path = lease_file(workspace_root)
    try:
        return parse_leases(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def write_leases(workspace_root: Path, boxes: Mapping[str, Box]) -> None:
    path = lease_file(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_leases(boxes), encoding="utf-8", newline="\n")


def seed_env(source: Path, target: Path, env: Mapping[str, str]) -> None:
    """Write the box's `.env`: the source checkout's, plus the managed overrides."""
    try:
        existing = source.read_text(encoding="utf-8") if source.is_file() else ""
    except OSError:
        existing = ""
    try:
        target.write_text(render_env(existing, env), encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"worktree: could not write {target}: {exc}", file=sys.stderr)


def live_boxes(workspace_root: Path) -> dict[str, Box]:
    """Leases whose worktree directory still exists.

    The lease file is a record, not the truth: a `git worktree remove` run by hand
    leaves the entry behind, and a stale entry holds a port slot nobody is using. The
    directory is the truth, so it is what filters.
    """
    return {
        name: box
        for name, box in read_leases(workspace_root).items()
        if box_path(workspace_root, name).is_dir()
    }


def load_registry(root: Path) -> devkit_ports.Registry | None:
    """The port registry, or None when this workspace has no `ports.toml`.

    None is a real answer, not a failure: a workspace of stackless repos needs no
    registry, and a box in one still gets a `COMPOSE_PROJECT_NAME`.
    """
    if not (root / devkit_ports.REGISTRY_NAME).is_file():
        return None
    return devkit_ports.load(root)


def known_projects(workspace: Path) -> list[str]:
    """Registered checkouts, from the same parser `sweep` and the dispatcher use."""
    try:
        return devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorktreeError(f"cannot read the workspace registry at {workspace}: {exc}") from exc


def run_steps(
    cwd: Path, steps: tuple[tuple[str, ...], ...], timeout: float = 300.0
) -> tuple[list[str], str, str]:
    """Run git argv in `cwd`, stopping at the first failure. `(ran, failed, error)`.

    Bounded because `new` is reachable from a PreToolUse hook (`worktree-guard.py`),
    where an unbounded `git fetch` against an unreachable remote does not fail — it
    hangs the agent's tool call. A timeout is reported as an ordinary step failure, so
    the fetch-is-optional path in `apply_new` handles it like any other.
    """
    ran: list[str] = []
    for step in steps:
        rendered = "git " + " ".join(step)
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *step],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ran, rendered, f"timed out after {timeout:g}s"
        except OSError as exc:
            return ran, rendered, str(exc)
        if completed.returncode != 0:
            return ran, rendered, (completed.stderr or completed.stdout or "").strip()
        ran.append(rendered)
    return ran, "", ""


def should_seed_env(stack: bool, env_tracked: bool) -> bool:
    """Whether `new` may write the box's `.env`.

    Not when the project **tracks** its `.env`. Seeding rewrites the file, so a box
    would be dirty from the moment it was cut: it could never classify as `spent`,
    `reap` would refuse it forever, and a `/ship` from inside it would commit devkit's
    managed block as if it were the task's work. A box that is born unreapable
    defeats the one guarantee this tier has over `sweep.py`.

    Almost every project gitignores `.env` — carameli and ibkr_trader both do — so
    this is the rare path, and it is a stated skip rather than a silent one.
    """
    return stack and not env_tracked


def is_tracked(repo: Path, relative: str) -> bool:
    """True when git has `relative` under version control in `repo`.

    Unknown reads as tracked: the conservative direction, since the cost of a wrong
    "untracked" is a box that can never be reaped, and the cost of a wrong "tracked"
    is a `.env` the operator has to write once.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", relative],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return completed.returncode == 0


def compose_down(path: Path, project_name: str) -> tuple[bool, str]:
    """`compose down -v` scoped to one box. `(ok, message)`; a missing docker is not fatal.

    The `-v` is the box-only exception documented on `reap_plan`. `-p` is passed so the
    scope is the box's own project name and cannot fall back to the directory name or
    to a seeded `COMPOSE_PROJECT_NAME` from the source checkout's `.env`.
    """
    try:
        completed = subprocess.run(
            ["docker", "compose", "-p", project_name, "down", "-v", "--remove-orphans"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker is not on PATH — the stack was left running"
    except subprocess.TimeoutExpired:
        return False, "compose down timed out after 300s — the stack may still be running"
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout or "").strip()
    return True, f"stack {project_name} torn down (containers, network, volumes)"


# --- modes ------------------------------------------------------------------


def plan_new(
    project: str,
    workspace: Path,
    slug: str,
    session: str = "",
    fetch: bool = True,
) -> SpawnPlan:
    """Resolve everything `new` needs from disk, then hand off to the pure planner."""
    root = workspace.parent
    projects = known_projects(workspace)
    source = devkit_project.resolve_project(project, projects, root)

    git = sweep.git_for(source)
    default_branch = tb.detect_default_branch(git, fallback="")
    if not default_branch:
        raise WorktreeError(
            f"cannot resolve origin/HEAD in {project} — there is no base branch to cut from"
        )
    existing = set(
        sweep._out(git("for-each-ref", "--format=%(refname:short)", "refs/heads/")).splitlines()
    )
    boxes = live_boxes(root)
    registry = load_registry(root) if has_stack(source) else None
    return spawn_plan(
        project=project,
        workspace_root=root,
        slug=slug,
        default_branch=default_branch,
        existing_branches=existing,
        boxes=boxes,
        registry=registry,
        session=session,
        fetch=fetch,
    )


def apply_new(plan: SpawnPlan, workspace: Path, timeout: float = 300.0) -> tuple[bool, list[str]]:
    """Create the box. `(ok, notes)`; nothing is recorded unless the worktree exists.

    `timeout` is per git step. The guard hook lowers it, because there it is an
    agent's tool call that is waiting.
    """
    root = workspace.parent
    source = root / plan.box.project
    notes: list[str] = []
    boxes_root(root).mkdir(parents=True, exist_ok=True)

    _, failed, error = run_steps(source, plan.steps, timeout=timeout)
    if failed:
        # A failed `fetch` is a stale base, not a failure: the worktree still gets cut
        # from whatever `origin/<default>` says locally, which is what an offline
        # machine has. Anything else leaves nothing behind to clean up, because the
        # lease is only written after the worktree exists.
        if failed.startswith("git fetch"):
            notes.append(
                f"fetch failed ({error.splitlines()[0] if error else 'no detail'}) — "
                f"the box is cut from a possibly stale origin/<default>"
            )
            _, failed, error = run_steps(source, plan.steps[1:], timeout=timeout)
        if failed:
            notes.append(f"FAILED at `{failed}`: {error}")
            return False, notes

    path = Path(plan.path)
    stack = has_stack(path)
    if should_seed_env(stack, is_tracked(path, ".env")):
        seed_env(source / ".env", path / ".env", plan.env)
        notes.append(f"seeded {path.name}/.env (COMPOSE_PROJECT_NAME={plan.box.name})")
    elif stack:
        notes.append(
            f"[warn] .env is tracked in {plan.box.project}, so it was left alone — this "
            f"box shares the source checkout's COMPOSE_PROJECT_NAME and ports. Export "
            f"{' '.join(f'{k}={v}' for k, v in sorted(plan.env.items()))} when running "
            f"compose here, or gitignore .env so future boxes can be seeded."
        )

    boxes = read_leases(root)
    boxes[plan.box.name] = plan.box
    write_leases(root, boxes)
    return True, notes


def inspect_box(
    box: Box, workspace_root: Path, fetch: bool = False
) -> tuple[sweep.State, str, str]:
    """`(state, verdict, reason)` for one box, through `sweep`'s classifier.

    Deliberately the same classifier the static tier uses. A second one would be a
    second opinion about "does this hold unshipped work", and the two would disagree
    exactly when it mattered.
    """
    state = sweep.inspect(box.name, box_path(workspace_root, box.name), fetch=fetch)
    verdict, reason = sweep.classify(state)
    return state, verdict, reason


def plan_reap(
    name: str,
    workspace: Path,
    force: bool = False,
    keep_stack: bool = False,
    fetch: bool = True,
) -> ReapPlan:
    root = workspace.parent
    boxes = read_leases(root)
    box = boxes.get(name)
    path = box_path(root, name)
    if box is None:
        if not path.is_dir():
            known = ", ".join(sorted(boxes)) or "(none)"
            raise WorktreeError(f"no box called {name!r}; live boxes: {known}")
        # A worktree with no lease: created by hand, or the lease file was lost. Reap
        # it anyway — refusing would leave the only cleanup path as `rm -rf`.
        box = Box(name=name, project=project_of(name), branch="", slot=-1)

    state, verdict, reason = inspect_box(box, root, fetch=fetch)
    pr_merged = False
    if fetch and box.branch and state.host == "github":
        pr_merged = sweep.has_merged_pr(sweep.gh_for(path), box.branch)
    return reap_plan(
        box=box,
        workspace_root=root,
        state=state,
        verdict=verdict,
        reason=reason,
        pr_merged=pr_merged,
        force=force,
        keep_stack=keep_stack,
        has_stack=has_stack(path),
    )


def apply_reap(plan: ReapPlan, workspace: Path) -> tuple[bool, list[str]]:
    """Destroy the box. `(ok, notes)`. The lease is released only once it is gone.

    A failed stack teardown does **not** stop the git cleanup, and does not report
    success either. Both halves of that matter: aborting would leave the box in place
    forever over a daemon that happened to be down, while carrying on quietly would
    leak a container set and a volume set per task — which is the thing that makes the
    WSL2 VHDX the next bottleneck. So the box goes, and the exit code says the stack
    needs a look.
    """
    root = workspace.parent
    notes: list[str] = []
    stack_ok = True
    if plan.stack_down:
        stack_ok, message = compose_down(Path(plan.path), plan.box)
        notes.append(f"{'' if stack_ok else '[warn] '}{message}")
        if not stack_ok:
            notes.append(
                f"the box was still removed, but its containers and volumes may survive "
                f"as project {plan.box} — check `docker compose ls` and prune by hand"
            )

    source = root / plan.project
    ran, failed, error = run_steps(source, plan.steps)
    notes.extend(ran)
    if failed:
        notes.append(f"FAILED at `{failed}`: {error}")
        return False, notes

    boxes = read_leases(root)
    boxes.pop(plan.box, None)
    write_leases(root, boxes)
    notes.append(f"lease released (slot {plan.slot})" if plan.slot >= 0 else "lease released")
    return stack_ok, notes


def survey(workspace: Path, fetch: bool = False) -> list[dict]:
    """Every live box with its verdict and whether it can be reaped."""
    root = workspace.parent
    rows: list[dict] = []
    for name, box in sorted(live_boxes(root).items()):
        state, verdict, reason = inspect_box(box, root, fetch=fetch)
        rows.append(
            {
                "box": name,
                "project": box.project,
                "branch": box.branch or state.branch,
                "slot": box.slot,
                "session": box.session,
                "verdict": verdict,
                "reason": reason,
                "reapable": verdict in SAFE_TO_REAP,
                "path": str(box_path(root, name)),
            }
        )
    return rows


# --- reporting --------------------------------------------------------------


def render_survey(rows: list[dict]) -> str:
    if not rows:
        return "No ephemeral boxes. `worktree.py new <project>` cuts one."
    table = [("BOX", "BRANCH", "SLOT", "VERDICT", "REAPABLE")]
    table += [
        (
            row["box"],
            row["branch"] or "-",
            str(row["slot"]) if row["slot"] >= 0 else "-",
            row["verdict"],
            "yes" if row["reapable"] else "no",
        )
        for row in rows
    ]
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    held = [row for row in rows if not row["reapable"]]
    if held:
        lines.append("")
        lines.append(f"{len(held)} box(es) still holding work:")
        for row in held:
            lines.append(f"  {row['box']} [{row['verdict']}] -- {row['reason']}")
    return "\n".join(lines)


def render_spawn(plan: SpawnPlan, applied: bool, notes: list[str]) -> str:
    lines = [f"{'Created' if applied else 'Would create'} {plan.box.name}"]
    lines.append(f"  path    {plan.path}")
    lines.append(f"  branch  {plan.box.branch}")
    lines.append(f"  slot    {plan.box.slot if plan.box.slot >= 0 else '- (no Docker tier)'}")
    for n, step in enumerate(plan.steps, 1):
        lines.append(f"    {n}. git -C {plan.box.project} {' '.join(step)}")
    if plan.env:
        lines.append("  env     " + ", ".join(f"{k}={v}" for k, v in sorted(plan.env.items())))
    lines.extend(f"  {note}" for note in notes)
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


def render_reap(plan: ReapPlan, applied: bool, notes: list[str]) -> str:
    if plan.refusal:
        return f"{plan.box}: refused -- {plan.refusal}"
    lines = [f"{'Reaped' if applied else 'Would reap'} {plan.box}"]
    if plan.warning:
        lines.append(f"  [warn] {plan.warning}")
    first = 1
    if plan.stack_down:
        first = 2
        lines.append(f"    1. docker compose -p {plan.box} down -v --remove-orphans")
    for n, step in enumerate(plan.steps, first):
        lines.append(f"    {n}. git -C {plan.project} {' '.join(step)}")
    lines.extend(f"  {note}" for note in notes)
    if not applied:
        lines.append("\nDry run -- nothing was changed. Re-run with --yes to apply.")
    return "\n".join(lines)


# --- entrypoint -------------------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """The flags every mode takes, added to each SUBparser rather than the top level.

    Deliberately not shared through `parents=`, and deliberately not on the top-level
    parser. argparse only accepts a top-level option *before* the subcommand, so
    `worktree.py new demo --yes` — the spelling this tool's own docstring, its `--help`
    epilog and the guard hook's block message all use — was rejected with
    "unrecognized arguments: --yes", after the dry run had already printed a plan that
    looked like it was about to run. `parents=` fixes the position but reintroduces the
    defaults through the back door: a subparser copy re-applies its own default over a
    value already parsed, so `--yes` before the subcommand would be silently undone.

    One function called per subparser is the version with neither failure mode.
    """
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print what this would do and change nothing (the default)",
    )
    apply_mode.add_argument("--yes", dest="dry_run", action="store_false", help="actually run it")
    fetch_mode = parser.add_mutually_exclusive_group()
    fetch_mode.add_argument("--fetch", dest="fetch", action="store_true", default=True)
    fetch_mode.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="skip the network (a `new` box may start from a stale base)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    new = sub.add_parser("new", help="cut a fresh box for a project")
    new.add_argument("project")
    new.add_argument("--slug", default="", help="topic for the branch name (default: the project)")
    new.add_argument("--session", default="", help="tag the lease with an agent session id")
    add_common_args(new)

    add_common_args(sub.add_parser("list", help="every live box and whether it can be reaped"))

    reap = sub.add_parser("reap", help="destroy a box once its work has shipped")
    reap.add_argument("box")
    reap.add_argument(
        "--force",
        action="store_true",
        help="discard uncommitted changes; never destroys commits (see branch_delete_flag)",
    )
    reap.add_argument("--keep-stack", action="store_true", help="leave the Docker stack running")
    add_common_args(reap)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.workspace.is_file():
        print(f"worktree: no workspace file at {args.workspace}", file=sys.stderr)
        return 2

    try:
        if args.mode == "list":
            rows = survey(args.workspace, fetch=args.fetch)
            print(json.dumps(rows, indent=2) if args.json else render_survey(rows))
            return 0

        if args.mode == "new":
            plan = plan_new(
                args.project,
                args.workspace,
                slug=args.slug or args.project,
                session=args.session,
                fetch=args.fetch,
            )
            notes: list[str] = []
            ok = True
            if not args.dry_run:
                ok, notes = apply_new(plan, args.workspace)
            if args.json:
                print(
                    json.dumps(
                        {
                            "box": asdict(plan.box),
                            "path": plan.path,
                            "env": plan.env,
                            "applied": not args.dry_run,
                            "ok": ok,
                            "notes": notes,
                        },
                        indent=2,
                    )
                )
            else:
                print(render_spawn(plan, applied=not args.dry_run, notes=notes))
            return 0 if ok else 2

        doomed = plan_reap(
            args.box,
            args.workspace,
            force=args.force,
            keep_stack=args.keep_stack,
            fetch=args.fetch,
        )
        notes = []
        ok = not doomed.refusal
        if ok and not args.dry_run:
            ok, notes = apply_reap(doomed, args.workspace)
        applied = not args.dry_run and not doomed.refusal
        if args.json:
            print(
                json.dumps(
                    {
                        "box": doomed.box,
                        "refusal": doomed.refusal,
                        "warning": doomed.warning,
                        "applied": applied,
                        "ok": ok,
                        "notes": notes,
                    },
                    indent=2,
                )
            )
        else:
            print(render_reap(doomed, applied=applied, notes=notes))
        return 0 if ok else 1
    except (WorktreeError, devkit_project.ProjectError, devkit_ports.RegistryError) as exc:
        print(f"worktree: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
