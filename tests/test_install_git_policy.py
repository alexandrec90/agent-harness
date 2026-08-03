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


# --- --check: is the installed runtime still this checkout's? ----------------
# The install is a copy, so it goes stale silently -- the hooks keep firing and
# just enforce an older policy. The runtime on the author's machine was installed
# from a work-in-progress file ~18 hours before that change was committed, so
# `DEVKIT_SKIP_BRANCH_POLICY` did not exist there while the source said it did.


def test_a_fresh_install_reports_no_drift(tmp_path):
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    assert installer.compare_install(REPO_ROOT, target) == []


def test_an_installed_copy_that_fell_behind_the_source_is_drift(tmp_path):
    """The actual incident, reduced: the copy is a valid file, it is just old."""
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    (target / "devkit_git_policy.py").write_text("# an older policy\n", encoding="utf-8")

    drifted = installer.compare_install(REPO_ROOT, target)
    assert [d.name for d in drifted] == ["devkit_git_policy.py"]
    assert "differs" in drifted[0].reason


def test_a_missing_installed_file_is_drift_rather_than_silence(tmp_path):
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    (target / "pre-push").unlink()

    drifted = installer.compare_install(REPO_ROOT, target)
    assert [d.name for d in drifted] == ["pre-push"]
    assert drifted[0].reason == "not installed"


def test_every_drifted_file_is_reported_not_only_the_first(tmp_path):
    """One re-install fixes all of them, so naming one at a time would mean
    re-running the check to discover the next."""
    target = tmp_path / "hooks"
    installer.install_files(REPO_ROOT, target)
    for name in ("devkit_git_policy.py", "pre-commit"):
        (target / name).write_text("# stale\n", encoding="utf-8")

    assert len(installer.compare_install(REPO_ROOT, target)) == 2


def test_the_drift_report_names_the_fix(tmp_path):
    target = tmp_path / "hooks"
    drifted = [installer.Drift("pre-commit", "differs from this checkout")]
    report = installer.render_drift(target, drifted)
    assert "pre-commit" in report
    assert "--yes" in report


def test_check_exits_zero_when_the_install_matches(tmp_path):
    target = (tmp_path / "hooks").resolve()
    installer.install_files(REPO_ROOT, target)
    runner = FakeRunner(hooks_path=f"{target.as_posix()}\n")
    assert installer.run_check(REPO_ROOT, target, runner) == 0


def test_check_exits_one_when_the_install_has_drifted(tmp_path):
    target = (tmp_path / "hooks").resolve()
    installer.install_files(REPO_ROOT, target)
    (target / "pre-commit").write_text("# stale\n", encoding="utf-8")
    runner = FakeRunner(hooks_path=f"{target.as_posix()}\n")
    assert installer.run_check(REPO_ROOT, target, runner) == 1


def test_check_exits_two_where_the_policy_is_not_installed(tmp_path):
    """A fresh clone, CI, anyone else's machine. Reporting that as drift would make
    the check meaningless everywhere it is not the point."""
    assert installer.run_check(REPO_ROOT, tmp_path / "hooks", FakeRunner()) == 2


def test_check_exits_two_when_the_hooks_path_belongs_to_someone_else(tmp_path):
    runner = FakeRunner(hooks_path="C:/someone-elses-hooks\n")
    assert installer.run_check(REPO_ROOT, tmp_path / "hooks", runner) == 2
