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


# --- conformance ------------------------------------------------------------


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


def test_every_task_has_a_label_and_a_detail(canonical):
    """CLAUDE.md's convention: `detail` is the only place a one-click action can
    state its cost or blast radius."""
    for task in canonical["tasks"]:
        assert task.get("label"), task
        assert task.get("detail"), f"{task['label']} has no detail"


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


def test_every_dispatched_action_is_a_real_action(canonical):
    """A task naming an action `devkit_project` does not implement fails only when
    someone clicks it, and only for that one task."""
    dispatched = set()
    for task in canonical["tasks"]:
        args = task.get("args", [])
        if any("devkit_project.py" in str(a) for a in args):
            dispatched.add(args[-1] if args[-1] not in ("", None) else args[-2])
    unknown = {a for a in dispatched if a not in ACTIONS and not a.startswith("${input:")}
    assert not unknown, f"tasks dispatch to unknown actions: {unknown}"
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


def test_devkit_itself_implements_every_action():
    """devkit is upstream: if it cannot satisfy its own contract, the contract is wrong."""
    expected = {key for key, a in ACTIONS.items() if a.owner == devkit_project.PROJECT}
    report = conformance(["devkit"], REPO_ROOT.parent)
    assert set(report["devkit"]) == expected, (
        f"devkit is missing: {sorted(expected - set(report['devkit']))}"
    )


def test_every_devkit_owned_script_exists():
    """The other half: a DEVKIT action pointing at a script devkit does not ship."""
    missing = [
        a.script
        for a in ACTIONS.values()
        if a.owner == devkit_project.DEVKIT and not (REPO_ROOT / a.script).is_file()
    ]
    assert not missing, f"devkit-owned scripts missing: {missing}"
