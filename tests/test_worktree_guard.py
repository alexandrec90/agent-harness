"""Tests for the PreToolUse hook that spawns a box instead of refusing an edit.

The decision half is what matters and it is pure: `redirect_decision` gets a path, a
cwd, and the registry, and says whether this edit needs its own box. Everything it
returns None for is a call some other part of the harness already owns, so each of
those is a named test — a hook that fires on the ordinary project session would
double up with `branch-on-write.py` and make every edit cost a worktree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import load_script

guard = load_script("scripts/worktree-guard.py")

PROJECTS = ["carameli", "carameli-b", "ibkr_trader", "apt-finder", "apt-finder-b", "devkit"]


@pytest.fixture
def root(tmp_path):
    """A workspace root with the checkouts on disk, resolved as the hook resolves them."""
    base = tmp_path.resolve()
    for name in PROJECTS:
        (base / name).mkdir()
    (base / ".worktrees" / "carameli--x-0806").mkdir(parents=True)
    return base


def payload(tool: str = "Edit", path: str = "", cwd: str = "", session: str = "s1") -> str:
    return json.dumps(
        {
            "tool_name": tool,
            "tool_input": {"file_path": path},
            "cwd": cwd,
            "session_id": session,
        }
    )


# --- reading the hook payload -----------------------------------------------


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null"])
def test_malformed_payloads_parse_to_none(raw):
    assert guard.parse_hook_input(raw) is None


@pytest.mark.parametrize("key", ["file_path", "filePath", "path", "notebook_path", "notebookPath"])
def test_edited_path_reads_every_spelling_the_tools_use(key):
    """Edit/Write say file_path, apply_patch says path, NotebookEdit says notebook_path.
    Missing one means the hook silently allows that tool through."""
    assert guard.edited_path({"tool_input": {key: "/ws/carameli/a.py"}}) == "/ws/carameli/a.py"


def test_edited_path_tolerates_camel_case_tool_input():
    assert guard.edited_path({"toolInput": {"file_path": "/ws/carameli/a.py"}})


def test_edited_path_is_empty_when_there_is_none():
    assert guard.edited_path({"tool_input": {}}) == ""
    assert guard.edited_path({"tool_input": "nonsense"}) == ""


# --- who owns a path --------------------------------------------------------


def test_owning_project_finds_the_checkout_containing_the_file(root):
    assert guard.owning_project(root / "carameli" / "app" / "main.py", root, PROJECTS) == "carameli"


def test_owning_project_prefers_the_longest_match(root):
    """`apt-finder` is a prefix of `apt-finder-b`, and they are separate checkouts of
    one repo -- routing an edit to the wrong one is worse than not routing it."""
    target = root / "apt-finder-b" / "src" / "x.py"
    assert guard.owning_project(target, root, PROJECTS) == "apt-finder-b"


def test_owning_project_is_empty_outside_every_checkout(root):
    assert guard.owning_project(root / "notes.md", root, PROJECTS) == ""


# --- the decision -----------------------------------------------------------


def test_an_edit_from_the_workspace_root_gets_its_own_box(root):
    """The case the hook exists for: today this write lands on carameli's home branch
    with no task branch under it, and the next sweep reports it as `needs-branch`."""
    decision = guard.redirect_decision(
        str(root / "carameli" / "app" / "main.py"), str(root), root, PROJECTS
    )
    assert decision == ("carameli", str(Path("app/main.py")))


def test_an_edit_inside_its_own_checkout_is_left_alone(root):
    """The ordinary project session. `branch-on-write.py` owns it and does it better --
    it can tell a read-only turn from new work, which this hook cannot."""
    assert (
        guard.redirect_decision(
            str(root / "carameli" / "app" / "main.py"), str(root / "carameli"), root, PROJECTS
        )
        is None
    )


def test_an_edit_from_a_subdirectory_of_its_checkout_is_left_alone(root):
    assert (
        guard.redirect_decision(
            str(root / "carameli" / "app" / "main.py"),
            str(root / "carameli" / "app"),
            root,
            PROJECTS,
        )
        is None
    )


def test_an_edit_already_inside_a_box_is_left_alone(root):
    """Otherwise the hook would spawn a box for every edit made in the box it spawned."""
    target = root / ".worktrees" / "carameli--x-0806" / "app" / "main.py"
    assert guard.redirect_decision(str(target), str(root), root, PROJECTS) is None


def test_an_edit_outside_every_checkout_is_left_alone(root):
    """A scratch note beside the projects, the multi-root workspace file itself, the
    lease file -- none of them belongs to a repo, so none of them needs a box."""
    assert guard.redirect_decision(str(root / "notes.md"), str(root), root, PROJECTS) is None


def test_a_relative_path_is_resolved_against_the_session_cwd(root):
    decision = guard.redirect_decision("carameli/app/main.py", str(root), root, PROJECTS)
    assert decision is not None and decision[0] == "carameli"


def test_a_cross_checkout_edit_between_two_projects_is_redirected(root):
    """A devkit session editing a sibling checkout is the same mistake as a
    workspace-root one, which is why devkit wires this hook on itself."""
    decision = guard.redirect_decision(
        str(root / "carameli" / "a.py"), str(root / "devkit"), root, PROJECTS
    )
    assert decision is not None and decision[0] == "carameli"


def test_an_edit_between_a_worktree_pair_is_redirected(root):
    """`carameli` and `carameli-b` are separate checkouts on separate branches. An edit
    from one into the other lands on the other's home branch, exactly like any other
    cross-checkout write."""
    decision = guard.redirect_decision(
        str(root / "carameli-b" / "a.py"), str(root / "carameli"), root, PROJECTS
    )
    assert decision is not None and decision[0] == "carameli-b"


def test_no_path_means_no_decision(root):
    assert guard.redirect_decision("", str(root), root, PROJECTS) is None


# --- what the agent reads ---------------------------------------------------


def test_the_deny_message_leads_with_the_path_to_use():
    message = guard.deny_message(
        "carameli",
        "app/main.py",
        "/ws/.worktrees/carameli--ws-abc-0806",
        "carameli--ws-abc-0806",
        [],
    )
    assert "app/main.py" in message
    assert ".worktrees/carameli--ws-abc-0806" in message.replace("\\", "/")


def test_the_deny_message_names_the_way_out():
    """A block that does not say how to finish is a dead end, which is the thing this
    hook is supposed to not be."""
    message = guard.deny_message("carameli", "a.py", "/ws/.worktrees/b", "b", [])
    assert "/ship" in message
    assert "reap b" in message


def test_a_failed_spawn_still_blocks_and_says_how_to_do_it_by_hand():
    """Allowing the edit through would land it on the home branch -- the failure mode
    the hook exists to prevent, so a broken spawn must not fall back to it."""
    message = guard.failure_message("carameli", "a.py", "git worktree add: boom")
    assert "boom" in message
    assert "worktree.py new carameli" in message


def test_session_slug_is_stable_for_one_session():
    assert guard.session_slug("abcdef1234") == guard.session_slug("abcdef1234")
    assert guard.session_slug("abcdef1234") != guard.session_slug("zzzzzz9999")


def test_session_slug_survives_a_missing_session_id():
    assert guard.session_slug("") == "ws"


# --- the shell --------------------------------------------------------------


def test_absent_workspace_file_allows_everything(tmp_path, monkeypatch):
    """CI, a fresh clone, anyone else's machine: no multi-root registry means no
    cross-checkout edit is possible, so silence is correct rather than an error."""
    monkeypatch.setattr("sys.stdin", _stdin(payload(path="/anything")))
    assert guard.main(["--workspace", str(tmp_path / "nope.code-workspace")]) == guard.EXIT_ALLOW


def test_a_non_mutating_tool_is_allowed(tmp_path, root, monkeypatch):
    workspace = _workspace(root)
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(tool="Read", path=str(root / "carameli" / "a.py")))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_an_in_checkout_edit_is_allowed(root, monkeypatch):
    workspace = _workspace(root)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root / "carameli"))),
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_ALLOW


def test_a_session_reuses_the_box_it_already_has(root, monkeypatch, capsys):
    """One box per (session, project). Without this the hook cuts a worktree per edit,
    which would be worse than the problem it solves."""
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    assert guard.main(["--workspace", str(workspace)]) == guard.EXIT_BLOCK
    err = capsys.readouterr().err
    assert "carameli--ws-s1-0806" in err
    # "a box has been spawned" on the fortieth edit is simply untrue, and a message
    # that misdescribes what happened is how an agent concludes it is looping.
    assert "already has a box" in err
    assert "has been spawned" not in err


def test_the_reason_goes_to_stderr_because_stdout_is_not_surfaced(root, monkeypatch, capsys):
    workspace = _workspace(root)
    _lease(root, "carameli--ws-s1-0806", project="carameli", session="s1")
    monkeypatch.setattr(
        "sys.stdin", _stdin(payload(path=str(root / "carameli" / "a.py"), cwd=str(root)))
    )
    guard.main(["--workspace", str(workspace)])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


# --- helpers ----------------------------------------------------------------


class _stdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


def _workspace(root):
    path = root / "alex-projects.code-workspace"
    path.write_text(
        json.dumps({"folders": [{"path": name} for name in PROJECTS]}), encoding="utf-8"
    )
    return path


def _lease(root, name, **fields):
    """Record a live box, directory and all -- `live_boxes` filters on the directory."""
    (root / ".worktrees" / name).mkdir(parents=True, exist_ok=True)
    (root / ".worktrees" / "leases.json").write_text(
        json.dumps({"boxes": {name: {"branch": f"claude/{name.split('--')[1]}", **fields}}}),
        encoding="utf-8",
    )
