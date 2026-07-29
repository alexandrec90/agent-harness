"""Tests for the cross-project ship sweep.

The two contract tests at the bottom (`test_classify_is_total`,
`test_every_actionable_verdict_has_a_plan`) are the ones that matter: together
they are the machine-checkable form of "nothing gets stranded". Everything above
them pins the individual decisions.
"""

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
from support import sweep

# Pinned so branch names are assertable: tb.branch_name() stamps -<mmdd>.
DATE = dt.date(2026, 7, 29)

State = sweep.State
classify = sweep.classify
parse_workspace = sweep.parse_workspace
plan_for = sweep.plan_for


def on_default(**overrides) -> State:
    """A checkout sitting on its default branch."""
    base = {"name": "proj", "default_branch": "main", "branch": "main", "host": "github"}
    return State(**{**base, **overrides})


def on_feature(**overrides) -> State:
    """A checkout on a task branch, one commit ahead, pushed."""
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


def on_anchor(**overrides) -> State:
    """A linked worktree on its long-lived anchor branch (the `carameli-b` case)."""
    base = {
        "name": "proj-b",
        "default_branch": "main",
        "branch": "proj-b",
        "host": "github",
        "linked": True,
        "local_branches": ("main", "proj-b"),
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


def test_task_branch_with_nothing_on_it_is_spent_not_clean():
    # Merged, or cut and never used. Either way the worktree is still parked on it,
    # so there is something for --sync to do -- reporting `clean` here is what made
    # "nothing stranded" print while a checkout sat on a dead branch.
    verdict, reason = classify(on_feature(ahead=0, unpushed=0))
    assert verdict == sweep.SPENT
    assert "spent" in reason


# --- classification: long-lived worktree anchors ----------------------------
# `carameli-b` and `ibkr-b` are permanent worktree branches, not task branches.
# Agents without the branch-per-task hook leave work sitting on them.


def test_dirty_on_a_worktree_anchor_needs_a_branch_not_a_ship():
    # ship.py's is_shippable() only refuses the *default* branch, so /ship would
    # happily open a PR from `proj-b` and turn a permanent branch into a PR branch.
    verdict, reason = classify(on_anchor(dirty=4))
    assert verdict == sweep.NEEDS_BRANCH
    assert "4 uncommitted" in reason
    assert "proj-b" in reason
    assert "not a claude/ task branch" in reason


def test_commits_straight_to_an_anchor_also_need_a_branch():
    assert classify(on_anchor(ahead=2))[0] == sweep.NEEDS_BRANCH


def test_a_clean_anchor_behind_the_base_just_needs_a_fast_forward():
    assert classify(on_anchor(behind=5))[0] == sweep.NEEDS_PULL


def test_a_clean_current_anchor_is_clean():
    assert classify(on_anchor())[0] == sweep.CLEAN


def test_a_checkout_already_on_a_task_branch_is_left_alone():
    # The user's "don't cut a second branch for carameli" case: it is already on a
    # claude/... branch, so it is ready to ship, not stranded.
    assert classify(on_feature(dirty=9))[0] == sweep.READY


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


# --- home_ref: where a worktree parks between tasks -------------------------


def test_a_recorded_anchor_wins():
    assert sweep.home_ref(on_feature(anchor="proj-b", linked=True)) == "proj-b"


def test_a_checkout_already_on_a_home_branch_is_already_there():
    assert sweep.home_ref(on_anchor()) == "proj-b"
    assert sweep.home_ref(on_default()) == "main"


def test_a_primary_worktree_falls_back_to_the_default_branch():
    assert sweep.home_ref(on_feature()) == "main"


def test_a_linked_worktree_falls_back_to_a_branch_named_after_its_directory():
    # It cannot use the default branch -- the primary worktree has it checked out.
    state = on_feature(name="proj-b", linked=True, local_branches=("main", "proj-b"))
    assert sweep.home_ref(state) == "proj-b"


def test_a_linked_worktree_with_no_resolvable_home_refuses_to_guess():
    # `ibkr_trader-b` the directory vs `ibkr-b` the branch: the names do not match,
    # so there is nothing to infer and sync must say so rather than pick.
    state = on_feature(name="ibkr_trader-b", linked=True, local_branches=("main", "ibkr-b"))
    assert sweep.home_ref(state) == ""
    assert sweep.sync_plan(state, sweep.SPENT).refusal


# --- step 1: --branch --------------------------------------------------------


def test_branching_cuts_a_task_branch_off_head_carrying_the_dirty_tree():
    plan = sweep.branch_plan(on_default(dirty=29), slug="sweep", today=DATE)
    assert plan.steps[0] == ("checkout", "-b", "claude/sweep-0729")
    # No base ref: branching off HEAD is what carries uncommitted work across.
    assert not any("origin/main" in step[-1] for step in plan.steps if step[0] == "checkout")


def test_branching_records_where_the_work_came_from():
    assert sweep.branch_plan(on_anchor(dirty=4), today=DATE).anchor == "proj-b"


def test_branching_resets_a_home_branch_that_carried_commits():
    # Safe only here: the commits are already on the new branch. Leaving it would
    # diverge the home branch forever once the PR merges as a squash.
    plan = sweep.branch_plan(on_default(ahead=2), today=DATE)
    assert plan.steps[1] == ("branch", "-f", "main", "origin/main")


def test_branching_leaves_an_unmoved_home_branch_alone():
    plan = sweep.branch_plan(on_default(dirty=3), today=DATE)
    assert not any(step[0] == "branch" for step in plan.steps)


def test_branching_refuses_a_checkout_already_on_a_task_branch():
    assert sweep.branch_plan(on_feature(dirty=9), today=DATE).refusal


def test_the_slug_names_the_branch():
    # There is no prompt here to derive a topic from, so the caller supplies one;
    # `sweep` is the honest default rather than a fabricated description.
    plan = sweep.branch_plan(on_default(dirty=29), slug="ingestion connector settings", today=DATE)
    assert plan.steps[0][-1] == "claude/ingestion-connector-settings-0729"


def test_branch_names_do_not_collide_with_existing_ones():
    state = on_default(dirty=1, local_branches=("main", "claude/sweep-0729"))
    assert sweep.branch_plan(state, today=DATE).steps[0][-1] == "claude/sweep-0729-2"


# --- step 2: --sync ----------------------------------------------------------


def test_sync_returns_a_spent_worktree_home_and_deletes_the_branch():
    state = on_feature(ahead=0, unpushed=0, local_branches=("main", "claude/thing-0727"))
    steps = sweep.sync_plan(state, sweep.SPENT).steps
    assert ("checkout", "main") in steps
    assert ("merge", "--ff-only", "origin/main") in steps
    assert ("branch", "-d", "claude/thing-0727") in steps


def test_sync_deletes_the_branch_only_after_moving_off_it():
    # Order is the safety property: `branch -d` on a checked-out branch fails.
    steps = sweep.sync_plan(on_feature(ahead=0, unpushed=0), sweep.SPENT).steps
    assert steps.index(("checkout", "main")) < steps.index(("branch", "-d", "claude/thing-0727"))


def test_sync_never_force_deletes():
    state = on_feature(ahead=0, unpushed=0, merged_task_branches=("claude/old-0701",))
    assert all(step[:2] != ("branch", "-D") for step in sweep.sync_plan(state, sweep.SPENT).steps)


def test_sync_never_rewrites_history():
    # --ff-only is the whole safety story: a diverged branch errors, never merges.
    steps = sweep.sync_plan(on_default(behind=3), sweep.NEEDS_PULL).steps
    merges = [step for step in steps if step[0] == "merge"]
    assert merges and all("--ff-only" in step for step in merges)
    assert not any(step[0] in {"rebase", "reset", "push"} for step in steps)


def test_sync_reaps_merged_task_branches_it_is_not_standing_on():
    state = on_default(merged_task_branches=("claude/a-0701", "claude/b-0702"))
    steps = sweep.sync_plan(state, sweep.CLEAN).steps
    assert ("branch", "-d", "claude/a-0701") in steps
    assert ("branch", "-d", "claude/b-0702") in steps


def test_worktree_branches_are_parsed_from_the_porcelain_listing():
    text = (
        "worktree C:/x/carameli\nHEAD abc\nbranch refs/heads/claude/thing-0727\n\n"
        "worktree C:/x/carameli-b\nHEAD abc\nbranch refs/heads/carameli-b\n\n"
        "worktree C:/x/detached\nHEAD abc\ndetached\n"
    )
    assert sweep.parse_worktree_branches(text) == ("claude/thing-0727", "carameli-b")


def test_sync_never_deletes_a_branch_a_sibling_worktree_is_on():
    """Reaping is repo-wide, a checkout is per-worktree. `carameli-b` sees the
    branch `carameli` is mid-task on -- merged into the base, so it reads as
    abandoned -- and git would refuse the delete it proposes."""
    state = on_anchor(
        merged_task_branches=("claude/live-0729", "claude/dead-0701"),
        worktree_branches=("claude/live-0729", "proj-b"),
    )
    steps = sweep.sync_plan(state, sweep.CLEAN).steps
    assert ("branch", "-d", "claude/dead-0701") in steps
    assert ("branch", "-d", "claude/live-0729") not in steps


def test_sync_still_deletes_the_spent_branch_this_worktree_is_standing_on():
    # Our own branch is in worktree_branches too, but we check out `home` first.
    state = on_feature(
        ahead=0,
        unpushed=0,
        worktree_branches=("claude/thing-0727", "main"),
    )
    assert ("branch", "-d", "claude/thing-0727") in sweep.sync_plan(state, sweep.SPENT).steps


def test_sync_never_deletes_the_home_branch():
    state = on_anchor(merged_task_branches=("proj-b", "claude/a-0701"))
    steps = sweep.sync_plan(state, sweep.CLEAN).steps
    assert not any(step == ("branch", "-d", "proj-b") for step in steps)


def test_sync_refuses_anything_with_unshipped_work():
    # The ordering the two steps depend on: syncing a PR still in review would move
    # the worktree off the branch under review.
    for verdict in (sweep.READY, sweep.NEEDS_PR, sweep.NEEDS_BRANCH):
        plan = sweep.sync_plan(on_feature(dirty=1), verdict)
        assert plan.refusal, verdict
        assert not plan.steps, verdict


def test_sync_refuses_a_blocked_checkout():
    assert sweep.sync_plan(on_default(branch=""), sweep.BLOCKED).refusal


def test_sync_records_the_home_branch_for_linked_worktrees_only():
    linked = on_feature(ahead=0, name="proj-b", linked=True, local_branches=("main", "proj-b"))
    assert sweep.sync_plan(linked, sweep.SPENT).anchor == "proj-b"
    # A primary can always resolve the default branch, so a stored value would only
    # be one more thing that can go stale.
    assert sweep.sync_plan(on_feature(ahead=0), sweep.SPENT).anchor == ""


def test_sync_skips_the_fetch_when_asked():
    steps = sweep.sync_plan(on_default(), sweep.CLEAN, fetch=False).steps
    assert not any(step[0] == "fetch" for step in steps)


# --- the "nothing stranded" contract ----------------------------------------

ALL_VERDICTS = {
    sweep.BLOCKED,
    sweep.NEEDS_BRANCH,
    sweep.READY,
    sweep.NEEDS_PR,
    sweep.NEEDS_PULL,
    sweep.SPENT,
    sweep.CLEAN,
    sweep.SKIPPED,
}

# Every combination of the axes classify() actually branches on. `proj-b` is the
# third branch case -- neither the default branch nor a task branch -- and `linked`
# is the axis home_ref() turns on.
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
        linked=linked,
    )
    for is_git in (True, False)
    for host in ("github", "azure", "none")
    for default_branch in ("main", "")
    for branch in ("main", "claude/x", "proj-b", "")
    for dirty in (0, 5)
    for behind in (0, 3)
    for ahead in (0, 2)
    for linked in (True, False)
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


