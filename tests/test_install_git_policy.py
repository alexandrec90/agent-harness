"""Tests for installing Devkit's global Git hook dispatcher."""

import os
import subprocess
import sys

from support import REPO_ROOT, load_script

installer = load_script("scripts/install-git-policy.py")


class FakeRunner:
    def __init__(self, hooks_path=""):
        self.hooks_path = hooks_path
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if tuple(argv) == ("git", "config", "--global", "--get", "core.hooksPath"):
            return subprocess.CompletedProcess(
                argv, 0 if self.hooks_path else 1, self.hooks_path, ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_install_copies_all_runtime_files_and_marks_hooks_executable(tmp_path):
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)

    assert (target / "devkit_git_policy.py").is_file()
    for name in ("pre-commit", "pre-push"):
        hook = target / name
        assert hook.is_file()
        if os.name != "nt":
            assert os.access(hook, os.X_OK)


def test_installed_wrappers_delegate_to_the_policy_module(tmp_path):
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    (target / "devkit_git_policy.py").write_text(
        "def main(name):\n    print(f'delegated:{name}')\n    return 0\n",
        encoding="utf-8",
    )

    for hook_name in ("pre-commit", "pre-push"):
        result = subprocess.run(
            [sys.executable, str(target / hook_name)],
            input="" if hook_name == "pre-push" else None,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"delegated:{hook_name}"


def test_configuration_sets_global_dispatcher_and_pruning(tmp_path):
    runner = FakeRunner()
    installer.configure_git(tmp_path / "hooks", runner)
    assert (
        "git",
        "config",
        "--global",
        "core.hooksPath",
        (tmp_path / "hooks").resolve().as_posix(),
    ) in runner.calls
    assert ("git", "config", "--global", "fetch.prune", "true") in runner.calls
    assert (
        "git",
        "config",
        "--global",
        "devkit.branchPolicy.failClosed",
        "true",
    ) in runner.calls


def test_install_refuses_to_overwrite_an_unrelated_global_hooks_path(tmp_path):
    runner = FakeRunner(hooks_path="C:/someone-elses-hooks\n")
    try:
        installer.ensure_compatible_hooks_path(tmp_path / "hooks", runner)
    except installer.InstallRefusedError as error:
        assert "someone-elses-hooks" in str(error)
    else:
        raise AssertionError("an unrelated core.hooksPath must be preserved")


def test_reinstall_accepts_the_same_global_hooks_path(tmp_path):
    target = (tmp_path / "hooks").resolve()
    runner = FakeRunner(hooks_path=f"{target.as_posix()}\n")
    installer.ensure_compatible_hooks_path(target, runner)
