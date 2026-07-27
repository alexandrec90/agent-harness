"""Integration tests for the SessionStart provisioning hook.

`.claude/hooks/session-start.sh` had **no tests**, and that is precisely how it
shipped a bug that broke every project except the one it was written for: it read
`requirements-dev.txt` unconditionally and expanded `${uv_version:?...}`. `:?` on
an empty value *exits* a non-interactive shell, and the trailing `|| echo` cannot
catch a parameter-expansion failure — so in any project without pip-tools locks,
provisioning died mid-script, before the PATH export, leaving an empty venv and no
ruff/mypy/pytest. Only remote sandboxes run that code path, so nothing local ever
went red.

**This file is vendored into every consuming project**, so it may not assert any
one project's dependency model. It builds throwaway projects of each shape and
checks the script dispatches correctly in all of them.

The script is driven for real (`CLAUDE_CODE_REMOTE=true`) with stub executables on
PATH, so nothing is installed and no network is touched. Every stub appends its
argv to a log, which is what the assertions read.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

SCRIPT = REPO_ROOT / ".claude" / "hooks" / "session-start.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or not SCRIPT.exists(), reason="needs bash and .claude/hooks/session-start.sh"
)

# The line whose absence *is* the bug: provisioning must reach the end of the
# script and persist the venv on PATH, whatever the project's dependency model.
PATH_EXPORT = "export PATH="


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_project(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path, Path]:
    """A throwaway project plus a stub bin dir. Returns (project, log, env_file)."""
    project = tmp_path / "proj"
    project.mkdir()
    for name, text in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    # The real config loader, so the manifest seam is exercised rather than faked.
    (project / "scripts" / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy(
        REPO_ROOT / "scripts" / "hooks" / "harness_config.py",
        project / "scripts" / "hooks" / "harness_config.py",
    )

    log = tmp_path / "calls.log"
    stub_bin = tmp_path / "bin"

    # `python3` must stay real for `harness_config.py` (that call is the seam under
    # test); anything else is logged and skipped.
    _write_exec(
        stub_bin / "python3",
        f'#!/bin/sh\necho "python3 $*" >> "{log}"\n'
        f'case "$*" in *harness_config.py*) exec "{sys.executable}" "$@";; esac\nexit 0\n',
    )
    for tool in ("npm", "curl", "node"):
        _write_exec(stub_bin / tool, f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 0\n')

    # Pre-created so the script's `[ -d .venv ]` short-circuits venv creation.
    _write_exec(
        project / ".venv" / "bin" / "python",
        f'#!/bin/sh\necho "venv-python $*" >> "{log}"\nexit 0\n',
    )

    return project, log, tmp_path / "env_file"


def _run(project: Path, log: Path, env_file: Path) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.update(
        CLAUDE_CODE_REMOTE="true",
        CLAUDE_PROJECT_DIR=str(project),
        CLAUDE_ENV_FILE=str(env_file),
        PATH=f"{project.parent / 'bin'}{os.pathsep}{env.get('PATH', '')}",
    )
    proc = subprocess.run(
        [BASH, str(SCRIPT)], cwd=project, env=env, capture_output=True, text=True, timeout=120
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    written = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    return proc.returncode, calls + proc.stdout, written


PYPROJECT = '[project]\nname = "probe"\nversion = "0.1.0"\n'


@pytest.mark.parametrize(
    ("files", "expect_in_calls"),
    [
        # uv-native: one committed lockfile, uv owns the venv.
        pytest.param({"uv.lock": "", "pyproject.toml": PYPROJECT}, "uv sync", id="uv-lock"),
        # pip-tools: fully-pinned compiled locks.
        pytest.param(
            {"requirements.txt": "ruff==0.15.0\n", "requirements-dev.txt": "uv==0.4.0\n"},
            "-r requirements.txt",
            id="requirements-locks",
        ),
        # Unlocked pyproject — the shape every generated project starts in.
        pytest.param({"pyproject.toml": PYPROJECT}, "-e .[dev]", id="unlocked-pyproject"),
    ],
)
def test_each_dependency_model_installs_and_still_exports_path(tmp_path, files, expect_in_calls):
    project, log, env_file = _make_project(tmp_path, files)
    rc, output, written = _run(project, log, env_file)
    assert rc == 0, output
    assert expect_in_calls in output, output
    # The regression assertion. Before the fix this file was empty for every model
    # except `requirements-locks`, because the shell had already exited.
    assert PATH_EXPORT in written, f"provisioning died before the PATH export\n{output}"


def test_project_with_no_dependency_file_warns_and_continues(tmp_path):
    """A project with no dependency file must warn and continue, not die."""
    project, log, env_file = _make_project(tmp_path, {"README.md": "# probe\n"})
    rc, output, written = _run(project, log, env_file)
    assert rc == 0, output
    assert "skipping Python install" in output
    assert PATH_EXPORT in written


def test_install_command_overrides_detection(tmp_path):
    """`[python] install_command` wins even when a lockfile is present."""
    marker = tmp_path / "override-ran"
    project, log, env_file = _make_project(
        tmp_path,
        {
            "uv.lock": "",
            "pyproject.toml": PYPROJECT,
            ".agent-harness.toml": f'[python]\ninstall_command = "touch {marker.as_posix()}"\n',
        },
    )
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert marker.exists(), f"install_command did not run\n{output}"
    assert "uv sync" not in output, "detection ran despite an explicit install_command"


def test_uv_pin_is_honoured_when_a_dev_lock_provides_one(tmp_path):
    project, log, env_file = _make_project(
        tmp_path, {"requirements.txt": "", "requirements-dev.txt": "uv==0.4.18\n"}
    )
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "uv==0.4.18" in output, output


def test_missing_uv_pin_falls_back_to_unpinned_uv(tmp_path):
    """The `:?`-to-`:+` fix: no pin must mean plain `uv`, not a dead shell."""
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, written = _run(project, log, env_file)
    assert rc == 0, output
    assert "pip install --quiet --disable-pip-version-check uv" in output.replace("\n", " "), output
    assert PATH_EXPORT in written


def test_frontend_install_is_skipped_without_a_frontend_tier(tmp_path):
    project, log, env_file = _make_project(tmp_path, {"pyproject.toml": PYPROJECT})
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "skipping npm install" in output
    assert "npm install" not in output.replace("skipping npm install", "")


def test_frontend_install_runs_when_the_manifest_declares_one(tmp_path):
    project, log, env_file = _make_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            ".agent-harness.toml": '[frontend]\nenabled = true\ndir = "web"\n',
            "web/package.json": "{}\n",
        },
    )
    rc, output, _ = _run(project, log, env_file)
    assert rc == 0, output
    assert "npm --prefix web" in output or "npm install --prefix web" in output, output


def test_script_contains_no_die_on_unset_expansions():
    """Guards the bug class, not just the one instance.

    `${var:?msg}` aborts a non-interactive shell and cannot be caught by `||`, so
    it must never appear in a hook that has to degrade gracefully across projects.
    """
    code = [
        line.strip()
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        # Comments are exempt: the script explains this very bug in prose.
        if not line.lstrip().startswith("#")
    ]
    offenders = [line for line in code if ":?" in line and "${" in line]
    assert not offenders, f"`${{var:?...}}` exits the shell; use `:-` or `:+`: {offenders}"
