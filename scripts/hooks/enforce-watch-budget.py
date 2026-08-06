#!/usr/bin/env python3
"""PreToolUse hook: caps how often one session may schedule its own next turn.

An agent that subscribes to a pull request, or schedules a wake-up to check on a
CI run, is handed a standing instruction to keep going until the thing it is
watching finishes. That is right while the thing can still move on its own. It
stops being right the moment the only thing that can move it is a person -- and
nothing in the loop notices the difference.

The failure this exists to stop, observed rather than imagined: a PR was fixed,
the fix was correct, and the remaining blocker was a credential only the repo
owner could create. The agent re-armed an hourly check-in **nine times**, each
one a full turn that re-read the same unchanged state and re-scheduled itself,
overnight, against the user's token budget, until the user noticed and asked
what was running. No single step was wrong; the loop had no exit.

So this is a budget, not a ban. `[watch] max_wakeups` in `.devkit.toml` (default
3, see `harness_config.WatchConfig`) allows the first few -- an eight-minute CI
run genuinely is worth a scheduled check -- and refuses the rest with a message
that names the alternative: report the state and the blocker to the user and end
the turn. Polling is the expensive way to wait; a sentence to a human is not.

**Counted per session, keyed on the harness's own `session_id`.** A fresh
session gets a fresh budget, so this never leaks into tomorrow's work. State is
one small JSON marker in the worktree's private git dir (`task_branch
.worktree_file`), which is where the branch markers already live: never in the
tree, never shared between parallel worktrees.

**Fail-open, everywhere.** Unparseable payload, no git, unwritable marker -- all
allow the call. A gate that blocks work because it could not find its own
scratch file is worse than the loop it prevents.

The escape hatch is `<PREFIX>_ALLOW_WATCH_LOOP=1` (`cfg.env`), for a user who
*wants* a long unattended watch. It is the user's knob. An agent setting it to
get past its own budget is circumventing the guardrail, not configuring it --
see the polling paragraph in `.claude/rules/engineering.md`.

Decision logic is a pure function (`decide`) over the payload and the stored
state, so the budget can be tested without a timer, a git dir, or a real hook
invocation. See `scripts/hooks/tests/test_enforce_watch_budget.py`.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

# scripts/hooks/ on path for the sibling config helper, and scripts/ for the
# shared marker locator -- both stdlib-only, both imported before any venv exists.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harness_config
import task_branch as tb

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
CFG = harness_config.load(REPO_ROOT)

# Claude Code hook contract: 0 allows, 2 blocks and feeds stderr back to the model.
# Any other non-zero code is a non-blocking hook *error* and the call proceeds.
EXIT_BLOCK = 2

MARKER_NAME = "devkit-watch-budget"

# Used when the project's `harness_config` predates `[watch]`. A consuming repo
# pins a devkit *tag*, and this hook is useful the moment it lands -- so it has to
# survive being vendored a release ahead of the config section it reads, rather
# than crashing every scheduling call with an AttributeError until the pin moves.
FALLBACK_MAX_WAKEUPS = 3

# Tools that make this session run again later without a human asking it to.
# Matched on the bare name and on the MCP form (`mcp__<server>__<tool>`), because
# the same capability is served by a built-in on one surface and an MCP server on
# another -- gating only one of them gates nothing.
SCHEDULING_TOOLS = (
    "ScheduleWakeup",
    "CronCreate",
    "send_later",
    "create_trigger",
    "subscribe_pr_activity",
)


def tool_key(tool_name: str) -> str:
    """The bare tool name, with any `mcp__<server>__` prefix stripped."""
    if tool_name.startswith("mcp__"):
        return tool_name.rsplit("__", 1)[-1]
    return tool_name


def is_scheduling(tool_name: str) -> bool:
    """True when this call schedules the session's own next turn."""
    return tool_key(tool_name) in SCHEDULING_TOOLS


def get_value(obj, *paths):
    """Return the first present dotted-path value (as str) from a nested dict."""
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and cur is not None:
            return str(cur)
    return None


def block_message(count: int, budget: int, env_name: str) -> str:
    """The reason fed back to the agent. Names the alternative, not just the rule."""
    return (
        f"Blocked: this session has already scheduled {budget} unattended "
        f"wake-up(s) (budget: {budget}, this would be #{count}).\n"
        "Stop polling. Report the current state and what is blocking it to the "
        "user, then end the turn -- a person reading one sentence is cheaper and "
        "faster than another timed turn that re-reads unchanged state.\n"
        "This fires when a watch loop stops converging, which almost always means "
        "the next move belongs to a human: a credential only they can add, a "
        "review only they can give, a merge only they can click. Polling cannot "
        "produce any of those.\n"
        "If the wait is genuinely still mechanical (a build that is actually "
        "running, a deploy in flight), say so and ask the user to approve "
        f"continued watching, or have them set {env_name}=1 for this session. Do "
        "not set it yourself and do not re-issue this call -- that is "
        "circumventing the budget, not configuring it."
    )


def decide(
    raw: str, state: dict, budget: int, env_name: str, allow_override: bool = False
) -> tuple[int, str, dict]:
    """Pure decision: (exit_code, message, new_state) from payload and prior state.

    `state` is the stored marker: {"session": str, "count": int}. A payload from a
    different session resets the count, so the budget is per-session rather than a
    permanent per-worktree allowance.
    """
    unchanged = dict(state)

    if not raw.strip():
        return 0, "", unchanged

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0, "", unchanged

    tool_name = get_value(payload, "tool_name", "toolName", "tool.name", "name") or ""
    if not is_scheduling(tool_name):
        return 0, "", unchanged

    if allow_override:
        return 0, "", unchanged

    session = get_value(payload, "session_id", "sessionId") or ""
    prior = 0
    if state.get("session") == session:
        prior = state.get("count", 0)
        if not isinstance(prior, int) or prior < 0:
            prior = 0

    count = prior + 1
    new_state = {"session": session, "count": count}

    if count > budget:
        # Not persisted past the limit: the count is already at the ceiling, and
        # letting it climb would make an approved retry report a wilder number.
        return EXIT_BLOCK, block_message(count, budget, env_name), state

    return 0, "", new_state


def configured_budget(cfg) -> int:
    """The budget, tolerant of a `harness_config` vendored before `[watch]` existed."""
    return getattr(getattr(cfg, "watch", None), "max_wakeups", FALLBACK_MAX_WAKEUPS)


def _git_quiet(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=30.0, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(list(args), 1, "", "")


def marker_path() -> Path | None:
    return tb.worktree_file(_git_quiet, MARKER_NAME)


def read_state(path: Path | None) -> dict:
    """The stored counter, or an empty dict when there is none. Never raises."""
    if path is None or not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(path: Path | None, state: dict) -> None:
    """Persist the counter. Best-effort: an unwritable marker must not block."""
    if path is None:
        return
    with contextlib.suppress(OSError, TypeError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")


def main() -> int:
    env_name = CFG.env("ALLOW_WATCH_LOOP")
    path = marker_path()
    exit_code, message, new_state = decide(
        sys.stdin.read(),
        read_state(path),
        configured_budget(CFG),
        env_name,
        allow_override=os.environ.get(env_name, "") not in ("", "0", "false"),
    )
    if exit_code == 0:
        write_state(path, new_state)
    if message:
        # stderr, not stdout: only stderr is surfaced for a blocking hook.
        print(message, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
