"""Tests for scripts/upgrade-project.py (adopting a devkit release in a consumer).

The refusals are the interesting half. An upgrade that runs when it should not is
how a harness bump ends up mixed into unrelated work — the failure that motivated
this script — so each precondition is pinned individually.
"""

import contextlib
import datetime as dt
import json
import subprocess

import pytest
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


def test_a_devkit_with_no_releases_is_refused():
    """There is nothing to adopt. Note this is about *tags existing*, not about
    where devkit's HEAD happens to sit -- keying off HEAD made this refuse on
    nearly every run, since devkit normally lives on a working branch."""
    reason = up.refusal(clean(), None)
    assert "no release tags" in reason
    assert up.plan(clean(), None, DATE).refusal


def test_a_project_already_on_the_tag_is_current(tmp_path):
    """The scheduled-run case: proving a project is up to date reads one file and
    touches nothing, so it cannot fail on a dirty tree or the wrong branch."""
    (tmp_path / "DEVKIT_VERSION").write_text("v0.5.3\n", encoding="utf-8")
    assert up.is_current(tmp_path, "v0.5.3")
    assert not up.is_current(tmp_path, "v0.5.4")


def test_a_project_that_never_vendored_is_not_current(tmp_path):
    assert not up.is_current(tmp_path, "v0.5.3")


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


class RecordingGit:
    """Records calls; `fail_on` makes the first matching call fail."""

    def __init__(self, fail_on: str = ""):
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        failed = bool(self.fail_on) and self.fail_on in " ".join(args)
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1 if failed else 0, stdout="", stderr="nope"
        )


def test_a_no_op_upgrade_leaves_no_branch_behind():
    """This is meant to run on a schedule to prove nothing is stale, so the
    already-current path has to be free: one empty claude/devkit-upgrade branch per
    check would be litter that --sync then has to reap."""
    git = RecordingGit()
    assert up._abandon(git, "master", "already current", code=0) == 0
    assert git.calls[0] == ("checkout", "master")
    assert git.calls[1][:2] == ("branch", "-d")


def test_abandoning_never_force_deletes():
    """`branch -d` refusing means the run did more than it thought -- a state for a
    human, not one to force past."""
    git = RecordingGit(fail_on="branch -d")
    assert up._abandon(git, "master", "already current", code=0) == 2
    assert not any(step[:2] == ("branch", "-D") for step in git.calls)


def test_abandoning_reports_when_it_cannot_get_home():
    git = RecordingGit(fail_on="checkout")
    assert up._abandon(git, "master", "the pull refused", code=2) == 2
    assert not any(step[:2] == ("branch", "-d") for step in git.calls)


# --- scope: which checkouts `--all` covers -----------------------------------


def test_the_devkit_source_is_never_its_own_upgrade_target():
    """It is where the release comes from; pulling it into itself is meaningless."""
    assert up.select_all([up.Candidate("devkit", is_source=True)]) == [("devkit", up.SKIP_SOURCE)]


def test_the_source_is_reported_as_the_source_even_though_it_never_vendored():
    """devkit has no DEVKIT_VERSION either, and "it never adopted" would be a
    misleading way to describe the repo that publishes the releases."""
    decided = up.select_all([up.Candidate("devkit", adopts=False, is_source=True)])
    assert decided == [("devkit", up.SKIP_SOURCE)]


def test_a_checkout_that_never_vendored_is_skipped_not_refused():
    """A workspace holds checkouts that were never generated from devkit. Passing
    over one is not a failure, and must not colour the exit code of a run that
    upgraded everything it could."""
    assert up.select_all([up.Candidate("reference-repo", adopts=False)]) == [
        ("reference-repo", up.SKIP_UNADOPTED)
    ]


def test_worktree_siblings_are_upgraded_once_and_the_first_listed_wins():
    """Both would cut `claude/devkit-upgrade-<mmdd>` in one ref store, so the second
    would fail on a branch that already exists -- and land a duplicate PR if it did not."""
    decided = up.select_all(
        [
            up.Candidate("carameli", common_dir="/repos/carameli/.git"),
            up.Candidate("carameli-b", common_dir="/repos/carameli/.git"),
        ]
    )
    assert decided[0] == ("carameli", "")
    assert "shares a repo with carameli" in decided[1][1]


def test_the_sibling_skip_names_the_way_around_it():
    """First-listed-wins is arbitrary when that one turns out to be dirty, so the
    message has to point at the escape hatch: name the sibling explicitly."""
    decided = up.select_all(
        [
            up.Candidate("carameli", common_dir="/repos/carameli/.git"),
            up.Candidate("carameli-b", common_dir="/repos/carameli/.git"),
        ]
    )
    assert "name this one explicitly" in decided[1][1]


def test_two_clones_of_one_remote_are_both_upgraded():
    """Separate ref stores, separate vendored copies -- each needs its own PR."""
    decided = up.select_all(
        [
            up.Candidate("carameli", common_dir="/repos/a/.git"),
            up.Candidate("carameli-clone", common_dir="/repos/b/.git"),
        ]
    )
    assert decided == [("carameli", ""), ("carameli-clone", "")]


def test_checkouts_git_would_not_identify_are_not_collapsed_together():
    """An empty ref store is "unknown", not "the same one" -- bucketing them would
    silently skip every project after the first."""
    decided = up.select_all([up.Candidate("one"), up.Candidate("two")])
    assert decided == [("one", ""), ("two", "")]


