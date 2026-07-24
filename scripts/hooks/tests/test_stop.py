"""Unit tests for the portable Stop hook snapshot logic."""

import io
import sys

from conftest import load_module

hook = load_module("scripts/hooks/stop.py")


class _FakeStdin:
    """Minimal stdin stand-in exposing .buffer/.isatty()/.read() for _read_stdin."""

    def __init__(self, data: bytes, tty: bool = False):
        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self):
        return self.buffer.read().decode("utf-8", "surrogateescape")


def test_no_profile_is_noop(tmp_path):
    profile = tmp_path / "skills-profile.json"
    snapshot = tmp_path / "skills-profile.optimized.json"
    assert hook.save_snapshot(profile, snapshot) == 0
    assert not snapshot.exists()


def test_profile_present_saves_snapshot(tmp_path):
    profile = tmp_path / "skills-profile.json"
    snapshot = tmp_path / "skills-profile.optimized.json"
    profile.write_text('{"fix-tests": {"invocations": 3}}')

    assert hook.save_snapshot(profile, snapshot) == 0
    assert snapshot.exists()
    assert snapshot.read_text() == profile.read_text()


def test_snapshot_overwrites_previous(tmp_path):
    profile = tmp_path / "skills-profile.json"
    snapshot = tmp_path / "skills-profile.optimized.json"
    snapshot.write_text('{"stale": true}')
    profile.write_text('{"fresh": true}')

    assert hook.save_snapshot(profile, snapshot) == 0
    assert snapshot.read_text() == '{"fresh": true}'


def test_copy_failure_returns_one(tmp_path):
    profile = tmp_path / "skills-profile.json"
    profile.write_text("{}")
    # Destination directory does not exist -> shutil.copy2 raises OSError
    snapshot = tmp_path / "missing-dir" / "snap.json"

    assert hook.save_snapshot(profile, snapshot) == 1


def test_should_normalize_requires_opt_in():
    assert hook.should_normalize({"CARAMELI_NORMALIZE_KNOWN_FIXES_ON_STOP": "1"}) is True
    assert hook.should_normalize({"CARAMELI_NORMALIZE_KNOWN_FIXES_ON_STOP": "0"}) is False
    assert hook.should_normalize({}) is False


def test_skin_changed_detects_porcelain_lines():
    assert hook.skin_changed(" M frontend/src/skins/carameli/Tile.tsx\n") is True
    assert hook.skin_changed("") is False
    assert hook.skin_changed("\n  \n") is False


def test_finalize_targets_cover_state_driven_skills():
    skills = {skill for skill, _ in hook.FINALIZE_TARGETS}
    assert skills == {"audit-design-flaws", "make-tests", "make-frontend-tests", "refactor"}


def test_archive_targets_present_with_transcript():
    payload = '{"transcript_path": "/x/session.jsonl", "cwd": "/repo"}'
    assert hook.archive_targets_present(payload) is True


def test_archive_targets_present_without_transcript():
    assert hook.archive_targets_present('{"cwd": "/repo"}') is False
    assert hook.archive_targets_present('{"transcript_path": ""}') is False


def test_archive_targets_present_rejects_non_object_and_garbage():
    assert hook.archive_targets_present("[1, 2, 3]") is False
    assert hook.archive_targets_present("not json") is False
    assert hook.archive_targets_present("") is False


def test_read_stdin_decodes_utf8_payload(monkeypatch):
    payload = '{"transcript_path": "/x/sesión.jsonl"}'
    monkeypatch.setattr(sys, "stdin", _FakeStdin(payload.encode("utf-8")))
    assert hook._read_stdin() == payload


def test_read_stdin_survives_undecodable_byte(monkeypatch):
    # A lone 0x9d byte is undefined in cp1252 and invalid UTF-8. The reader must
    # not crash on it, and the byte must round-trip back out unchanged when the
    # string is re-encoded for the archive child. Regression for the stop-hook
    # UnicodeEncodeError: 'charmap' codec can't encode character '\udc9d'.
    raw = b'{"transcript_path": "/x/a\x9d.jsonl"}'
    monkeypatch.setattr(sys, "stdin", _FakeStdin(raw))
    result = hook._read_stdin()
    assert result.encode("utf-8", "surrogateescape") == raw