# --- the two-step contract ---------------------------------------------------
# Step 1 ships work out; step 2 tidies up after it merges. Together they have to
# cover every actionable verdict, or "ship them all, then sync them all" leaves
# something behind -- which is the whole point of the sweep.


def test_the_two_steps_cover_every_actionable_verdict():
    handled = sweep.BRANCHABLE | sweep.SYNCABLE | {sweep.READY, sweep.NEEDS_PR, sweep.BLOCKED}
    assert handled >= sweep.ACTIONABLE
    # READY/NEEDS_PR belong to /ship, and BLOCKED to a human -- neither is a mode.
    assert not (sweep.BRANCHABLE & sweep.SYNCABLE)


def test_every_mutating_plan_either_acts_or_says_why_not():
    """No silent no-ops: a plan with no steps and no refusal reads as 'done' and
    would let a checkout drop out of both steps unnoticed."""
    for state in STATES:
        verdict, _ = classify(state)
        if verdict == sweep.SKIPPED:
            continue
        plan = sweep.sync_plan(state, verdict)
        assert plan.steps or plan.refusal, (state, verdict)
        if verdict in sweep.BRANCHABLE:
            cut = sweep.branch_plan(state, today=DATE)
            assert cut.steps or cut.refusal, state


def test_no_mutating_plan_ever_emits_a_destructive_git_command():
    """The safety envelope, asserted over the whole state space rather than by
    reading the source: every step is one git refuses to run destructively."""
    banned = {"reset", "rebase", "push", "clean", "restore"}
    for state in STATES:
        verdict, _ = classify(state)
        plans = [sweep.sync_plan(state, verdict)]
        if verdict in sweep.BRANCHABLE:
            plans.append(sweep.branch_plan(state, today=DATE))
        for plan in plans:
            for step in plan.steps:
                assert step[0] not in banned, (state, step)
                assert step[:2] != ("branch", "-D"), (state, step)
                if step[0] == "merge":
                    assert "--ff-only" in step, (state, step)
                # `branch -f` only ever retargets the branch the work just left,
                # and only onto the remote's own tip.
                if step[:2] == ("branch", "-f"):
                    assert step[2] == state.branch, (state, step)
                    assert step[3] == f"origin/{state.default_branch}", (state, step)


