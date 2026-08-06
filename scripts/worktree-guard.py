#!/usr/bin/env python3
"""PreToolUse hook: give a cross-checkout edit its own box instead of refusing it.

The workspace-level agent needs to *see* every checkout — that is the whole reason it
is opened at the workspace root rather than inside one repo. What it must not do is
**write** into one from there, and today it silently can: `branch-on-write.py`
resolves "which repo" from the cwd, so an edit issued from the workspace root reaches
`carameli/app/main.py` with no task branch cut underneath it. The change lands on
that repo's home branch, and the next `sweep.py` reports it as `needs-branch` — the
agent manufactures the exact backlog the sweep exists to clear.

Refusing the edit would fix that and cost the turn. So this hook does the other
thing: it **spawns the box the edit should have been made in** and hands the path
back. One box per (session, project), so a session that touches three repos gets
three boxes and a session that makes forty edits in one repo gets one.

The block is still a block — a PreToolUse hook cannot rewrite a tool's arguments, so
the edit has to be re-issued at the returned path. What it is not is a dead end: by
the time the agent reads the message, the worktree exists, is on a fresh task branch
off `origin/<default>`, and has its own `COMPOSE_PROJECT_NAME` and port lease.

**Silent on everything else**, which is most calls:

  - an edit inside the checkout the session is already in (the ordinary project
    session — `branch-on-write.py` owns that case and does it better, because it can
    see whether the work is new);
  - an edit already inside a box;
  - any path that is not under a registered checkout;
  - any machine with no multi-root workspace file, which is every CI runner and every
    fresh clone.

Wired in devkit's own `.claude/settings.json` as well as the workspace root's. In a
devkit session it is one `Path.resolve()` and out — but it fires for real the moment a
devkit session edits a sibling checkout, which is the same class of mistake and the
reason devkit runs the hooks it ships.

Pure helpers (`edited_path`, `owning_project`, `redirect_decision`, `deny_message`)
are unit-tested in `tests/test_worktree_guard.py`; `main` is the thin shell that
spawns and reports.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
# Resolved by the sys.path insert above; `scripts/precommit/` is not a package. The
# shared loader is used because `worktree.py` is importable by name but this file is
# not — see that module's docstring for why the registration order matters.
from _loader import load_by_path

import devkit_project

worktree = load_by_path("worktree", Path(__file__).resolve().parent / "worktree.py")

# Claude Code hook contract, matching `enforce-capped-bash.py`: 0 allows the call, 2
# blocks it and feeds stderr back to the model. A blocking hook MUST write its reason
# to stderr — stdout is not surfaced.
EXIT_ALLOW = 0
EXIT_BLOCK = 2

# Tools that write a file. Mirrors `branch-on-write.py`'s MUTATING_TOOLS: both hooks
# are answering "is the agent about to change a file, and is it allowed to here?".
MUTATING_TOOLS = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "create_file"}
)

# Per-git-step ceiling while spawning. Lower than `worktree.apply_new`'s default
# because an agent's tool call is blocked for the duration; a `git fetch` that has not
# answered in 30s is a network problem, and the box is better cut from a stale local
# `origin/<default>` than not cut at all.
SPAWN_TIMEOUT = 30.0


def parse_hook_input(raw: str) -> dict | None:
    """Parse raw stdin into a dict, or None when absent/malformed."""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _tool_name(payload: dict) -> str:
    return str(payload.get("tool_name") or payload.get("toolName") or "")


def edited_path(payload: dict) -> str:
    """The path a mutating tool is about to write, or "".

    Tolerates snake_case and camelCase keys as the other hooks do, and reads the
    several spellings the tools use for the same argument (`file_path` for Edit and
    Write, `path` for apply_patch/create_file, `notebook_path` for NotebookEdit).
    """
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "filePath", "path", "notebook_path", "notebookPath"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _within(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or sits underneath it."""
    try:
        return child == parent or parent in child.parents
    except (OSError, ValueError):
        return False


def owning_project(target: Path, root: Path, projects: list[str]) -> str:
    """Which registered checkout contains `target`; "" when none does.

    The longest match wins, so `apt-finder-b/x.py` is attributed to `apt-finder-b`
    rather than to `apt-finder` — the two are separate checkouts of one repo and
    routing an edit to the wrong one would be worse than not routing it at all.
    """
    best = ""
    for name in projects:
        if _within(target, root / name) and len(name) > len(best):
            best = name
    return best


