"""Tests for the shared-task dispatcher.

The behaviour worth pinning is the failure text: a task that runs in the wrong
directory, or against a project that does not implement the action, must say so by
name. A traceback from a missing file in an unexpected cwd is the outcome this
script exists to prevent.
"""

import json
import re

import pytest
from support import (
    LIVE_WORKSPACE,
    REPO_ROOT,
    devkit_jsonc,
    devkit_project,
    needs_live_workspace,
)

devkit_jsonc_loads = devkit_jsonc.loads

ACTIONS = devkit_project.ACTIONS
Action = devkit_project.Action
ProjectError = devkit_project.ProjectError
conformance = devkit_project.conformance
known_projects = devkit_project.known_projects
plan_command = devkit_project.plan_command
resolve_project = devkit_project.resolve_project

WORKSPACE = json.dumps({"folders": [{"path": "alpha"}, {"path": "beta"}, {"path": "VanillaLand"}]})


@pytest.fixture
def checkouts(tmp_path):
    """Two conforming-ish checkouts: alpha has both scripts, beta has neither."""
    alpha = tmp_path / "alpha"
    (alpha / "scripts").mkdir(parents=True)
    (alpha / "scripts" / "lint-all.py").write_text("")
    (alpha / "scripts" / "run-tests.py").write_text("")
    (tmp_path / "beta").mkdir()
    return tmp_path


# --- the registry -----------------------------------------------------------


def test_projects_come_from_the_workspace_registry():
    assert known_projects(WORKSPACE) == ["alpha", "beta"]


def test_the_reference_checkout_is_not_a_project():
    # VanillaLand ships no harness; nothing in ACTIONS applies to it.
    assert "VanillaLand" not in known_projects(WORKSPACE)


# --- resolution -------------------------------------------------------------


def test_a_registered_project_resolves_to_its_directory(checkouts):
    assert resolve_project("alpha", ["alpha", "beta"], checkouts) == checkouts / "alpha"


def test_an_unknown_project_names_the_real_ones(checkouts):
    with pytest.raises(ProjectError, match=r"unknown project 'gamma'.*alpha, beta"):
        resolve_project("gamma", ["alpha", "beta"], checkouts)


def test_an_empty_project_is_rejected_rather_than_defaulting(checkouts):
    # A picker that supplies "" must not silently run somewhere plausible.
    with pytest.raises(ProjectError, match="no project given"):
        resolve_project("", ["alpha", "beta"], checkouts)


def test_registered_but_missing_directory_is_distinguished(checkouts):
    with pytest.raises(ProjectError, match="registered in the workspace but"):
        resolve_project("ghost", ["alpha", "beta", "ghost"], checkouts)


# --- command planning -------------------------------------------------------


def test_command_runs_the_projects_own_script(checkouts):
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [])
    assert command == ["python", "scripts/lint-all.py"]


def test_fixed_action_args_come_before_caller_args(checkouts):
    command = plan_command(ACTIONS["lint-changed"], checkouts / "alpha", ["--verbose"])
    assert command == ["python", "scripts/lint-all.py", "--changed", "--verbose"]


def test_empty_picker_tokens_are_dropped(checkouts):
    """VS Code pickers can yield "", which argparse would read as a stray positional.

    devkit's tasks.json carries redundant-looking flags precisely to avoid this; the
    dispatcher drops empties too so a task cannot fail on an invisible argument.
    """
    assert plan_command(ACTIONS["test"], checkouts / "alpha", ["", "-k", ""]) == [
        "python",
        "scripts/run-tests.py",
        "-k",
    ]


def test_notify_wrap_is_used_when_the_project_ships_it(checkouts):
    (checkouts / "alpha" / "scripts" / "notify-wrap.py").write_text("")
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [])
    assert command[:3] == ["python", "scripts/notify-wrap.py", "Lint: Everything"]
    assert command[3] == "--"
    assert command[4:] == ["python", "scripts/lint-all.py"]


def test_a_project_missing_the_script_is_named(checkouts):
    with pytest.raises(ProjectError, match="beta does not implement this action"):
        plan_command(ACTIONS["lint"], checkouts / "beta", [])


