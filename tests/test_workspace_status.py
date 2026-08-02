"""Tests for scripts/workspace-status.py (the SessionStart status line).

The properties that matter are all about *not* being annoying: silent when
healthy, silent when it cannot tell, and never able to fail a session start. A
status line that cries wolf is removed within a week, and then the thing it was
watching goes unwatched again.
"""

import json

from support import load_script, sweep

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