def test_read_stdin_empty_for_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b'{"x": 1}', tty=True))
    assert hook._read_stdin() == ""


# --- pre-stop verification --------------------------------------------------


def test_stop_hook_active_true_only_when_flagged():
    assert hook.stop_hook_active('{"stop_hook_active": true}') is True
    assert hook.stop_hook_active('{"stop_hook_active": false}') is False
    assert hook.stop_hook_active('{"cwd": "/repo"}') is False
    assert hook.stop_hook_active("not json") is False
    assert hook.stop_hook_active("[1, 2]") is False


def test_verify_enabled_opt_out():
    assert hook.verify_enabled({}) is True
    assert hook.verify_enabled({"CARAMELI_SKIP_STOP_VERIFY": "0"}) is True
    assert hook.verify_enabled({"CARAMELI_SKIP_STOP_VERIFY": "1"}) is False


def test_changed_paths_parses_status_and_renames():
    porcelain = " M app/main.py\n?? scripts/new.py\nR  app/old.py -> app/renamed.py\n\n"
    assert hook.changed_paths(porcelain) == [
        "app/main.py",
        "scripts/new.py",
        "app/renamed.py",
    ]


def test_changed_paths_empty():
    assert hook.changed_paths("") == []


def test_path_predicates():
    assert hook._is_py("app/x.py") and hook._is_py("app/y.pyi")
    assert not hook._is_py("README.md")
    assert hook._is_frontend("frontend/src/App.tsx")
    assert not hook._is_frontend("frontend/vite.config.ts")
    assert hook._is_reqs("requirements.txt")
    assert hook._is_reqs("requirements-dev.in")
    assert not hook._is_reqs("app/requirements_notes.md")
    assert hook._is_script("scripts/hooks/stop.py")
    assert not hook._is_script("scripts/notes.md")


def test_host_test_targets_app_change_runs_whole_unit_suite():
    # An app/ change can break tests anywhere -> whole unit suite.
    assert hook.host_test_targets(["app/main.py"]) == ["tests/unit"]
    assert hook.host_test_targets(["app/x.py", "tests/unit/t.py"]) == ["tests/unit"]


def test_host_test_targets_tests_only_runs_changed_files():
    assert hook.host_test_targets(["tests/unit/test_a.py", "tests/integration/test_b.py"]) == [
        "tests/integration/test_b.py",
        "tests/unit/test_a.py",
    ]


def test_host_test_targets_no_app_or_tests_is_empty():
    assert hook.host_test_targets(["scripts/hooks/stop.py", "README.md"]) == []


def test_select_checks_empty_diff_runs_nothing():
    assert hook.select_checks([]) == []


def test_select_checks_never_includes_db_tier():
    # The DB tier is handled by run_db_tests, not select_checks.
    assert hook.select_checks(["app/main.py"]) == [hook.CHECK_LINT]
    assert hook.CHECK_TESTS not in hook.select_checks(["tests/unit/t.py"])


def test_select_checks_scripts_run_host_tests():
    # A scripts/ change is verified on the host with no Docker.
    assert hook.select_checks(["scripts/hooks/stop.py"]) == [
        hook.CHECK_LINT,
        hook.CHECK_SCRIPT_TESTS,
    ]


def test_select_checks_reqs_adds_lock_markers():
    checks = hook.select_checks(["requirements.txt"])
    assert hook.CHECK_LOCKS in checks and hook.CHECK_TESTS not in checks


def test_select_checks_frontend_adds_vitest():
    assert hook.select_checks(["frontend/src/App.tsx"]) == [hook.CHECK_LINT, hook.CHECK_FRONTEND]


def test_select_checks_non_relevant_change_runs_lint_only():
    # A docs edit still runs lint (lint-all --changed self-scopes to a no-op),
    # but never script-tests / locks / frontend.
    assert hook.select_checks(["README.md"]) == [hook.CHECK_LINT]


