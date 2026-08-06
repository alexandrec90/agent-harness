"""Tests for scripts/workspace-status.py (the SessionStart status line).

The properties that matter are all about *not* being annoying: silent when
healthy, silent when it cannot tell, and never able to fail a session start. A
status line that cries wolf is removed within a week, and then the thing it was
watching goes unwatched again.
"""

import json

from support import LIVE_WORKSPACE, REPO_ROOT, load_script, needs_live_workspace, sweep

ws = load_script("scripts/workspace-status.py")


def result(name: str, verdict: str) -> sweep.Result:
    return sweep.Result(sweep.State(name=name), verdict, "reason", [])


# --- silence when there is nothing to say ------------------------------------


def test_a_clean_workspace_says_nothing():
    results = [result("carameli", sweep.CLEAN), result("devkit", sweep.SKIPPED)]
    assert ws.render(results, {}, "v0.5.3") == ""


def test_stranded_work_names_the_checkouts():
    """ "3 checkouts need action" makes you run something else to find out which."""
    results = [result("carameli", sweep.READY), result("devkit", sweep.CLEAN)]
    line = ws.render(results, {}, "v0.5.3")
    assert "carameli (ready)" in line
    assert "devkit" not in line


def test_behind_projects_are_named_with_their_version():
    line = ws.render([], {"carameli": "v0.5.2"}, "v0.5.3")
    assert "devkit v0.5.3 available" in line
    assert "carameli on v0.5.2" in line


def test_both_halves_appear_together():
    line = ws.render([result("devkit", sweep.READY)], {"carameli": "v0.5.2"}, "v0.5.3")
    assert "stranded work" in line
    assert "devkit v0.5.3 available" in line
    assert line.count("[workspace]") == 2


# --- "cannot tell" is not "behind" -------------------------------------------


def test_a_project_with_no_recorded_tag_is_not_reported_behind(tmp_path):
    """An unrecorded tag means the pull predates the receipt carrying one. Guessing
    would put every un-upgraded project in this line forever."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "DEVKIT_FILES.json").write_text(
        json.dumps({"version": 1, "files": {}}), encoding="utf-8"
    )
    assert ws.projects_behind(tmp_path, ["proj"], "v0.5.3") == {}


def test_a_project_on_the_latest_tag_is_not_behind(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "DEVKIT_FILES.json").write_text(
        json.dumps({"devkit_tag": "v0.5.3", "files": {}}), encoding="utf-8"
    )
    assert ws.projects_behind(tmp_path, ["proj"], "v0.5.3") == {}


def test_a_project_on_an_older_tag_is_behind(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "DEVKIT_FILES.json").write_text(
        json.dumps({"devkit_tag": "v0.5.2", "files": {}}), encoding="utf-8"
    )
    assert ws.projects_behind(tmp_path, ["proj"], "v0.5.3") == {"proj": "v0.5.2"}


def test_a_missing_or_malformed_receipt_is_skipped(tmp_path):
    (tmp_path / "none").mkdir()
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "DEVKIT_FILES.json").write_text("{not json", encoding="utf-8")
    assert ws.projects_behind(tmp_path, ["none", "bad", "absent"], "v0.5.3") == {}


# --- version ordering --------------------------------------------------------


def test_tags_sort_numerically_not_lexically():
    """v0.5.10 is newer than v0.5.9; string ordering says otherwise."""
    assert ws._version_key("v0.5.10") > ws._version_key("v0.5.9")
    assert ws._version_key("v0.10.0") > ws._version_key("v0.9.9")


def test_a_non_version_tag_sorts_lowest():
    assert ws._version_key("nightly") < ws._version_key("v0.0.1")


def test_the_latest_tag_is_read_from_loose_refs(tmp_path):
    tags = tmp_path / ".git" / "refs" / "tags"
    tags.mkdir(parents=True)
    for name in ("v0.5.2", "v0.5.10", "v0.5.9"):
        (tags / name).write_text("deadbeef\n", encoding="utf-8")
    assert ws.latest_devkit_tag(tmp_path) == "v0.5.10"


def test_the_latest_tag_is_read_from_packed_refs(tmp_path):
    (tmp_path / ".git").mkdir(parents=True)
    (tmp_path / ".git" / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "aaa refs/tags/v0.5.2\n"
        "bbb refs/tags/v0.5.3\n"
        "ccc refs/heads/main\n",
        encoding="utf-8",
    )
    assert ws.latest_devkit_tag(tmp_path) == "v0.5.3"


def test_a_repo_with_no_tags_reports_nothing(tmp_path):
    assert ws.latest_devkit_tag(tmp_path) == ""


# --- it must never break a session -------------------------------------------


def test_an_absent_workspace_is_silent_and_successful(tmp_path, monkeypatch, capsys):
    """The registry is workstation-local: on CI or a fresh clone there is simply
    nothing to report, which is not an error."""
    monkeypatch.setattr(ws, "DEFAULT_WORKSPACE", tmp_path / "nope.code-workspace")
    assert ws.main([]) == 0
    assert capsys.readouterr().out == ""


def test_a_failure_anywhere_still_exits_zero(tmp_path, monkeypatch):
    """A status line that can fail a session start gets removed the first time it
    is wrong -- and then nothing is watching again."""
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"folders": [{"path": "proj"}]}', encoding="utf-8")
    monkeypatch.setattr(ws, "DEFAULT_WORKSPACE", workspace)
    monkeypatch.setattr(ws.sweep, "sweep", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert ws.main([]) == 0


# --- the branch-policy half --------------------------------------------------
# The global hooks are a copy of scripts/git_policy.py, so they go stale with no
# symptom: the hooks still fire, they just enforce an older policy. Nothing else
# in the workspace would ever mention it.

installer = load_script("scripts/install-git-policy.py")


def installed(target, ref="v0.5.3"):
    """A realistic install, made through the installer's own code path."""
    installer.install(REPO_ROOT, target, ref)
    return target