# --- devkit-owned actions ---------------------------------------------------


def test_devkit_owned_action_uses_an_absolute_path(checkouts, tmp_path):
    """It runs with cwd set to the checkout, so a relative path would miss."""
    devkit_root = tmp_path / "dk"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "git-sync-keep.py").write_text("")
    command = plan_command(ACTIONS["sync-branch"], checkouts / "beta", [], devkit_root)
    assert command == ["python", str(devkit_root / "scripts" / "git-sync-keep.py")]


def test_devkit_owned_action_works_in_a_non_conforming_checkout(checkouts, tmp_path):
    # beta ships no scripts/ at all — the ibkr_trader case. A devkit-owned action
    # must still run there; that is the whole point of the DEVKIT owner.
    devkit_root = tmp_path / "dk"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "docker-maint.py").write_text("")
    command = plan_command(ACTIONS["docker-prune"], checkouts / "beta", [], devkit_root)
    assert command[-1] == "prune"


def test_a_broken_devkit_checkout_is_distinguished_from_a_project_gap(checkouts, tmp_path):
    with pytest.raises(ProjectError, match="devkit is missing"):
        plan_command(ACTIONS["sync-branch"], checkouts / "alpha", [], tmp_path / "empty")


def test_docker_up_forces_a_rebuild(checkouts, tmp_path):
    """Keep the `--build` the hoisted carameli task carried.

    Dropping it would make "Docker: Start Stack" quietly start a stale image after a
    requirements or Dockerfile change — a stack that comes up healthy running last
    week's code, which nothing downstream reports.
    """
    devkit_root = tmp_path / "devkit"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "docker-maint.py").write_text("")
    command = plan_command(ACTIONS["docker-up"], checkouts / "beta", [], devkit_root)
    assert command[-2:] == ["up", "--build"]


def test_stack_actions_are_devkit_owned_with_a_project_override(checkouts, tmp_path):
    """PROJECT-owned would make the shared contract unsatisfiable where it should be.

    devkit and a `bare` preset have no compose stack at all, so demanding a
    `docker-up.py` from every checkout would report them as non-conforming for
    correctly lacking one. DEVKIT-owned + `docker-maint.py`'s `DELEGATES` gives both
    halves: a generic `compose up -d` that works in a freshly generated project, and
    carameli's health-polling script when the repo ships one.
    """
    devkit_root = tmp_path / "devkit"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "docker-maint.py").write_text("")
    for key in ("docker-up", "docker-down"):
        assert ACTIONS[key].owner == devkit_project.DEVKIT
        # Runs in a checkout that ships no scripts/ of its own.
        plan_command(ACTIONS[key], checkouts / "beta", [], devkit_root)


def test_hook_tests_is_devkit_owned_so_every_checkout_can_run_it(checkouts, tmp_path):
    """`pytest scripts/hooks/tests/ -q` is byte-identical in every consumer.

    The vendored tier is at the same path everywhere (it is in the MANIFEST) and
    pytest's `testpaths` excludes it everywhere, so a PROJECT-owned version would be
    four identical scripts. DEVKIT-owned means it runs in a checkout that ships no
    `scripts/` of its own at all.
    """
    devkit_root = tmp_path / "devkit"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "hook-tests.py").write_text("")
    command = plan_command(ACTIONS["test-hooks"], checkouts / "beta", [], devkit_root)
    assert command[1] == str(devkit_root / "scripts" / "hook-tests.py")


# --- project-scoped actions -------------------------------------------------
#
# The mechanism that let the last two `.vscode/tasks.json` files be deleted. A task
# defined inside a repo is rendered once per WORKTREE folder, so carameli's Playwright
# run appeared twice in the quick-pick with nothing to tell the copies apart. Scoping
# moves it up without claiming every checkout can run it.

check_scope = devkit_project.check_scope
expected_actions = devkit_project.expected_actions
in_scope = devkit_project.in_scope


def test_an_unscoped_action_applies_everywhere():
    assert in_scope(ACTIONS["lint"], "anything-at-all")