# --- running a plan ----------------------------------------------------------


class FakeGit:
    """A `git(*args)` stand-in that fails on the first step matching `fail_on`."""

    def __init__(self, fail_on: str = ""):
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        failed = bool(self.fail_on) and self.fail_on in " ".join(args)
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=1 if failed else 0,
            stdout="",
            stderr="fatal: nope" if failed else "",
        )


PLAN = sweep.Plan(
    steps=(("checkout", "main"), ("merge", "--ff-only", "origin/main"), ("branch", "-d", "gone")),
    anchor="",
)


def test_a_plan_runs_its_steps_in_order(tmp_path):
    git = FakeGit()
    result = sweep.apply_plan("proj", tmp_path, PLAN, git=git)
    assert result.ok
    assert git.calls == list(PLAN.steps)


def test_a_failing_step_stops_the_rest():
    """The steps are ordered so a later one is only safe once the earlier ones
    landed -- deleting a branch after a checkout that never happened is the exact
    way a safe plan turns destructive."""
    git = FakeGit(fail_on="merge")
    result = sweep.apply_plan("proj", Path("."), PLAN, git=git)
    assert not result.ok
    assert result.failed == "git merge --ff-only origin/main"
    assert "nope" in result.error
    assert ("branch", "-d", "gone") not in git.calls


def test_a_refused_plan_runs_nothing():
    git = FakeGit()
    result = sweep.apply_plan("proj", Path("."), sweep.Plan(refusal="has unshipped work"), git=git)
    assert git.calls == []
    assert not result.ok


def test_the_dry_run_prints_the_same_steps_the_real_run_executes():
    """A dry run is only worth having if it is the truth: both renders come from
    the same Plan objects the runner consumes."""
    result = sweep.Result(on_feature(ahead=0), sweep.SPENT, "spent", [])
    plan = sweep.sync_plan(result.state, sweep.SPENT)
    dry = sweep.render_plans("sync", [(result, plan)], applied=False)
    wet = sweep.render_plans("sync", [(result, plan)], applied=True)
    for step in plan.steps:
        assert " ".join(step) in dry
        assert " ".join(step) in wet
    assert "Dry run" in dry and "Dry run" not in wet


def test_refusals_are_reported_not_hidden():
    result = sweep.Result(on_feature(dirty=2), sweep.READY, "2 uncommitted", [])
    plan = sweep.sync_plan(result.state, sweep.READY)
    assert "skipped" in sweep.render_plans("sync", [(result, plan)], applied=False)
    assert plan.refusal in sweep.render_plans("sync", [(result, plan)], applied=False)


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