def test_a_current_install_says_nothing(tmp_path):
    target = installed(tmp_path / "hooks")
    assert ws.policy_line(REPO_ROOT, target, latest="v0.5.3") == ""


def test_a_modified_runtime_is_named(tmp_path):
    target = installed(tmp_path / "hooks")
    (target / "devkit_git_policy.py").write_text("# tampered\n", encoding="utf-8")

    line = ws.policy_line(REPO_ROOT, target, latest="v0.5.3")
    assert "branch policy" in line
    assert "devkit_git_policy.py" in line
    assert "install-git-policy.py --yes" in line


def test_an_install_from_an_older_release_is_reported_as_behind(tmp_path):
    target = installed(tmp_path / "hooks")
    line = ws.policy_line(REPO_ROOT, target, latest="v0.6.0")
    assert "installed from v0.5.3" in line
    assert "v0.6.0 available" in line


def test_a_runtime_with_no_receipt_is_reported_as_unidentifiable(tmp_path):
    """The state this machine was in: installed before receipts existed."""
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    assert "installed.json" in ws.policy_line(REPO_ROOT, target, latest="v0.5.3")


def test_an_absent_install_is_silence_not_a_warning(tmp_path):
    """A fresh clone, CI, anyone else's machine -- there is nothing to say."""
    assert ws.policy_line(REPO_ROOT, tmp_path / "never-installed", latest="v0.5.3") == ""


def test_an_unusable_source_tree_is_silence_not_an_exception(tmp_path):
    """This runs at session start, so it may never be the reason one fails -- and a
    checkout with no installer to load is exactly the shape that would raise."""
    target = installed(tmp_path / "hooks")
    assert ws.policy_line(tmp_path / "not-a-checkout", target, latest="v0.5.3") == ""


def test_an_unknown_latest_tag_does_not_invent_a_warning(tmp_path):
    target = installed(tmp_path / "hooks")
    assert ws.policy_line(REPO_ROOT, target, latest="") == ""


def test_the_policy_half_joins_the_others(tmp_path):
    line = ws.render([result("devkit", sweep.READY)], {}, "v0.5.3", "branch policy: x")
    assert "stranded work" in line
    assert "branch policy: x" in line
    assert line.count("[workspace]") == 2


def test_the_policy_half_alone_still_prints():
    assert ws.render([], {}, "", "branch policy: x") == "[workspace] branch policy: x"


# --- adoption shape ----------------------------------------------------------
# The half that answers "is this checkout a devkit project at all", which nothing asked
# before: `upgrade-project.py` skips an unadopted checkout with a reason and moves on,
# and that skip renders indistinguishably from a routine one.


