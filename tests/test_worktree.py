"""Tests for the ephemeral-worktree tier.

The destructive half is what these are for. `reap` deletes a worktree, its branch and
its Docker volumes, so every one of those steps is asserted as *argv* against a
hand-built `sweep.State` — no repo, no daemon, no network. That is the same contract
`tests/test_sweep.py` keeps for the same reason: a safety property nobody can check
cheaply is a safety property nobody checks.

`test_reap_refuses_while_work_is_only_in_the_box` is the reversion check for the whole
design. If it passed with `SAFE_TO_REAP` widened to include `ready`, the tier would be
back to stranding work — just faster than before.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest
from support import devkit_ports, sweep, worktree

TODAY = _dt.date(2026, 8, 6)


def registry(max_slots: int = 8, **slots: int) -> devkit_ports.Registry:
    return devkit_ports.from_dict(
        {
            "registry": {"max_slots": max_slots},
            "services": {"app": 8000, "db": 5432},
            "slots": dict(slots),
        }
    )


def box(name: str = "carameli--voicemail-0806", **kwargs) -> worktree.Box:
    defaults = {
        "project": "carameli",
        "branch": "claude/voicemail-0806",
        "slot": 3,
    }
    return worktree.Box(name=name, **{**defaults, **kwargs})


def state(**kwargs) -> sweep.State:
    defaults = {
        "name": "carameli--voicemail-0806",
        "is_git": True,
        "host": "github",
        "default_branch": "master",
        "branch": "claude/voicemail-0806",
        "linked": True,
    }
    return sweep.State(**{**defaults, **kwargs})


# --- naming -----------------------------------------------------------------


def test_box_name_joins_project_and_branch_topic():
    assert worktree.box_name("carameli", "claude/voicemail-0806") == "carameli--voicemail-0806"


def test_box_name_keeps_a_non_task_branch_whole():
    """No `claude/` prefix to strip means the branch name is the topic, not a slice of it."""
    assert worktree.box_name("carameli", "hotfix") == "carameli--hotfix"


def test_box_name_is_a_legal_compose_project_name():
    """It becomes COMPOSE_PROJECT_NAME, which compose restricts to [a-z0-9][a-z0-9_-]*."""
    for project, branch in (
        ("apt-finder", "claude/rent-caps-0806"),
        ("ibkr_trader", "claude/oos-0806-2"),
    ):
        name = worktree.box_name(project, branch)
        assert name[0].isalnum()
        assert all(c.isalnum() or c in "-_" for c in name)
        assert name.lower() == name


def test_project_of_recovers_the_source_checkout():
    assert worktree.project_of("apt-finder--rent-caps-0806") == "apt-finder"
    assert worktree.project_of("not-a-box") == ""


# --- leases -----------------------------------------------------------------


def test_leases_round_trip():
    boxes = {"carameli--x-0806": box("carameli--x-0806")}
    assert worktree.parse_leases(worktree.render_leases(boxes)) == boxes


@pytest.mark.parametrize("text", ["", "{", "null", '{"boxes": []}', '{"boxes": {"a": 3}}'])
def test_unreadable_leases_are_no_boxes_not_a_crash(text):
    """A truncated lease file must cost a slot, never the whole tier."""
    assert worktree.parse_leases(text) == {}


def test_next_lease_slot_skips_both_pinned_checkouts_and_live_boxes():
    """The bug this exists to prevent: handing a box a slot a pinned checkout already holds."""
    reg = registry(carameli=0, ibkr_trader=1)
    boxes = {"carameli--a-0806": box("carameli--a-0806", slot=2)}
    assert worktree.next_lease_slot(reg, boxes) == 3


def test_next_lease_slot_reuses_a_gap_left_by_a_reaped_box():
    reg = registry(carameli=0, ibkr_trader=2)
    assert worktree.next_lease_slot(reg, {}) == 1


def test_next_lease_slot_ignores_boxes_with_no_stack():
    """slot=-1 means "no Docker tier", not "slot minus one"."""
    reg = registry(carameli=0)
    boxes = {"devkit--x-0806": box("devkit--x-0806", project="devkit", slot=-1)}
    assert worktree.next_lease_slot(reg, boxes) == 1


def test_next_lease_slot_raises_when_the_registry_is_full():
    reg = registry(max_slots=2, carameli=0, ibkr_trader=1)
    with pytest.raises(devkit_ports.RegistryError, match="all 2 port slots"):
        worktree.next_lease_slot(reg, {})


def test_find_session_box_is_scoped_to_one_project():
    boxes = {
        "carameli--ws-abc-0806": box("carameli--ws-abc-0806", session="abc"),
        "ibkr_trader--ws-abc-0806": box(
            "ibkr_trader--ws-abc-0806", project="ibkr_trader", session="abc"
        ),
    }
    found = worktree.find_session_box(boxes, "ibkr_trader", "abc")
    assert found is not None and found.name == "ibkr_trader--ws-abc-0806"


def test_find_session_box_without_a_session_id_finds_nothing():
    """An empty session must not match every unstamped lease and reuse a stranger's box."""
    boxes = {"carameli--x-0806": box("carameli--x-0806", session="")}
    assert worktree.find_session_box(boxes, "carameli", "") is None