def redirect_decision(
    target: str, cwd: str, root: Path, projects: list[str]
) -> tuple[str, str] | None:
    """`(project, path relative to that checkout)` when this edit needs its own box.

    None — allow, silently — for every case someone else already owns:

    - a path under `.worktrees/`: the edit is already in a box, which is the whole
      point of having sent it there;
    - a session whose cwd is inside the checkout being edited: the ordinary
      project-level session, where `branch-on-write.py` cuts the branch and knows
      enough to decline on a read-only turn or an existing feature branch;
    - anything outside a registered checkout, including the workspace file itself and
      any scratch directory beside the projects.
    """
    if not target:
        return None
    try:
        base = Path(cwd) if cwd else Path.cwd()
        resolved = (
            (base / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        )
        root = root.resolve()
        here = base.resolve()
    except (OSError, ValueError, RuntimeError):
        return None

    if _within(resolved, worktree.boxes_root(root)):
        return None
    project = owning_project(resolved, root, projects)
    if not project:
        return None
    if _within(here, root / project):
        return None
    try:
        relative = resolved.relative_to(root / project)
    except ValueError:
        return None
    return project, str(relative)


def session_slug(session: str) -> str:
    """The branch topic for a box the guard cut.

    There is no prompt to derive a topic from here — the hook sees a tool call, not
    the task — so the name says what it honestly is: this session's box for this
    project. `deny_message` points at `worktree.py new --slug` for a task worth
    naming properly.
    """
    return f"ws-{session[:8]}" if session else "ws"


def deny_message(
    project: str, relative: str, box_path: str, box: str, notes: list[str], spawned: bool = True
) -> str:
    """What the agent reads. The path first, because that is the actionable part.

    `spawned` distinguishes the first edit into a project from the fortieth. Both are
    blocked and both name the same box, but "a box has been spawned" is simply untrue
    on the reuse path, and a message that misdescribes what just happened is how an
    agent concludes it is in a loop.
    """
    lines = [
        f"Blocked: this session is not inside {project}, so an edit to {relative} would "
        f"land on that checkout's home branch with no task branch under it.",
        "",
        (
            "A box has been spawned for it. Re-issue the edit against:"
            if spawned
            else "This session already has a box for this project. Re-issue the edit against:"
        ),
        f"    {Path(box_path) / relative}",
        "",
        f"The box is on a fresh claude/... branch cut from origin/<default>, with its own "
        f"COMPOSE_PROJECT_NAME ({box}) and port lease, so its stack cannot collide with "
        f"{project}'s.",
        "",
        "When the work is done, /ship from inside the box, then:",
        f"    python {Path(__file__).parent / 'worktree.py'} reap {box} --yes",
        "",
        f"Reap refuses while the box still holds unshipped work, so nothing can be "
        f"stranded in it. For a task worth naming, `worktree.py new {project} "
        f"--slug <topic> --yes` cuts a better-named one.",
    ]
    if notes:
        lines += ["", *[f"note: {note}" for note in notes]]
    return "\n".join(lines)


def failure_message(project: str, relative: str, error: str) -> str:
    """When spawning failed. Still a block, because allowing the edit is the bad outcome.

    Naming the manual command matters more than usual here: the whole promise of this
    hook is that being blocked is never a dead end, and a spawn that failed is the one
    case where the agent has to finish the job itself.
    """
    return "\n".join(
        [
            f"Blocked: an edit to {project}/{relative} from outside that checkout would land "
            f"on its home branch with no task branch under it.",
            "",
            f"Spawning a box for it failed: {error}",
            "",
            "Cut one by hand and re-issue the edit there:",
            f"    python {Path(__file__).parent / 'worktree.py'} new {project} --slug <topic> --yes",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    workspace = Path(args[args.index("--workspace") + 1]) if "--workspace" in args else None
    workspace = workspace or worktree.DEFAULT_WORKSPACE
    # No multi-root registry means no cross-checkout edit is possible: a CI runner, a
    # fresh clone, anyone else's machine. Silence is the correct answer, not an error.
    if not workspace.is_file():
        return EXIT_ALLOW

    payload = parse_hook_input(sys.stdin.read())
    if payload is None or _tool_name(payload) not in MUTATING_TOOLS:
        return EXIT_ALLOW

    try:
        projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
    except OSError:
        return EXIT_ALLOW

    decision = redirect_decision(
        edited_path(payload), str(payload.get("cwd") or ""), workspace.parent, projects
    )
    if decision is None:
        return EXIT_ALLOW
    project, relative = decision
    session = str(payload.get("session_id") or payload.get("sessionId") or "")

    root = workspace.parent
    existing = worktree.find_session_box(worktree.live_boxes(root), project, session)
    if existing is not None:
        print(
            deny_message(
                project,
                relative,
                str(worktree.box_path(root, existing.name)),
                existing.name,
                [],
                spawned=False,
            ),
            file=sys.stderr,
        )
        return EXIT_BLOCK

    try:
        plan = worktree.plan_new(
            project, workspace, slug=session_slug(session), session=session, fetch=True
        )
        ok, notes = worktree.apply_new(plan, workspace, timeout=SPAWN_TIMEOUT)
    except Exception as exc:
        print(failure_message(project, relative, f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return EXIT_BLOCK

    if not ok:
        print(failure_message(project, relative, "; ".join(notes) or "no detail"), file=sys.stderr)
        return EXIT_BLOCK

    print(deny_message(project, relative, plan.path, plan.box.name, notes), file=sys.stderr)
    return EXIT_BLOCK


if __name__ == "__main__":
    sys.exit(main())
