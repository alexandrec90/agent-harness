"""Tests for scripts/upgrade-project.py (adopting a devkit release in a consumer).

The refusals are the interesting half. An upgrade that runs when it should not is
how a harness bump ends up mixed into unrelated work — the failure that motivated
this script — so each precondition is pinned individually.
"""

import datetime as dt
import subprocess

from support import load_script, sweep

up = load_script("scripts/upgrade-project.py")

DATE = dt.date(2026, 8, 2)


def clean(**overrides) -> sweep.State:
    """A consumer sitting on its home branch with nothing uncommitted."""
    base = {
        "name": "carameli",
        "default_branch": "master",
        "branch": "master",
        "host": "github",
    }
    return sweep.State(**{**base, **overrides})


# --- naming ------------------------------------------------------------------


def test_the_branch_is_a_dated_task_branch():
    assert up.branch_name(DATE) == "claude/devkit-upgrade-0802"


def test_the_commit_names_the_release():
    """Unlike a swept commit, the version really is the description here."""
    assert up.commit_message("v0.5.3", 38) == "Adopt devkit v0.5.3 (38 vendored file(s))"


def test_the_pr_body_lists_all_four_pins_and_the_previous_version():
    body = up.pr_body("v0.5.3", "v0.5.2", ["scripts/hooks/stop.py"])
    assert "v0.5.3" in body and "v0.5.2" in body
    assert "DEVKIT_VERSION" in body
    assert "rev:" in body
    assert "ref:" in body
    assert "- `scripts/hooks/stop.py`" in body


def test_the_pr_body_truncates_a_long_file_list():
    files = [f"scripts/hooks/f{i}.py" for i in range(120)]
    body = up.pr_body("v0.5.3", "v0.5.2", files)
    assert f"and {120 - sweep.PR_BODY_FILE_LIMIT} more" in body


# --- refusals ----------------------------------------------------------------


def test_a_clean_project_on_its_home_branch_is_upgradable():
    assert up.refusal(clean(), "v0.5.3") == ""
    assert up.plan(clean(), "v0.5.3", DATE).steps == (
        ("checkout", "-b", "claude/devkit-upgrade-0802"),
    )


def test_an_untagged_devkit_is_refused():
    """The whole point of the release guard: there is no release to adopt."""
    reason = up.refusal(clean(), None)
    assert "not tagged" in reason
    assert up.plan(clean(), None, DATE).refusal


def test_a_dirty_project_is_refused():
    """A branch cut under uncommitted work carries it along, and the commit would
    have to guess which files belonged to the upgrade."""
    reason = up.refusal(clean(dirty=364), "v0.5.3")
    assert "uncommitted" in reason
    assert not up.plan(clean(dirty=364), "v0.5.3", DATE).steps


def test_a_project_already_on_a_task_branch_is_refused():
    reason = up.refusal(clean(branch="claude/thing-0801"), "v0.5.3")
    assert "task branch" in reason


def test_a_non_git_directory_is_refused():
    assert up.refusal(clean(is_git=False), "v0.5.3") == "not a git checkout"


def test_every_refusal_state_yields_a_plan_that_says_why():
    """No silent no-ops: a plan with neither steps nor a refusal reads as done."""
    states = [
        clean(),
        clean(dirty=1),
        clean(is_git=False),
        clean(branch="claude/x-0801"),
    ]
    for state in states:
        for tag in ("v0.5.3", None):
            built = up.plan(state, tag, DATE)
            assert built.steps or built.refusal, (state, tag)


def test_the_upgrade_records_the_home_branch_it_came_from():
    # So `sweep --sync` can park the worktree back afterwards.
    assert up.plan(clean(branch="carameli-b"), "v0.5.3", DATE).anchor == "carameli-b"


# --- what gets committed -----------------------------------------------------


class FakeGit:
    def __init__(self, porcelain: str = ""):
        self.porcelain = porcelain

    def __call__(self, *args: str):
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=0, stdout=self.porcelain, stderr=""
        )


def test_changed_paths_reports_everything_the_pull_touched():
    """Safe to take wholesale because the tree was clean beforehand -- the refusal
    above is what makes `add -A` correct rather than reckless."""
    git = FakeGit(" M DEVKIT_VERSION\n M .pre-commit-config.yaml\n?? scripts/hooks/new.py\n")
    assert up.changed_paths(git) == [
        "DEVKIT_VERSION",
        ".pre-commit-config.yaml",
        "scripts/hooks/new.py",
    ]


def test_an_already_current_project_has_nothing_to_commit():
    assert up.changed_paths(FakeGit("")) == []