# --- env seeding ------------------------------------------------------------


def test_managed_env_always_carries_the_compose_project_name():
    env = worktree.managed_env("carameli--x-0806", registry(), 3)
    assert env["COMPOSE_PROJECT_NAME"] == "carameli--x-0806"
    assert env["DB_HOST_PORT"] == "5435"
    assert env["APP_HOST_PORT"] == "8003"


def test_managed_env_without_a_registry_still_namespaces_the_stack():
    assert worktree.managed_env("devkit--x-0806", None, -1) == {
        "COMPOSE_PROJECT_NAME": "devkit--x-0806"
    }


def test_render_env_appends_so_the_managed_keys_win():
    """Compose's dotenv parser takes the last assignment, which is why nothing above is edited."""
    rendered = worktree.render_env("DB_HOST_PORT=5432\nSECRET=keepme\n", {"DB_HOST_PORT": "5435"})
    assert rendered.index("SECRET=keepme") < rendered.index("DB_HOST_PORT=5435")
    assert "DB_HOST_PORT=5432" in rendered


def test_render_env_is_idempotent():
    """Re-spawning must not stack a second managed block on the first."""
    once = worktree.render_env("SECRET=keepme\n", {"DB_HOST_PORT": "5435"})
    twice = worktree.render_env(once, {"DB_HOST_PORT": "5435"})
    assert once == twice
    assert twice.count(worktree.MANAGED_BEGIN) == 1


def test_render_env_replaces_a_stale_block_rather_than_leaving_both_ports():
    once = worktree.render_env("SECRET=keepme\n", {"DB_HOST_PORT": "5435"})
    twice = worktree.render_env(once, {"DB_HOST_PORT": "5437"})
    assert "DB_HOST_PORT=5437" in twice
    assert "DB_HOST_PORT=5435" not in twice


def test_a_tracked_env_is_never_seeded():
    """Regression: seeding rewrites the file, so a box whose repo *tracks* `.env` was
    dirty from birth -- never `spent`, never reapable, and a `/ship` from inside it
    would have committed devkit's managed block as the task's work."""
    assert worktree.should_seed_env(stack=True, env_tracked=True) is False


def test_an_untracked_env_is_seeded_when_there_is_a_stack():
    assert worktree.should_seed_env(stack=True, env_tracked=False) is True


def test_a_project_with_no_stack_gets_no_env_at_all():
    assert worktree.should_seed_env(stack=False, env_tracked=False) is False


def test_render_env_handles_an_empty_source():
    """A project whose `.env` is gitignored gives a fresh worktree nothing to seed from."""
    rendered = worktree.render_env("", {"COMPOSE_PROJECT_NAME": "x"})
    assert rendered.startswith("\n" + worktree.MANAGED_BEGIN) or rendered.lstrip().startswith(
        worktree.MANAGED_BEGIN
    )
    assert "COMPOSE_PROJECT_NAME=x" in rendered


# --- spawn ------------------------------------------------------------------


def spawn(**kwargs) -> worktree.SpawnPlan:
    from pathlib import Path

    defaults = {
        "project": "carameli",
        "workspace_root": Path("/ws"),
        "slug": "voicemail",
        "default_branch": "master",
        "existing_branches": set(),
        "boxes": {},
        "registry": registry(carameli=0),
        "today": TODAY,
    }
    return worktree.spawn_plan(**{**defaults, **kwargs})


def test_spawn_cuts_from_origin_not_from_the_source_checkouts_head():
    """The one place this differs from `sweep.branch_plan`, and the reason is the tier.

    Sweep branches from HEAD because it is rescuing a dirty tree it must not clobber.
    A box starts empty, so anywhere but the tip of the default branch is a stale base
    for no benefit.
    """
    steps = spawn().steps
    assert ("fetch", "--quiet", "origin") in steps
    add = next(s for s in steps if s[0] == "worktree")
    assert add[-1] == "origin/master"


def test_spawn_never_tracks_the_base():
    """`--no-track` for the reason `tb.checkout_argv` documents: otherwise a bare
    `git push` from the box lands the task's commits on the default branch."""
    add = next(s for s in spawn().steps if s[0] == "worktree")
    assert "--no-track" in add