def test_candidates_read_the_source_and_the_stamp_off_disk(tmp_path):
    devkit = tmp_path / "devkit"
    devkit.mkdir()
    adopter = tmp_path / "carameli"
    adopter.mkdir()
    (adopter / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    (tmp_path / "VanillaLand").mkdir()

    built = up.candidates_for(tmp_path, ["devkit", "carameli", "VanillaLand"], devkit)
    assert [c.is_source for c in built] == [True, False, False]
    assert [c.adopts for c in built] == [True, True, False]


def test_a_missing_checkout_directory_is_an_unadopted_one(tmp_path):
    """`--all` reads names from the workspace file, which can outlive a directory.
    Reporting the skip beats crashing the whole run on one stale entry."""
    built = up.candidates_for(tmp_path, ["gone"], tmp_path / "devkit")
    assert built == [up.Candidate("gone", adopts=False)]


# --- the CLI scope contract ---------------------------------------------------


def workspace(tmp_path, *names):
    """A `.code-workspace` listing `names`, as `parse_workspace` reads them."""
    path = tmp_path / "alex-projects.code-workspace"
    path.write_text(json.dumps({"folders": [{"path": n} for n in names]}), encoding="utf-8")
    return path


def test_a_scope_is_mandatory():
    """Neither branch of the VS Code picker can emit nothing, and a bare run that
    guessed "all" would open PRs nobody asked for."""
    with pytest.raises(SystemExit) as exit_info:
        up.main([])
    assert exit_info.value.code == 2


def test_naming_a_project_and_all_at_once_is_rejected():
    with pytest.raises(SystemExit) as exit_info:
        up.main(["carameli", "--all"])
    assert exit_info.value.code == 2


def test_an_untagged_devkit_stops_the_whole_run_once(tmp_path, capsys):
    """Same fact about devkit for every project; repeating it per project would read
    as four problems rather than one."""
    ws = workspace(tmp_path, "carameli", "carameli-b")
    assert up.main(["--all", "--workspace", str(ws), "--devkit", str(tmp_path / "nope")]) == 1
    assert capsys.readouterr().err.count("no release tags") == 1


def test_naming_the_devkit_source_is_an_error_not_a_skip(tmp_path, capsys, monkeypatch):
    """A skip is what `--all` does with a checkout it was not asked about. Asked
    directly, this cannot do what the operator wants, and must not exit 0."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    (tmp_path / "devkit").mkdir()
    ws = workspace(tmp_path, "devkit")
    code = up.main(["devkit", "--workspace", str(ws), "--devkit", str(tmp_path / "devkit")])
    assert code == 2
    assert up.SKIP_SOURCE in capsys.readouterr().err


def test_a_run_where_every_project_is_current_touches_nothing(tmp_path, capsys, monkeypatch):
    """The scheduled-run property, now at workspace scale: proving nothing is stale
    must not cut a branch, and must not build the source worktree either."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(
        up, "source_at_tag", lambda *_a: pytest.fail("built a worktree with nothing to pull")
    )
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("v0.5.3\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert capsys.readouterr().out.count("already on devkit v0.5.3") == 2


def test_one_project_refusing_does_not_stop_the_others(tmp_path, capsys, monkeypatch):
    """Independent repos, independent PRs. Stopping at the first refusal would make
    `--all` useless the moment one checkout is mid-task."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    seen: list[str] = []

    def fake_upgrade(name, project, tag, source=None):
        seen.append(name)
        return 1 if name == "carameli" else 0

    monkeypatch.setattr(up, "upgrade_one", fake_upgrade)
    monkeypatch.setattr(up, "source_at_tag", _no_worktree)
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 1
    assert seen == ["carameli", "ibkr_trader"]


def test_the_run_reports_the_worst_outcome(tmp_path, monkeypatch):
    """A failure mid-flight (2) outranks a refusal (1), which outranks a clean run:
    the exit code is what a scheduled invocation alerts on."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "source_at_tag", _no_worktree)
    codes = iter([1, 2])
    monkeypatch.setattr(up, "upgrade_one", lambda *_a, **_kw: next(codes))
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 2


def test_one_source_worktree_serves_the_whole_run(tmp_path, monkeypatch):
    """Every project adopts the same revision, so checking it out once per project
    would pay the clone cost N times for one tree."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    built: list[str] = []

    @contextlib.contextmanager
    def counting_source(_devkit, tag):
        built.append(tag)
        yield tmp_path / "src"

    monkeypatch.setattr(up, "source_at_tag", counting_source)
    monkeypatch.setattr(up, "upgrade_one", lambda *_a, **_kw: 0)
    for name in ("carameli", "ibkr_trader"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli", "ibkr_trader")
    assert up.main(["--all", "--yes", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert built == ["v0.5.3"]


def test_a_dry_run_still_reports_a_refusal(tmp_path, monkeypatch):
    """A dry run is how a scheduled check asks whether the release *could* be adopted.
    Exiting 0 over a project parked on a task branch answers "all clear" for the one
    state that is not."""
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(up, "upgrade_one", lambda *_a, **_kw: 1)
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli")
    assert up.main(["--all", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 1


def test_a_dry_run_never_builds_a_source_worktree(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(up, "latest_tag", lambda _devkit: "v0.5.3")
    monkeypatch.setattr(
        up, "source_at_tag", lambda *_a: pytest.fail("a dry run pulled from somewhere")
    )
    monkeypatch.setattr(up, "upgrade_one", lambda *_a, **_kw: 0)
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / "DEVKIT_VERSION").write_text("v0.5.2\n", encoding="utf-8")
    ws = workspace(tmp_path, "carameli")
    assert up.main(["--all", "--workspace", str(ws), "--devkit", str(tmp_path)]) == 0
    assert "Dry run" in capsys.readouterr().out


@contextlib.contextmanager
def _no_worktree(_devkit, _tag):
    """Stands in for `source_at_tag` when the test does not care about the source."""
    yield None
