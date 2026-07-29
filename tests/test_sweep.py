"""Tests for the cross-project ship sweep.

The two contract tests at the bottom (`test_classify_is_total`,
`test_every_actionable_verdict_has_a_plan`) are the ones that matter: together
they are the machine-checkable form of "nothing gets stranded". Everything above
them pins the individual decisions.
"""

import json

import pytest
from support import sweep

State = sweep.State
classify = sweep.classify
parse_workspace = sweep.parse_workspace
plan_for = sweep.plan_for


def on_default(**overrides) -> State:
    """A checkout sitting on its default branch."""
    base = {"name": "proj", "default_branch": "main", "branch": "main", "host": "github"}
    return State(**{**base, **overrides})


def on_feature(**overrides) -> State:
    """A checkout on a feature branch, one commit ahead, pushed."""
    base = {
        "name": "proj",
        "default_branch": "main",
        "branch": "claude/thing-0727",
        "host": "github",
        "ahead": 1,
        "upstream": "origin/claude/thing-0727",
        "unpushed": 0,
    }
    return State(**{**base, **overrides})


# --- workspace parsing ------------------------------------------------------

WORKSPACE = json.dumps(
    {
        "folders": [
            {"path": "carameli"},
            {"path": "carameli-b"},
            {"path": "ibkr_trader"},
            {"path": "devkit"},
            {"path": "VanillaLand"},
        ]
    }
)


def test_workspace_folders_are_read_in_file_order():
    assert parse_workspace(WORKSPACE, frozenset()) == [
        "carameli",
        "carameli-b",
        "ibkr_trader",
        "devkit",
        "VanillaLand",
    ]


def test_excluded_checkouts_are_dropped():
    # The default exclusion: VanillaLand is Azure DevOps with a `develop` base and
    # is a reference checkout, not one we ship from.
    assert "VanillaLand" not in parse_workspace(WORKSPACE)
    assert len(parse_workspace(WORKSPACE)) == 4


def test_nested_paths_reduce_to_the_checkout_name():
    text = json.dumps({"folders": [{"path": "nested/dir/proj"}, {"path": "win\\style\\other"}]})
    assert parse_workspace(text, frozenset()) == ["proj", "other"]


def test_malformed_workspace_reports_nothing_rather_than_raising():
    # A root-level task should print "no checkouts" and move on, not crash.
    assert parse_workspace("{not json") == []
    assert parse_workspace("[]") == []


def test_duplicate_folder_entries_are_swept_once():
    text = json.dumps({"folders": [{"path": "proj"}, {"path": "proj"}]})
    assert parse_workspace(text, frozenset()) == ["proj"]


# --- host detection ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/alexandrec90/devkit.git", "github"),
        ("git@github.com:alexandrec90/devkit.git", "github"),
        ("https://x@dev.azure.com/Coll/Proj/_git/Proj", "azure"),
        ("https://gitlab.com/x/y.git", "other"),
        ("", "none"),
    ],
)
def test_remote_host_picks_the_pr_api(url, expected):
    assert sweep.remote_host(url) == expected


def test_ahead_behind_survives_unparseable_output():
    assert sweep.parse_ahead_behind("4\t2") == (4, 2)
    assert sweep.parse_ahead_behind("") == (0, 0)
    assert sweep.parse_ahead_behind("fatal: bad revision") == (0, 0)


# --- classification: the default branch is where work strands ---------------


def test_dirty_on_the_default_branch_needs_a_branch_not_a_ship():
    # ship.py's is_shippable() refuses on the default branch, so a sweep that
    # called /ship here would just collect exit 3 and leave the work sitting.
    verdict, reason = classify(on_default(dirty=58))
    assert verdict == sweep.NEEDS_BRANCH
    assert "58 uncommitted" in reason


def test_commits_made_straight_to_the_default_branch_also_need_a_branch():
    verdict, reason = classify(on_default(ahead=2))
    assert verdict == sweep.NEEDS_BRANCH
    assert "straight to main" in reason


def test_dirty_and_ahead_on_the_default_branch_reports_both():
    _, reason = classify(on_default(dirty=3, ahead=2))
    assert "3 uncommitted" in reason
    assert "2 unpushed" in reason


def test_clean_but_behind_on_the_default_branch_only_needs_a_pull():
    assert classify(on_default(behind=31))[0] == sweep.NEEDS_PULL


def test_clean_and_current_on_the_default_branch_is_clean():
    assert classify(on_default())[0] == sweep.CLEAN


# --- classification: feature branches ---------------------------------------


def test_dirty_feature_branch_is_ready_to_ship():
    assert classify(on_feature(dirty=4))[0] == sweep.READY


def test_never_pushed_feature_branch_is_ready_to_ship():
    assert classify(on_feature(upstream="", unpushed=-1))[0] == sweep.READY


def test_locally_ahead_of_upstream_is_ready_to_ship():
    assert classify(on_feature(ahead=3, unpushed=2))[0] == sweep.READY