def test_a_scoped_action_applies_to_both_halves_of_its_worktree_pair():
    """Both halves always, because they are two checkouts of one repo — a script that
    exists in one exists in the other, so scoping to just the primary would make the `-b`
    picker option a guaranteed error."""
    for name in ("carameli", "carameli-b"):
        assert in_scope(ACTIONS["e2e"], name)
    for name in ("ibkr_trader", "ibkr_trader-b"):
        assert in_scope(ACTIONS["backtest"], name)


def test_an_out_of_scope_checkout_is_refused_by_name():
    """Not left to the missing-script error, which reads like "devkit has not implemented
    backtesting yet" and invites someone to go and implement it."""
    with pytest.raises(ProjectError, match=r"devkit is out of scope.*ibkr_trader"):
        check_scope(ACTIONS["backtest"], "devkit")


def test_scoping_crosses_neither_direction_between_the_two_repos():
    assert not in_scope(ACTIONS["e2e"], "ibkr_trader")
    assert not in_scope(ACTIONS["ingest"], "carameli")


def test_an_in_scope_checkout_passes_the_check():
    check_scope(ACTIONS["e2e"], "carameli-b")  # must not raise


def test_the_scoped_actions_cover_every_hoisted_project_task():
    """The eight that came out of the two deleted files, by action key.

    Listed rather than counted: a missing entry here means a task the user used to be
    able to click is now unreachable from anywhere, which nothing else in the suite
    notices — `test_every_action_is_reachable_from_a_task` only checks the actions that
    still exist.
    """
    scoped = {key for key, action in ACTIONS.items() if action.projects}
    assert scoped == {
        "test-target",
        "e2e",
        "ngrok",
        "vnc",
        "ingest",
        "snapshot-monthly",
        "backtest",
        "backtest-oos",
    }


def test_the_two_backtest_actions_share_a_script_and_differ_by_subcommand():
    """The OOS run fixes its own warm-up and simulation starts, so it cannot be `backtest`
    with different picker answers — it is a separate subcommand of one script."""
    assert ACTIONS["backtest"].script == ACTIONS["backtest-oos"].script
    assert ACTIONS["backtest"].args == ("run",)
    assert ACTIONS["backtest-oos"].args == ("oos",)


# --- conformance ------------------------------------------------------------


def test_a_scoped_action_is_not_expected_of_other_projects():
    """Without this, hoisting carameli's Playwright task would report ibkr_trader and
    devkit as missing `scripts/run-e2e.py` — a gap neither should ever close, and the
    kind of noise that teaches everyone to stop reading `--check`."""
    assert "e2e" in expected_actions("carameli")
    assert "e2e" not in expected_actions("ibkr_trader")
    assert "e2e" not in expected_actions("devkit")


def test_unscoped_actions_are_expected_of_everyone():
    assert {"test", "lint", "lint-changed"} <= expected_actions("devkit")


def test_conformance_reports_per_project_support(checkouts):
    report = conformance(["alpha", "beta"], checkouts)
    assert set(report["alpha"]) == {"lint", "lint-changed", "test"}
    assert report["beta"] == []


def test_conformance_ignores_devkit_owned_actions(checkouts):
    """Otherwise every checkout looks conformant and the real gap is hidden."""
    devkit_owned = {k for k, a in ACTIONS.items() if a.owner == devkit_project.DEVKIT}
    assert devkit_owned, "expected at least one devkit-owned action"
    reported = set(conformance(["alpha", "beta"], checkouts)["alpha"])
    assert not (reported & devkit_owned)


# --- registration -----------------------------------------------------------

RegistryEditError = devkit_project.RegistryEditError
register = devkit_project.register

COMMENTED = """{
\t// The one workspace. sweep.py reads this as the project registry.
\t"folders": [
\t\t{
\t\t\t"path": "carameli"
\t\t},
\t\t{
\t\t\t"name": "VanillaLand (reference)",
\t\t\t"path": "VanillaLand"
\t\t}
\t],
\t"tasks": {
\t\t"version": "2.0.0",
\t\t"tasks": [],
\t\t"inputs": [
\t\t\t{
\t\t\t\t// MAINTAINED BY new-project.py
\t\t\t\t"id": "project",
\t\t\t\t"type": "pickString",
\t\t\t\t"description": "Which checkout to run this in",
\t\t\t\t"options": [
\t\t\t\t\t"carameli"
\t\t\t\t],
\t\t\t\t"default": "carameli"
\t\t\t}
\t\t]
\t}
}
"""