def project(root, name: str, *, version=True, precommit=True):
    """A checkout carrying whichever adoption markers are asked for."""
    path = root / name
    path.mkdir(parents=True)
    if version:
        (path / "DEVKIT_VERSION").write_text("abc1234\n", encoding="utf-8")
    if precommit:
        (path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    return path


def test_a_fully_adopted_workspace_says_nothing(tmp_path):
    project(tmp_path, "carameli")
    project(tmp_path, "apt-finder")
    assert ws.adoption_line(tmp_path, ["carameli", "apt-finder"]) == ""


def test_a_checkout_that_never_vendored_devkit_is_named(tmp_path):
    """ibkr_trader's actual state: registered in the workspace, outside the harness."""
    project(tmp_path, "ibkr_trader", version=False, precommit=False)
    line = ws.adoption_line(tmp_path, ["ibkr_trader"])
    assert "ibkr_trader (never vendored devkit)" in line
    assert "sync-devkit.py --pull" in line, "a standing report has to say what fixes it"


def test_a_missing_pre_commit_gate_is_named_on_an_adopted_checkout(tmp_path):
    """data-lake's actual state. Every vendored hook present and none of them running:
    no error, no red build, the checks simply never fire."""
    project(tmp_path, "data-lake", precommit=False)
    assert "data-lake (no pre-commit gate)" in ws.adoption_line(tmp_path, ["data-lake"])


def test_only_the_first_missing_marker_is_reported(tmp_path):
    """ "never vendored devkit" already implies the gate is missing. Reporting both makes
    the reader triage a list where there is one fix."""
    project(tmp_path, "ibkr_trader", version=False, precommit=False)
    assert ws.adoption_line(tmp_path, ["ibkr_trader"]).count("ibkr_trader") == 1


def test_devkit_itself_is_not_an_adopter(tmp_path):
    """It is where these files come from, and has no DEVKIT_VERSION by design. Without
    the exemption this line names devkit every session and is ignored by week two."""
    source = project(tmp_path, "devkit", version=False)
    assert ws.adoption_line(tmp_path, ["devkit"], source=source) == ""


def test_a_missing_directory_is_silence_not_a_fault(tmp_path):
    """The registry is hand-edited and can name a checkout nobody has cloned yet."""
    assert ws.adoption_line(tmp_path, ["not-cloned-here"]) == ""


def test_the_adoption_half_joins_the_others(tmp_path):
    line = ws.render([result("devkit", sweep.READY)], {}, "v0.5.3", "", "not devkit projects: x")
    assert "stranded work" in line
    assert "not devkit projects: x" in line
    assert line.count("[workspace]") == 2


def test_the_adoption_half_alone_still_prints():
    assert ws.render([], {}, "", "", "not devkit projects: x") == (
        "[workspace] not devkit projects: x"
    )


# --- the architectural check ------------------------------------------------


# Checkouts knowingly outside the harness, each carrying the reason it still is. This is
# a **ratchet, not an allowlist**: the test below fails both when an unlisted checkout
# drifts out of shape AND when a listed one is fixed without being removed from here.
# That second half is what stops this becoming the same silent permanent skip it exists
# to replace.
UNADOPTED_EXCEPTIONS = {
    "ibkr_trader": "never onboarded -- predates adoption; needs a one-time vendoring pass",
    "ibkr_trader-b": "worktree of ibkr_trader; onboarding it is the same change",
    "data-lake": "vendored, but ships no .pre-commit-config.yaml, so no gate ever runs",
}


@needs_live_workspace
def test_every_registered_checkout_is_a_devkit_project():
    """The shape check devkit did not have. Being registered in the workspace means
    devkit's tooling manages you -- and every one of those tools (the vendored hooks,
    the commit gate, `lint-fix.py`, `upgrade-project.py`) silently does nothing for a
    checkout that never adopted. Nothing goes red; the work simply is not done."""
    names = sweep.parse_workspace(LIVE_WORKSPACE.read_text(encoding="utf-8"))
    faults = dict(ws.adoption_faults(LIVE_WORKSPACE.parent, names))

    unexpected = sorted(set(faults) - set(UNADOPTED_EXCEPTIONS))
    assert not unexpected, (
        f"{unexpected} are registered in the workspace but are not devkit projects: "
        f"{ {n: faults[n] for n in unexpected} }. Onboard them, or add each to "
        "UNADOPTED_EXCEPTIONS with the reason it is deliberate."
    )

    fixed = sorted(set(UNADOPTED_EXCEPTIONS) - set(faults))
    assert not fixed, (
        f"{fixed} are now properly adopted -- delete them from UNADOPTED_EXCEPTIONS. "
        "A stale exception is how a permanent gap goes quiet again."
    )
