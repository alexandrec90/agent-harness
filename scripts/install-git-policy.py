#!/usr/bin/env python3
"""Install Devkit's global pre-commit/pre-push branch policy.

The default is a read-only plan. Pass ``--yes`` to copy the runtime into a stable
user directory and configure Git globally. An unrelated existing ``core.hooksPath``
is preserved and causes a refusal rather than being overwritten.
"""

from __future__ import annotations

import argparse
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path.home() / ".devkit" / "git-hooks"
RUNTIME_FILES = {
    "scripts/git_policy.py": "devkit_git_policy.py",
    "scripts/git-hooks/pre-commit": "pre-commit",
    "scripts/git-hooks/pre-push": "pre-push",
}

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class InstallRefusedError(RuntimeError):
    """The install would replace Git configuration not owned by Devkit."""


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def install_files(source_root: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for source_name, destination_name in RUNTIME_FILES.items():
        source = source_root / source_name
        destination = target / destination_name
        shutil.copy2(source, destination)
        if destination_name in {"pre-commit", "pre-push"}:
            destination.chmod(
                destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )


def _configured_hooks_path(runner: Runner) -> str:
    result = runner(["git", "config", "--global", "--get", "core.hooksPath"])
    return result.stdout.strip() if result.returncode == 0 else ""


def ensure_compatible_hooks_path(target: Path, runner: Runner = run_command) -> None:
    configured = _configured_hooks_path(runner)
    if not configured:
        return
    target_value = target.resolve().as_posix()
    configured_path = Path(configured).expanduser()
    try:
        same = configured_path.resolve() == target.resolve()
    except OSError:
        same = configured.replace("\\", "/").rstrip("/") == target_value.rstrip("/")
    if not same:
        raise InstallRefusedError(
            f"global core.hooksPath is already '{configured}'; refusing to replace it"
        )


def _require_ok(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "command failed").strip()
    raise InstallRefusedError(f"{action}: {detail}")


def configure_git(target: Path, runner: Runner = run_command) -> None:
    values = (
        ("core.hooksPath", target.resolve().as_posix()),
        ("fetch.prune", "true"),
        ("devkit.branchPolicy.failClosed", "true"),
    )
    for key, value in values:
        result = runner(["git", "config", "--global", key, value])
        _require_ok(result, f"could not configure {key}")


def render_plan(target: Path) -> str:
    files = "\n".join(
        f"  copy {source} -> {target / destination}"
        for source, destination in RUNTIME_FILES.items()
    )
    return (
        "Devkit global Git policy install:\n"
        f"{files}\n"
        f"  git config --global core.hooksPath {target.resolve().as_posix()}\n"
        "  git config --global fetch.prune true\n"
        "  git config --global devkit.branchPolicy.failClosed true"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"stable runtime directory (default: {DEFAULT_TARGET})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print the install plan without changing anything (default)",
    )
    mode.add_argument(
        "--yes",
        dest="dry_run",
        action="store_false",
        help="copy the hooks and update global Git configuration",
    )
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    print(render_plan(target))
    try:
        ensure_compatible_hooks_path(target)
        if args.dry_run:
            print("\nDry run -- nothing changed. Re-run with --yes to install.")
            return 0
        install_files(REPO_ROOT, target)
        configure_git(target)
    except (InstallRefusedError, OSError) as error:
        print(f"\ninstall-git-policy: REFUSED -- {error}", file=sys.stderr)
        return 2
    print("\ninstall-git-policy: installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