def test_spawn_names_the_branch_the_way_the_hooks_do():
    plan = spawn()
    assert plan.box.branch == "claude/voicemail-0806"
    assert plan.box.name == "carameli--voicemail-0806"
    assert plan.path.replace("\\", "/").endswith(".worktrees/carameli--voicemail-0806")


def test_spawn_disambiguates_against_existing_branches():
    plan = spawn(existing_branches={"claude/voicemail-0806"})
    assert plan.box.branch == "claude/voicemail-0806-2"


def test_spawn_without_a_registry_leases_no_slot():
    """A project with no Docker tier must not spend a slot from a 16-entry ceiling."""
    plan = spawn(registry=None)
    assert plan.box.slot == -1
    assert plan.env == {"COMPOSE_PROJECT_NAME": "carameli--voicemail-0806"}


def test_spawn_can_skip_the_fetch():
    assert all(step[0] != "fetch" for step in spawn(fetch=False).steps)


def test_spawn_records_the_session_that_asked_for_it():
    assert spawn(session="abc123").box.session == "abc123"


def test_the_box_lives_outside_every_checkout():
    """Nested inside a project it would show up as untracked files there -- the exact
    `needs-branch` verdict this tier exists to stop manufacturing."""
    from pathlib import Path

    path = Path(spawn().path)
    assert path.parent.name == worktree.BOXES_DIR_NAME
    assert path.parent.parent == Path("/ws")


# --- reap: the safety property ----------------------------------------------


@pytest.mark.parametrize("verdict", sorted(worktree.SAFE_TO_REAP))
def test_reap_allows_a_box_whose_work_has_left_it(verdict):
    allowed, note = worktree.reap_decision(verdict, "", force=False)
    assert allowed and note == ""


@pytest.mark.parametrize(
    "verdict",
    sorted(sweep.ACTIONABLE - worktree.SAFE_TO_REAP),
)
def test_reap_refuses_while_work_is_only_in_the_box(verdict):
    """The reversion check for the whole design.

    Widen `SAFE_TO_REAP` to include `ready` and this fails -- which is what it is for:
    the tier's entire claim over `sweep.py` is that a box cannot be freed until its
    work has shipped, and `ready` is the verdict that means it has not.
    """
    allowed, note = worktree.reap_decision(verdict, "2 uncommitted file(s)", force=False)
    assert not allowed
    assert "/ship" in note


def test_ready_is_never_reapable():
    """Named explicitly as well as covered by the parametrised case: `ready` is the
    verdict for "the only copy is here", so it is the one that must never widen."""
    assert sweep.READY not in worktree.SAFE_TO_REAP


def test_force_says_what_it_will_destroy():
    allowed, note = worktree.reap_decision(sweep.READY, "2 uncommitted file(s)", force=True)
    assert allowed
    assert "discarded" in note and "2 uncommitted file(s)" in note


# --- reap: the plan ---------------------------------------------------------


def reap(verdict: str = sweep.NEEDS_PR, **kwargs) -> worktree.ReapPlan:
    from pathlib import Path

    defaults = {
        "box": box(),
        "workspace_root": Path("/ws"),
        "state": state(upstream="origin/claude/voicemail-0806", unpushed=0, ahead=2),
        "verdict": verdict,
        "reason": "2 commit(s) pushed",
        "has_stack": True,
    }
    return worktree.reap_plan(**{**defaults, **kwargs})


def test_reap_tears_the_stack_down_before_removing_the_worktree():
    """Order matters: the compose file lives in the worktree that is about to go."""
    plan = reap()
    assert plan.stack_down
    assert plan.steps[0][:2] == ("worktree", "remove")


def test_reap_skips_the_stack_for_a_project_that_has_none():
    assert not reap(has_stack=False).stack_down


def test_keep_stack_leaves_the_containers_running():
    assert not reap(keep_stack=True).stack_down


def test_reap_deletes_the_branch_from_the_source_checkout():
    """The worktree holding the branch is gone by then, so the delete has to run in the
    project, which is what `ReapPlan.project` is for."""
    plan = reap()
    assert plan.project == "carameli"
    assert plan.steps[-1][:1] == ("branch",)


def test_a_refused_reap_plans_nothing_at_all():
    plan = reap(sweep.READY, state=state(dirty=3), reason="3 uncommitted file(s)")
    assert plan.refusal
    assert plan.steps == () and not plan.stack_down and not plan.acts


def test_reap_never_forces_the_worktree_removal_by_default():
    assert "--force" not in reap().steps[0]


def test_force_reap_discards_the_worktree_but_not_the_commits():
    """The asymmetry the whole `--force` design rests on: `worktree remove --force`
    throws away uncommitted edits, `branch -d` still refuses to lose commits."""
    plan = reap(sweep.READY, state=state(dirty=3, ahead=1), reason="3 files", force=True)
    assert "--force" in plan.steps[0]
    assert plan.steps[-1][1] == "-d"


