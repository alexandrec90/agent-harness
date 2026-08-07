"""Tests for the per-session task slug.

The contract this file holds down is a *handshake between two processes*: a
UserPromptSubmit hook writes, a PreToolUse hook in a different session-directory
reads, and the only thing they share is the session id. Nothing else in the workspace
connects those two events, so if the key or the location drift apart the symptom is
not an error — it is every box quietly reverting to `ws-<hex>` names, which is what
the whole file exists to stop.

Every helper is also required to be *silent on failure*: this runs before every
prompt, and the worst honest outcome of a failure here is an uglier branch name.
"""

from __future__ import annotations

import json

from support import task_slug, worktree


def test_a_slug_written_by_the_prompt_hook_is_read_back_by_the_guard(tmp_path):
    """The handshake, end to end and in one assertion."""
    assert task_slug.record(tmp_path, "sess-1", "add-voicemail-retry") is not None
    assert task_slug.read(tmp_path, "sess-1") == "add-voicemail-retry"


def test_slugs_live_beside_the_leases_not_in_a_repo(tmp_path):
    """Both ends can compute this path; neither can compute the other's git dir."""
    assert task_slug.slugs_dir(tmp_path).parent == worktree.boxes_root(tmp_path)


def test_an_unrecorded_session_reads_as_empty_not_an_error(tmp_path):
    assert task_slug.read(tmp_path, "never-seen") == ""
    assert task_slug.read(tmp_path, "") == ""


def test_a_later_prompt_overwrites_an_earlier_one(tmp_path):
    """A session whose first prompt is "hi" should still get the real task's name."""
    task_slug.record(tmp_path, "sess-1", "hi")
    task_slug.record(tmp_path, "sess-1", "rewrite-the-scheduler")
    assert task_slug.read(tmp_path, "sess-1") == "rewrite-the-scheduler"


def test_a_session_id_cannot_escape_the_slugs_directory(tmp_path):
    """The id reaches the filesystem as a name, so it is constrained, not trusted."""
    assert task_slug.safe_session("../../etc/passwd") == "etcpasswd"
    assert task_slug.safe_session("a/b\\c") == "abc"
    assert task_slug.safe_session("") == ""
    assert len(task_slug.safe_session("x" * 500)) == 64


def test_an_unusable_session_id_records_nothing_rather_than_writing_somewhere_odd(tmp_path):
    assert task_slug.record(tmp_path, "///", "topic") is None
    assert task_slug.record(tmp_path, "sess-1", "") is None


def test_prune_keeps_the_most_recent_and_drops_the_rest(tmp_path):
    """One file per prompt per session, and nothing deletes them on the normal path --
    on a workstation short of disk that is a directory that grows forever."""
    import os
    import time

    for n in range(6):
        path = task_slug.record(tmp_path, f"sess-{n}", f"topic-{n}")
        os.utime(path, (time.time() + n, time.time() + n))

    assert task_slug.prune(tmp_path, keep=2) == 4
    survivors = {p.name for p in task_slug.slugs_dir(tmp_path).iterdir()}
    assert survivors == {"sess-4", "sess-5"}


def test_prune_on_a_directory_that_does_not_exist_is_zero_not_a_crash(tmp_path):
    assert task_slug.prune(tmp_path) == 0


def _stdin(text: str):
    class _Fake:
        def read(self):
            return text

    return _Fake()


def _workspace(tmp_path):
    path = tmp_path / "alex-projects.code-workspace"
    path.write_text(json.dumps({"folders": [{"path": "carameli"}]}), encoding="utf-8")
    return path


def test_main_records_the_prompts_topic_for_the_session(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    payload = json.dumps(
        {"session_id": "sess-9", "prompt": "Please add a retry to the voicemail poller"}
    )
    monkeypatch.setattr("sys.stdin", _stdin(payload))

    assert task_slug.main(["--workspace", str(workspace)]) == 0

    # `slug_from_prompt` strips the filler, so the name is what the task is *about*.
    recorded = task_slug.read(tmp_path, "sess-9")
    assert "voicemail" in recorded
    assert "please" not in recorded


def test_main_is_silent_without_a_workspace_file(tmp_path, monkeypatch):
    """A CI runner, a fresh clone, anyone else's machine: there is no box tier."""
    monkeypatch.setattr("sys.stdin", _stdin(json.dumps({"session_id": "s", "prompt": "x"})))
    assert task_slug.main(["--workspace", str(tmp_path / "nope.code-workspace")]) == 0


def test_main_never_fails_a_prompt_on_malformed_input(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    for raw in ("", "not json", "[]", "null"):
        monkeypatch.setattr("sys.stdin", _stdin(raw))
        assert task_slug.main(["--workspace", str(workspace)]) == 0
