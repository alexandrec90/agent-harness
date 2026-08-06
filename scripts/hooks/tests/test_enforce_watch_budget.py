"""Unit tests for the enforce-watch-budget PreToolUse hook decision logic.

Vendored tier: `decide` takes the budget, the env name and the prior state as
arguments precisely so these tests depend on neither the `[watch]` block of
whichever repo they run in, nor a real git dir, nor a timer.

The regression these pin is a loop, not a single call, so most of the tests here
drive `decide` repeatedly and assert on where it stops.
"""

import json

import pytest
from conftest import load_module

hook = load_module("scripts/hooks/enforce-watch-budget.py")

ENV = "DEVKIT_ALLOW_WATCH_LOOP"


def payload(tool_name, session="s1"):
    return json.dumps({"tool_name": tool_name, "session_id": session, "tool_input": {}})


def run(state, tool="ScheduleWakeup", session="s1", budget=3, override=False):
    return hook.decide(payload(tool, session), state, budget, ENV, allow_override=override)


# --- tool identification ---


def test_bare_and_mcp_forms_of_the_same_tool_both_count():
    # The same capability is a built-in on one surface and an MCP server on
    # another; gating only one of them gates nothing.
    assert hook.is_scheduling("ScheduleWakeup")
    assert hook.is_scheduling("mcp__Claude_Code_Remote__send_later")
    assert hook.is_scheduling("mcp__Claude_Code_Remote__subscribe_pr_activity")
    assert hook.is_scheduling("mcp__github__subscribe_pr_activity")


def test_unrelated_tools_are_not_scheduling():
    for name in ("Read", "Bash", "mcp__github__add_issue_comment", "mcp__github__get_me"):
        assert not hook.is_scheduling(name)


def test_tool_key_strips_only_the_mcp_prefix():
    assert hook.tool_key("mcp__a__send_later") == "send_later"
    assert hook.tool_key("ScheduleWakeup") == "ScheduleWakeup"


# --- allow paths ---


def test_empty_stdin_allows():
    assert hook.decide("", {}, 3, ENV)[0] == 0
    assert hook.decide("  \n", {}, 3, ENV)[0] == 0


def test_malformed_json_allows():
    # Fail-open: a gate that blocks work because it cannot read its own payload
    # is worse than the loop it prevents.
    assert hook.decide("{not json", {}, 3, ENV)[0] == 0


def test_non_scheduling_tool_allows_and_does_not_consume_budget():
    code, msg, state = run({}, tool="Read")
    assert (code, msg) == (0, "")
    assert state == {}


def test_first_call_is_allowed_and_counted():
    code, msg, state = run({})
    assert (code, msg) == (0, "")
    assert state == {"session": "s1", "count": 1}


def test_calls_up_to_the_budget_are_allowed():
    state = {}
    for expected in (1, 2, 3):
        code, _, state = run(state)
        assert code == 0
        assert state["count"] == expected


# --- the block ---


def test_the_call_past_the_budget_is_blocked():
    state = {}
    for _ in range(3):
        _, _, state = run(state)
    code, msg, _ = run(state)
    assert code == hook.EXIT_BLOCK
    assert msg


def test_the_nine_iteration_loop_this_exists_to_stop():
    """The observed failure, replayed: 9 re-arms against unchanging state."""
    state = {}
    allowed = 0
    for _ in range(9):
        code, _, state = run(state)
        if code == 0:
            allowed += 1
    assert allowed == 3, "budget did not cap the loop"


def test_block_message_names_the_alternative_not_just_the_rule():
    state = {"session": "s1", "count": 3}
    _, msg, _ = run(state)
    # The whole point is that the agent knows what to do instead of retrying.
    assert "Report the current state" in msg
    assert "end the turn" in msg
    assert ENV in msg
    assert "not set it yourself" in msg


def test_blocking_does_not_advance_the_stored_count():
    # Otherwise an approved retry reports a wilder number than actually happened.
    state = {"session": "s1", "count": 3}
    _, _, new_state = run(state)
    assert new_state == state


def test_budget_of_zero_blocks_the_very_first_call():
    code, _, _ = run({}, budget=0)
    assert code == hook.EXIT_BLOCK


# --- per-session accounting ---


def test_a_new_session_gets_a_fresh_budget():
    spent = {"session": "s1", "count": 99}
    code, _, state = run(spent, session="s2")
    assert code == 0
    assert state == {"session": "s2", "count": 1}


def test_the_same_session_keeps_accumulating():
    code, _, state = run({"session": "s1", "count": 2})
    assert code == 0
    assert state == {"session": "s1", "count": 3}


@pytest.mark.parametrize(
    "bad", [{"session": "s1", "count": "many"}, {"session": "s1", "count": -4}]
)
def test_a_corrupt_counter_restarts_rather_than_crashing(bad):
    code, _, state = run(bad)
    assert code == 0
    assert state == {"session": "s1", "count": 1}


# --- the override ---


def test_the_override_allows_past_the_budget():
    state = {"session": "s1", "count": 99}
    code, msg, _ = run(state, override=True)
    assert (code, msg) == (0, "")


# --- budget resolution across vendoring order ---


def test_budget_comes_from_the_watch_section_when_present():
    class Cfg:
        watch = type("W", (), {"max_wakeups": 7})()

    assert hook.configured_budget(Cfg()) == 7


def test_budget_falls_back_when_harness_config_predates_the_watch_section():
    """A consuming repo pins a devkit tag, so this hook can land a release early.

    Without the fallback it would AttributeError on every scheduling call in any
    project whose vendored harness_config has no `watch` — which is every project
    between this landing and the pin moving.
    """

    class OldCfg:
        bash = type("B", (), {"max_bytes": 4000})()

    assert hook.configured_budget(OldCfg()) == hook.FALLBACK_MAX_WAKEUPS


def test_the_fallback_matches_the_shipped_default():
    """Two literals that must agree — asserted in whichever world this repo is in.

    This test tier is vendored, so it also runs in consuming repos pinned to a
    devkit that predates `[watch]`. Branching rather than skipping: both arms
    assert a real property, and a skip here would silently stop checking the
    agreement in the repo that actually ships both halves.
    """
    import harness_config

    shipped = getattr(harness_config, "WatchConfig", None)
    if shipped is None:
        # Vendored ahead of its config section: the fallback *is* the budget here.
        assert hook.configured_budget(harness_config.Config()) == hook.FALLBACK_MAX_WAKEUPS
        return
    assert hook.FALLBACK_MAX_WAKEUPS == shipped().max_wakeups


# --- state IO (the impure half, exercised against a real path) ---


def test_read_state_tolerates_missing_and_corrupt_markers(tmp_path):
    assert hook.read_state(None) == {}
    assert hook.read_state(tmp_path / "nope.json") == {}
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert hook.read_state(corrupt) == {}


def test_read_state_rejects_a_non_object_marker(tmp_path):
    listy = tmp_path / "list.json"
    listy.write_text("[1, 2]", encoding="utf-8")
    assert hook.read_state(listy) == {}


def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "nested" / "marker.json"
    hook.write_state(path, {"session": "s1", "count": 2})
    assert hook.read_state(path) == {"session": "s1", "count": 2}


def test_write_state_without_a_path_is_a_no_op():
    hook.write_state(None, {"session": "s1", "count": 1})