def test_registration_adds_the_project_to_the_registry():
    updated = register(COMMENTED, ["newproj"])
    assert "newproj" in devkit_project.known_projects(updated)


def test_registration_adds_the_picker_option():
    updated = register(COMMENTED, ["newproj"])
    options = devkit_jsonc_loads(updated)["tasks"]["inputs"][0]["options"]
    assert options == ["carameli", "newproj"]


def test_registration_preserves_comments():
    """A json.dumps round-trip would delete these; the folder list would lose the only
    place it explains what VanillaLand is and why sweep depends on it."""
    updated = register(COMMENTED, ["newproj"])
    assert "sweep.py reads this as the project registry" in updated
    assert "MAINTAINED BY new-project.py" in updated


def test_reference_checkouts_stay_last():
    updated = register(COMMENTED, ["newproj"])
    paths = [f["path"] for f in devkit_jsonc_loads(updated)["folders"]]
    assert paths == ["carameli", "newproj", "VanillaLand"]


def test_registering_a_project_and_its_worktree():
    updated = register(COMMENTED, ["newproj", "newproj-b"])
    assert devkit_project.known_projects(updated) == ["carameli", "newproj", "newproj-b"]


def test_registration_is_idempotent():
    """new-project.py can be re-run over an existing name; that must not double-add."""
    once = register(COMMENTED, ["newproj"])
    twice = register(once, ["newproj"])
    assert once == twice


def test_the_result_is_still_valid_jsonc():
    updated = register(COMMENTED, ["newproj", "newproj-b"])
    assert devkit_jsonc_loads(updated)["tasks"]["version"] == "2.0.0"


def test_a_workspace_without_a_folders_array_is_refused():
    with pytest.raises(RegistryEditError, match=r"no .folders. array"):
        register('{"tasks": {}}', ["x"])


