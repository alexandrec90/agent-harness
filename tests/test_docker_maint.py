"""Tests for `scripts/docker-maint.py`'s stack modes and its argument plumbing.

The daemon modes (`restart-engine`, `fix`, `prune`) are deliberately not exercised
here: they kill Docker Desktop and compact a WSL2 VHDX, so there is nothing to assert
that does not involve doing it. What IS tested is everything the `up`/`down` hoist
added, plus the two invariants that would be expensive to get wrong:

  - arguments reach the delegate (before this, `find_delegate`'s spawn passed none, so
    a hoisted "Docker: Start Stack" would have dropped `--build` in every project that
    ships its own `docker-up.py` — a stack that comes up healthy running a stale image);
  - `down` never grows `-v`, because this runs from a one-click task over a project
    picker and named volumes hold real dev databases.
"""

from __future__ import annotations

import sys

import pytest
from support import load_script

docker_maint = load_script("scripts/docker-maint.py")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """A cwd that looks like a project with a compose stack."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def commands(monkeypatch):
    """Capture what `run()` would have executed instead of executing it."""
    seen: list[list[str]] = []
    monkeypatch.setattr(docker_maint, "run", lambda cmd, **kw: seen.append(list(cmd)) or 0)
    return seen


# --- argument parsing -------------------------------------------------------


def test_split_args_extracts_the_mode_and_forwards_the_rest():
    assert docker_maint.split_args(["up", "--build"]) == ("up", ["--build"], False)


def test_split_args_consumes_generic_from_anywhere():
    """It is devkit's own flag, not the delegate's, so it must not be forwarded."""
    mode, forwarded, generic_only = docker_maint.split_args(["--generic", "down", "--timeout", "5"])
    assert (mode, forwarded, generic_only) == ("down", ["--timeout", "5"], True)


def test_split_args_rejects_an_unknown_mode():
    assert docker_maint.split_args(["upp"])[0] is None
    assert docker_maint.split_args([])[0] is None


def test_an_unknown_mode_is_a_usage_error(capsys):
    assert docker_maint.main(["upp"]) == 2
    assert "usage: docker-maint.py" in capsys.readouterr().err


def test_split_args_is_pure():
    original = ["up", "--build"]
    docker_maint.split_args(original)
    assert original == ["up", "--build"]


# --- delegation -------------------------------------------------------------


def test_a_projects_own_script_wins_and_receives_the_arguments(stack, monkeypatch):
    """The regression this hoist would otherwise have shipped.

    carameli's `docker-up.py --build` became a workspace task; if the fixed `--build`
    stops arriving, the task still exits 0 and still brings a stack up — just not the
    one the user's edits are in.
    """
    script = stack / "scripts" / "docker-up.py"
    script.parent.mkdir()
    script.write_text("")
    spawned: dict = {}

    def fake_run(cmd, **kwargs):
        spawned["cmd"] = list(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(docker_maint.subprocess, "run", fake_run)

    assert docker_maint.main(["up", "--build"]) == 0
    assert spawned["cmd"] == [sys.executable, str(script), "--build"]


def test_generic_skips_a_delegate_that_exists(stack, commands):
    (stack / "scripts").mkdir()
    (stack / "scripts" / "docker-down.py").write_text("")
    docker_maint.main(["down", "--generic"])
    assert commands == [["docker", "compose", "down"]]


def test_the_delegates_exit_code_is_the_answer(stack, monkeypatch):
    script = stack / "scripts" / "docker-down.py"
    script.parent.mkdir()
    script.write_text("")
    monkeypatch.setattr(
        docker_maint.subprocess, "run", lambda cmd, **kw: type("R", (), {"returncode": 3})()
    )
    assert docker_maint.main(["down"]) == 3


def test_every_mode_has_a_delegate_entry_and_a_generic_fallback():
    """`find_delegate` indexes DELEGATES by mode and `GENERIC` is indexed the same way.

    A mode listed in MODES but absent from either dict is a KeyError at click time,
    which reads as a crash rather than as the missing wiring it is.
    """
    assert set(docker_maint.MODES) == set(docker_maint.DELEGATES) == set(docker_maint.GENERIC)


# --- the generic stack fallbacks --------------------------------------------


def test_generic_up_forwards_what_it_was_given(stack, commands):
    docker_maint.main(["up", "--build"])
    assert commands == [["docker", "compose", "up", "-d", "--build"]]


def test_generic_down_stops_containers_only(stack, commands):
    docker_maint.main(["down"])
    assert commands == [["docker", "compose", "down"]]


def test_generic_down_never_destroys_volumes(stack, commands):
    """Not a style preference. A named volume here is a dev database, and this runs
    from a picker where the wrong entry is one keystroke away."""
    docker_maint.main(["down"])
    assert not ({"-v", "--volumes"} & set(commands[0]))


def test_a_repo_with_no_compose_file_is_named_rather_than_handed_to_compose(
    tmp_path, monkeypatch, capsys
):
    """devkit and a `bare` preset have no stack, and the task is one picker over every
    checkout — so landing on one is ordinary. Exit 2 (a usage answer), never 0."""
    monkeypatch.chdir(tmp_path)
    calls: list = []
    monkeypatch.setattr(docker_maint, "run", lambda cmd, **kw: calls.append(cmd) or 0)

    for mode in ("up", "down"):
        assert docker_maint.main([mode]) == 2
    assert calls == [], "compose was invoked for a repo that has no compose file"
    assert "no compose file" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name", ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
)
def test_compose_file_recognises_every_supported_spelling(tmp_path, name):
    (tmp_path / name).write_text("")
    assert docker_maint.compose_file(tmp_path) == tmp_path / name


def test_compose_file_returns_none_when_there_is_no_stack(tmp_path):
    assert docker_maint.compose_file(tmp_path) is None