def test_fully_pushed_feature_branch_needs_its_pr_confirmed():
    assert classify(on_feature())[0] == sweep.NEEDS_PR


def test_feature_branch_with_nothing_on_it_is_clean():
    # A spent branch (already merged) or one just cut -- no work to strand.
    assert classify(on_feature(ahead=0, unpushed=0))[0] == sweep.CLEAN


# --- classification: blocked and skipped ------------------------------------


def test_non_git_directory_is_skipped():
    assert classify(State(name="sports_betting", is_git=False))[0] == sweep.SKIPPED


def test_detached_head_is_blocked():
    assert classify(on_default(branch=""))[0] == sweep.BLOCKED


def test_unresolvable_default_branch_is_blocked():
    assert classify(on_default(default_branch=""))[0] == sweep.BLOCKED


def test_missing_origin_is_blocked():
    assert classify(on_feature(host="none"))[0] == sweep.BLOCKED


# --- plans ------------------------------------------------------------------


def test_stranded_work_is_told_to_cut_a_branch_before_shipping():
    state = on_default(dirty=7)
    plan = plan_for(state, sweep.NEEDS_BRANCH)
    assert "claude/" in plan[0]
    assert plan[-1].startswith("/ship")


def test_a_stale_branch_rebases_before_it_ships():
    plan = plan_for(on_feature(dirty=1, behind=4), sweep.READY)
    assert any("rebase origin/main" in step for step in plan)
    assert not any("merge origin/main" in step for step in plan)


def test_a_stale_branch_with_a_pr_merges_instead_of_rebasing():
    # Rebasing a branch with an open PR detaches its review threads.
    plan = plan_for(on_feature(behind=4), sweep.NEEDS_PR)
    assert any("merge origin/main" in step for step in plan)


def test_an_up_to_date_branch_gets_no_sync_step():
    assert not any("origin/main" in step for step in plan_for(on_feature(dirty=1), sweep.READY))


def test_conflicts_are_never_auto_resolved():
    plan = plan_for(on_feature(dirty=1, behind=2), sweep.READY)
    assert any("never auto-resolve" in step for step in plan)


# --- the "nothing stranded" contract ----------------------------------------

ALL_VERDICTS = {
    sweep.BLOCKED,
    sweep.NEEDS_BRANCH,
    sweep.READY,
    sweep.NEEDS_PR,
    sweep.NEEDS_PULL,
    sweep.CLEAN,
    sweep.SKIPPED,
}

# Every combination of the axes classify() actually branches on.
STATES = [
    State(
        name="proj",
        is_git=is_git,
        host=host,
        default_branch=default_branch,
        branch=branch,
        dirty=dirty,
        behind=behind,
        ahead=ahead,
        upstream=upstream,
        unpushed=unpushed,
    )
    for is_git in (True, False)
    for host in ("github", "azure", "none")
    for default_branch in ("main", "")
    for branch in ("main", "claude/x", "")
    for dirty in (0, 5)
    for behind in (0, 3)
    for ahead in (0, 2)
    for upstream, unpushed in (("", -1), ("origin/claude/x", 0), ("origin/claude/x", 2))
]


def test_classify_is_total():
    """No state falls through to an unknown verdict -- nothing can go unclassified."""
    for state in STATES:
        verdict, reason = classify(state)
        assert verdict in ALL_VERDICTS, state
        assert reason, state


def test_every_actionable_verdict_has_a_plan():
    """Anything the sweep flags comes with a concrete next action."""
    for state in STATES:
        verdict, _ = classify(state)
        plan = plan_for(state, verdict)
        if verdict in sweep.TERMINAL:
            assert plan == [], state
        else:
            assert plan, state


def test_actionable_and_terminal_verdicts_partition_the_space():
    assert sweep.ACTIONABLE | sweep.TERMINAL == ALL_VERDICTS
    assert not (sweep.ACTIONABLE & sweep.TERMINAL)


# --- exit codes -------------------------------------------------------------


def result(verdict: str) -> sweep.Result:
    return sweep.Result(State(name="p"), verdict, "reason", [])


def test_exit_code_reports_clean_then_actionable_then_blocked():
    assert sweep.exit_code([result(sweep.CLEAN), result(sweep.SKIPPED)]) == 0
    assert sweep.exit_code([result(sweep.CLEAN), result(sweep.READY)]) == 1
    # Blocked outranks actionable: a human is needed either way.
    assert sweep.exit_code([result(sweep.READY), result(sweep.BLOCKED)]) == 2


def test_shared_remotes_are_called_out():
    url = "https://github.com/alexandrec90/carameli.git"
    results = [
        sweep.Result(State(name="carameli", remote_url=url), sweep.CLEAN, "", []),
        sweep.Result(State(name="carameli-b", remote_url=url), sweep.CLEAN, "", []),
        sweep.Result(State(name="devkit", remote_url="https://x/devkit.git"), sweep.CLEAN, "", []),
    ]
    assert sweep.dedupe_note(results) == {url: ["carameli", "carameli-b"]}