@needs_live_workspace
def test_registering_against_the_real_workspace_file():
    """The shape assertions above are on a fixture; this proves them on the live file."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    updated = register(text, ["probe", "probe-b"])
    assert "probe" in devkit_project.known_projects(updated)
    assert "probe-b" in devkit_project.known_projects(updated)
    options = next(
        i for i in devkit_jsonc_loads(updated)["tasks"]["inputs"] if i["id"] == "project"
    )["options"]
    assert options[-2:] == ["probe", "probe-b"]
    # VanillaLand is a reference checkout and must not drift into the middle.
    assert [f["path"] for f in devkit_jsonc_loads(updated)["folders"]][-1] == "VanillaLand"


# --- the canonical task block ------------------------------------------------

tasks_drift = devkit_project.tasks_drift
workspace_tasks = devkit_project.workspace_tasks


@pytest.fixture
def canonical():
    return devkit_jsonc_loads(devkit_project.CANONICAL_TASKS.read_text(encoding="utf-8"))


def test_the_canonical_block_exists_and_parses(canonical):
    assert canonical["version"] == "2.0.0"
    assert canonical["tasks"], "the canonical task block defines no tasks"


def test_no_drift_against_itself(canonical):
    assert tasks_drift(canonical, canonical) == []


def test_drift_reports_a_missing_task(canonical):
    trimmed = {**canonical, "tasks": canonical["tasks"][1:]}
    problems = tasks_drift(trimmed, canonical)
    assert any(p.startswith("missing from the workspace:") for p in problems)


def test_drift_reports_a_changed_definition(canonical):
    changed = {
        **canonical,
        "tasks": [{**canonical["tasks"][0], "command": "nope"}, *canonical["tasks"][1:]],
    }
    assert any(p.startswith("definition differs:") for p in tasks_drift(changed, canonical))


def test_drift_reports_an_extra_input(canonical):
    extra = {**canonical, "inputs": [*canonical["inputs"], {"id": "stray"}]}
    assert "input not in devkit: stray" in tasks_drift(extra, canonical)


def _dispatched_actions(canonical) -> dict[str, str]:
    """{action key: task label} for every task routed through `devkit_project.py`."""
    found: dict[str, str] = {}
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not args or not args[0].endswith("devkit_project.py"):
            continue
        # The dispatcher's CLI is `--project <name> <action> [extra…]`.
        index = args.index("--project")
        found[args[index + 2]] = task["label"]
    return found


def test_every_dispatched_task_names_a_real_action(canonical):
    """The workspace block passes the action key through VERBATIM.

    That is the whole seam between the two files, and it is stringly-typed: a task
    naming `docker-upp` is not a parse error anywhere — VS Code renders it, the click
    succeeds, and argparse rejects the choice several layers down, in a terminal, with
    the project picker already answered. This is the only place that mismatch can be
    caught before someone clicks it.
    """
    unknown = {
        action: label
        for action, label in _dispatched_actions(canonical).items()
        if action not in ACTIONS
    }
    assert not unknown, f"tasks naming actions the dispatcher does not define: {unknown}"


def test_every_action_is_reachable_from_a_task(canonical):
    """The other direction: an action nobody can click is dead weight in ACTIONS.

    Adding an entry to `ACTIONS` is documented as the only step a new generic task
    needs — which is true only if the task block is actually extended to call it.
    Without this, a half-finished hoist leaves an action that exists, is tested, and
    is reachable only by typing the CLI nobody uses.
    """
    unreachable = set(ACTIONS) - set(_dispatched_actions(canonical))
    assert not unreachable, f"actions with no task to invoke them: {sorted(unreachable)}"


def test_every_task_has_a_label_and_a_detail(canonical):
    """CLAUDE.md's convention: `detail` is the only place a one-click action can
    state its cost or blast radius."""
    for task in canonical["tasks"]:
        assert task.get("label"), task
        assert task.get("detail"), f"{task['label']} has no detail"


def test_every_task_has_an_icon(canonical):
    """With every task consolidated into one list, the icon is what makes it navigable.

    A task with no icon renders as a bare label in a list of twenty-nine, which is the
    state this consolidation would otherwise have created.
    """
    for task in canonical["tasks"]:
        icon = task.get("icon", {})
        assert icon.get("id"), f"{task['label']} has no icon id"
        assert icon.get("color"), f"{task['label']} has no icon colour"


def test_no_two_tasks_share_an_icon_and_colour(canonical):
    """An icon repeated under two labels is worse than no icon: it reads as "same kind of
    thing" to the eye and then is not.

    Two pairs were exactly that before the consolidation — `beaker`/green under both test
    tasks, `checklist`/yellow under both lint tasks.
    """
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for task in canonical["tasks"]:
        icon = task.get("icon", {})
        key = (icon.get("id", ""), icon.get("color", ""))
        if key in seen:
            clashes.append(f"{seen[key]} and {task['label']} both use {key[0]}/{key[1]}")
        seen[key] = task["label"]
    assert not clashes, "; ".join(clashes)


def test_a_scoped_task_offers_exactly_the_checkouts_its_action_allows(canonical):
    """The seam between this file and `Action.projects`, asserted from both ends.

    These have to agree or the picker is a trap: an option the dispatcher refuses looks
    like a supported choice right up to the point it fails in a terminal, with the rest of
    the inputs already answered. Offering FEWER than the action allows is the quieter
    failure — the `-b` worktree silently stops being reachable from the editor.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    checked = 0
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not args or not args[0].endswith("devkit_project.py") or "--project" not in args:
            continue
        index = args.index("--project")
        picker = re.fullmatch(r"\$\{input:([A-Za-z_][A-Za-z0-9_]*)\}", args[index + 1])
        action = ACTIONS.get(args[index + 2])
        if picker is None or action is None or not action.projects:
            continue
        offered = [
            option if isinstance(option, str) else option["value"]
            for option in inputs[picker.group(1)]["options"]
        ]
        assert set(offered) == set(action.projects), (
            f"{task['label']}: picker offers {sorted(offered)} but the action is defined "
            f"for {sorted(action.projects)}"
        )
        checked += 1
    assert checked, "no scoped task found — the wiring this test guards is gone"