def test_no_reap_plan_ever_emits_a_capital_D_without_the_remote_having_it():
    """A blanket safety sweep over the plan surface, not one path through it."""
    for verdict in sorted(sweep.ACTIONABLE | {sweep.CLEAN}):
        for force in (False, True):
            plan = reap(verdict, state=state(dirty=1), reason="x", force=force, pr_merged=False)
            assert all(step[1] != "-D" for step in plan.steps if step[0] == "branch")


# --- reap: which delete flag ------------------------------------------------


def test_branch_delete_is_forced_once_the_pr_has_merged():
    """A squash merge leaves the branch a non-ancestor of the default branch, so `-d`
    refuses forever -- while GitHub holds every line of it."""
    assert worktree.branch_delete_flag(state(), pr_merged=True) == "-D"


def test_branch_delete_is_forced_when_everything_is_pushed():
    """An open PR: not merged, so not an ancestor, but the remote has every commit."""
    assert (
        worktree.branch_delete_flag(state(upstream="origin/x", unpushed=0), pr_merged=False) == "-D"
    )


def test_branch_delete_defers_to_git_when_commits_are_only_local():
    assert (
        worktree.branch_delete_flag(state(upstream="origin/x", unpushed=2), pr_merged=False) == "-d"
    )


def test_branch_delete_defers_to_git_when_there_is_no_upstream():
    """`unpushed` is -1 for a branch that was never pushed -- not 0, which would read
    as "nothing outstanding" and force-delete the only copy."""
    assert worktree.branch_delete_flag(state(upstream="", unpushed=-1), pr_merged=False) == "-d"


# --- the tiers stay separate ------------------------------------------------


def test_boxes_are_invisible_to_the_sweep():
    """A box is not a folder in the workspace file, so `sweep.py` never sees it. That
    separation is what lets the two tiers keep different lifecycles."""
    workspace = json.dumps({"folders": [{"path": "carameli"}, {"path": ".worktrees"}]})
    # Even a hand-added `.worktrees` entry is a directory of boxes, not a checkout:
    # nothing in the sweep would know what to do with it, which is why `new` never
    # registers one.
    assert "carameli--voicemail-0806" not in sweep.parse_workspace(workspace)


def test_a_failed_teardown_removes_the_box_but_does_not_report_success(tmp_path, monkeypatch):
    """Leaking a container and volume set per task is what makes the VHDX the next
    bottleneck, so a stack that would not come down has to be visible -- while still
    not stranding the box over a daemon that happened to be off."""
    # Any filename: `apply_reap` only reads `workspace.parent`. Deliberately not the
    # live registry's name -- `test_self_hosting.py` flags a test carrying that literal,
    # and a fixture that merely borrows the name is indistinguishable to it.
    workspace = tmp_path / "ws" / "registry.code-workspace"
    workspace.parent.mkdir()
    monkeypatch.setattr(worktree, "compose_down", lambda *a: (False, "docker is not on PATH"))
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))
    monkeypatch.setattr(worktree, "read_leases", lambda root: {})
    monkeypatch.setattr(worktree, "write_leases", lambda root, boxes: None)

    ok, notes = worktree.apply_reap(
        worktree.ReapPlan(box="demo--x-0806", project="demo", stack_down=True), workspace
    )
    assert ok is False
    assert any("lease released" in note for note in notes)
    assert any("may survive" in note for note in notes)


# --- the CLI ----------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["new", "demo", "--slug", "x", "--yes"],
        ["reap", "demo--x-0806", "--yes", "--force"],
        ["list", "--no-fetch"],
    ],
)
def test_flags_are_accepted_after_the_subcommand(args, tmp_path):
    """Regression: `--yes` used to be a top-level-only flag.

    argparse accepts a top-level option only *before* the subcommand, so
    `worktree.py new demo --yes` -- the spelling this module's docstring, its --help
    output and `worktree-guard.py`'s block message all use -- died with "unrecognized
    arguments: --yes". The dry run had already printed a plan by then, so it read as
    the tool refusing to do the thing it had just described.

    Reaching the "no workspace file" return proves argparse accepted the argv:
    an argparse rejection raises SystemExit instead of returning.
    """
    missing = tmp_path / "nope.code-workspace"
    assert worktree.main([*args, "--workspace", str(missing)]) == 2


def test_reap_uses_the_same_classifier_as_the_sweep():
    """One opinion about "does this hold unshipped work". Two would disagree exactly
    when it mattered."""
    assert worktree.SAFE_TO_REAP <= (sweep.ACTIONABLE | sweep.TERMINAL)