def test_parse_host_port_variants():
    assert hook._parse_host_port("0.0.0.0:5432\n") == "5432"
    assert hook._parse_host_port("[::]:5432") == "5432"
    assert hook._parse_host_port("127.0.0.1:5433") == "5433"
    # Multi-line (IPv4 + IPv6): last non-empty line wins, still a valid port.
    assert hook._parse_host_port("0.0.0.0:5432\n[::]:5432\n") == "5432"
    assert hook._parse_host_port("") is None
    assert hook._parse_host_port("garbage") is None


def test_run_checks_skips_missing_tool(monkeypatch):
    monkeypatch.setattr(hook, "_command_for", lambda name: None)
    assert hook.run_checks([hook.CHECK_FRONTEND]) == []


def test_run_checks_collects_failures(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "_command_for", lambda name: (["true"], hook.REPO_ROOT, None))
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *a, **k: sp.CompletedProcess([], 1, "boom\n", "bad line\n"),
    )
    failures = hook.run_checks([hook.CHECK_LINT])
    assert len(failures) == 1
    assert failures[0][0] == hook.CHECK_LINT
    assert "bad line" in failures[0][2]


def test_run_checks_oserror_is_skip_not_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("no such tool")

    monkeypatch.setattr(hook, "_command_for", lambda name: (["nope"], hook.REPO_ROOT, None))
    monkeypatch.setattr(hook.subprocess, "run", boom)
    assert hook.run_checks([hook.CHECK_LINT]) == []


def test_verify_skips_when_loop_active(monkeypatch):
    monkeypatch.setattr(hook, "run_checks", lambda names: [("lint", None, "x")])
    assert hook.verify('{"stop_hook_active": true}', {}) == 0


def test_verify_skips_when_opted_out(monkeypatch):
    monkeypatch.setattr(hook, "run_checks", lambda names: [("lint", None, "x")])
    assert hook.verify("{}", {"CARAMELI_SKIP_STOP_VERIFY": "1"}) == 0


def test_verify_returns_two_on_failure(monkeypatch):
    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: " M app/main.py\n")
    monkeypatch.setattr(hook, "run_db_tests", lambda paths, env: [])
    monkeypatch.setattr(hook, "run_checks", lambda names: [("lint", "logs/lint-errors.log", "")])
    assert hook.verify("{}", {}) == 2


def test_verify_returns_two_when_db_tests_fail(monkeypatch):
    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: " M app/main.py\n")
    monkeypatch.setattr(hook, "run_checks", lambda names: [])
    monkeypatch.setattr(hook, "run_db_tests", lambda paths, env: [("tests", None, "F app/x")])
    assert hook.verify("{}", {}) == 2


def test_verify_returns_zero_when_clean(monkeypatch):
    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: " M app/main.py\n")
    monkeypatch.setattr(hook, "run_checks", lambda names: [])
    monkeypatch.setattr(hook, "run_db_tests", lambda paths, env: [])
    assert hook.verify("{}", {}) == 0


# --- Tier 2b: host pytest against db+redis (+ opt-in autostart) -------------


def test_autostart_enabled_opt_in():
    assert hook.autostart_enabled({}) is False
    assert hook.autostart_enabled({"CARAMELI_STOP_TESTS_AUTOSTART": "0"}) is False
    assert hook.autostart_enabled({"CARAMELI_STOP_TESTS_AUTOSTART": "1"}) is True


def test_services_to_stop_only_newly_started():
    assert hook.services_to_stop({"redis"}, {"redis", "db"}) == ["db"]
    assert hook.services_to_stop({"db", "redis"}, {"db", "redis"}) == []


def test_db_redis_running_needs_both(monkeypatch):
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: {"db", "redis", "app"})
    assert hook.db_redis_running() is True
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: {"db"})
    assert hook.db_redis_running() is False