def test_every_input_referenced_is_defined(canonical):
    """An undefined ${input:…} fails at click time with an opaque error."""
    defined = {i["id"] for i in canonical["inputs"]}
    referenced = set(re.findall(r"\$\{input:([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(canonical)))
    assert referenced <= defined, f"undefined inputs: {referenced - defined}"
    assert defined <= referenced, f"unused inputs: {defined - referenced}"


def test_every_mutating_sweep_task_offers_the_scope_picker(canonical):
    """`--only` restricts every sweep mode, so every step that changes a checkout has
    to let you aim it at one.

    Step 3 shipped without the picker and so was all-or-nothing: when a sync failed in
    one repo, the only way to retry it was the CLI, and the fallback for a one-click
    workflow being unable to express "just this one" is re-running it over every
    checkout. The read-only modes are deliberately exempt — an unscoped sweep IS the
    report, and a scoped one answers a question nobody asked of it.
    """
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not any("sweep.py" in a for a in args):
            continue
        if not {"--branch", "--ship", "--sync"} & set(args):
            continue
        assert "${input:sweepScope}" in args, (
            f"{task['label']} changes checkouts but cannot be scoped to one"
        )


def test_some_task_still_routes_through_the_dispatcher(canonical):
    """The wiring itself, asserted separately from what it points at.

    This is what survives of a second "is it a real action?" check that guessed the action
    from `args[-1]`, falling back to `args[-2]`. The guess held only while every dispatched
    task ended with the action key or a picker; the hoisted tasks end with real arguments
    (`--arg=${input:ingestArg}`, a TigerVNC path), so it started reporting those as unknown
    actions. `test_every_dispatched_task_names_a_real_action` above makes the same
    assertion off the dispatcher's actual CLI shape — `--project <name> <action>` — which
    is positional and does not need guessing. Only the emptiness check was unique to it.
    """
    dispatched = _dispatched_actions(canonical)
    assert dispatched, "no task routes through the dispatcher — the wiring is gone"


@needs_live_workspace
def test_the_live_workspace_matches_the_canonical_block(canonical):
    """The check `--check-tasks` runs, as a test so devkit's own gate catches drift."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    problems = tasks_drift(workspace_tasks(text), canonical)
    assert not problems, "run `python scripts/devkit_project.py --adopt-tasks`: " + "; ".join(
        problems
    )


@needs_live_workspace
def test_the_project_picker_lists_only_real_checkouts():
    """A stale picker entry is caught by resolve_project, but it should not be there."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    picker = next(i for i in workspace_tasks(text)["inputs"] if i["id"] == "project")
    assert set(picker["options"]) <= set(devkit_project.known_projects(text))


# --- the real repos ---------------------------------------------------------


def test_devkit_itself_implements_every_action_it_is_on_the_hook_for():
    """devkit is upstream: if it cannot satisfy its own contract, the contract is wrong.

    "Its own" is `expected_actions("devkit")`, not every PROJECT-owned action — the scoped
    ones belong to one repo's worktree pair and demanding a `run-e2e.py` or an IBKR
    backtest of devkit would be asking it to grow a frontend and a broker.
    """
    expected = expected_actions("devkit")
    report = conformance(["devkit"], REPO_ROOT.parent)
    assert set(report["devkit"]) == expected, (
        f"devkit is missing: {sorted(expected - set(report['devkit']))}"
    )
    assert not (expected & {"e2e", "backtest"}), "a scoped action leaked into devkit's contract"


def test_every_devkit_owned_script_exists():
    """The other half: a DEVKIT action pointing at a script devkit does not ship."""
    missing = [
        a.script
        for a in ACTIONS.values()
        if a.owner == devkit_project.DEVKIT and not (REPO_ROOT / a.script).is_file()
    ]
    assert not missing, f"devkit-owned scripts missing: {missing}"