def test_host_db_env_builds_urls_from_ports(monkeypatch):
    monkeypatch.setattr(
        hook, "_compose_host_port", lambda svc, port, *a: "5599" if svc == "db" else "6699"
    )
    env = hook.host_db_env()
    assert (
        env["DATABASE_URL"]
        == "postgresql+asyncpg://carameli:carameli_local_dev@localhost:5599/carameli"
    )
    assert env["REDIS_URL"] == "redis://localhost:6699"


def test_host_db_env_none_when_port_unresolved(monkeypatch):
    monkeypatch.setattr(hook, "_compose_host_port", lambda svc, port, *a: None)
    assert hook.host_db_env() is None


def test_run_db_tests_no_targets_never_touches_docker(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not run")

    monkeypatch.setattr(hook, "db_redis_running", boom)
    assert hook.run_db_tests(["scripts/x.py", "README.md"], {}) == []


def test_run_db_tests_runs_when_db_up_no_autostart(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    up = {"called": False}
    monkeypatch.setattr(
        hook, "_compose_up_db_redis", lambda *a, **k: up.__setitem__("called", True)
    )
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 0))

    assert hook.run_db_tests(["app/main.py"], {}) == []
    assert up["called"] is False  # already up -> no autostart
    assert stopped["svc"] == []  # started nothing -> stop nothing


def test_run_db_tests_reports_pytest_failure(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    monkeypatch.setattr(hook, "_compose_stop", lambda *a, **k: None)
    monkeypatch.setattr(
        hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 1, "F tests/unit/x\n", "")
    )
    failures = hook.run_db_tests(["tests/unit/x.py"], {})
    assert failures and failures[0][0] == hook.CHECK_TESTS
    assert "tests/unit/x" in failures[0][2]


def test_run_db_tests_skips_when_down_and_not_opted_in(monkeypatch):
    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    up = {"called": False}
    monkeypatch.setattr(
        hook, "_compose_up_db_redis", lambda *a, **k: up.__setitem__("called", True)
    )
    assert hook.run_db_tests(["app/main.py"], {}) == []
    assert up["called"] is False  # no opt-in -> never autostarts


def test_run_db_tests_autostarts_and_stops_only_started(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    running = iter([{"redis"}, {"redis", "db"}])  # before up, after up
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: next(running))
    monkeypatch.setattr(hook, "_compose_up_db_redis", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 0))
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))

    assert hook.run_db_tests(["app/main.py"], {"CARAMELI_STOP_TESTS_AUTOSTART": "1"}) == []
    assert stopped["svc"] == ["db"]  # only db was newly started (redis was already up)


def test_run_db_tests_up_failure_skips_and_stops_nothing(monkeypatch):
    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: set())
    monkeypatch.setattr(hook, "_compose_up_db_redis", lambda *a, **k: False)  # daemon down
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))

    assert hook.run_db_tests(["app/main.py"], {"CARAMELI_STOP_TESTS_AUTOSTART": "1"}) == []
    assert "svc" not in stopped  # returns before try/finally -> _compose_stop never called


# --- verify_python: run the checks in the venv, not the launching interpreter ---
# The hooks are wired as `python3 <script>`; on Windows that resolves to the
# Microsoft Store shim, which has no pytest and none of the project's linters, so
# every check failed on tooling rather than on the code.


def test_verify_python_prefers_windows_venv(tmp_path):
    py = tmp_path / ".venv/Scripts/python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")

    assert hook.verify_python(tmp_path) == str(py)


def test_verify_python_prefers_posix_venv(tmp_path):
    py = tmp_path / ".venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text("")

    assert hook.verify_python(tmp_path) == str(py)


def test_verify_python_falls_back_to_launcher_without_venv(tmp_path):
    assert hook.verify_python(tmp_path) == sys.executable


def test_verify_python_used_by_lint_and_test_checks(tmp_path, monkeypatch):
    py = tmp_path / ".venv/Scripts/python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(hook, "REPO_ROOT", tmp_path)

    for check in (hook.CHECK_LINT, hook.CHECK_SCRIPT_TESTS, hook.CHECK_LOCKS):
        argv, _cwd, _artifact = hook._command_for(check)
        assert argv[0] == str(py), f"{check} must run under the venv interpreter"
