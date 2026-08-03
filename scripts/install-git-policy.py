#!/usr/bin/env python3
"""Install Devkit's global pre-commit/pre-push branch policy.

The default is a read-only plan. Pass ``--yes`` to copy the runtime into a stable
user directory and configure Git globally. An unrelated existing ``core.hooksPath``
is preserved and causes a refusal rather than being overwritten.

``--check`` answers the question the install itself cannot: **is the runtime that
is actually enforcing the policy still the one in this checkout?** The install is a
*copy*, so the two drift the moment either moves, and nothing about a stale copy
looks wrong -- the hooks still fire, they just enforce an older policy. That is not
hypothetical: the runtime on the author's machine was installed from a
work-in-progress file roughly eighteen hours before that change was committed, so
the escape hatch (``DEVKIT_SKIP_BRANCH_POLICY``) silently did not exist there, and
the only symptom was an env var that appeared to do nothing.

The direction of the risk is what makes it worth a mode: a stale copy can be
missing a *loosening* (annoying) or a *tightening* (a policy everyone believes is
enforced and is not), and neither is visible anywhere.
"""

from __future__ import annotations

import argparse
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Drift:
    """One installed file that no longer matches the source it was copied from."""

    name: str
    reason: str


def compare_install(source_root: Path, target: Path) -> list[Drift]:
    """Installed files that differ from `source_root`'s, in `RUNTIME_FILES` order.

    Byte-for-byte, the same standard `sync-devkit.py --check` holds vendored files
    to, and for the same reason: this is a copy, and a copy that is merely *similar*
    is precisely the failure being looked for. An empty list means the policy being
    enforced is the policy in this checkout.

    Pure and filesystem-only -- no git, no network -- so the session-start status
    line can call it without spawning anything.
    """
    drifted: list[Drift] = []
    for source_name, destination_name in RUNTIME_FILES.items():
        destination = target / destination_name
        if not destination.is_file():
            drifted.append(Drift(destination_name, "not installed"))
            continue
        try:
            same = (source_root / source_name).read_bytes() == destination.read_bytes()
        except OSError as error:
            # Unreadable is not "identical". Reporting it as drift errs toward the
            # answer that makes someone look, which is the safe direction here.
            drifted.append(Drift(destination_name, f"unreadable ({error.strerror or error})"))
            continue
        if not same:
            drifted.append(Drift(destination_name, "differs from this checkout"))
    return drifted


def render_drift(target: Path, drifted: Sequence[Drift]) -> str:
    """Why a `--check` failed, and the one command that fixes it."""
    lines = [f"install-git-policy: the runtime installed at {target} is out of date:"]
    lines += [f"  {drift.name} -- {drift.reason}" for drift in drifted]
    lines.append(
        "The hooks run the *installed* copy, so this is the policy being enforced, "
        "not the one in this checkout. Re-run: python scripts/install-git-policy.py --yes"
    )
    return "\n".join(lines)


def run_check(source_root: Path, target: Path, runner: Runner = run_command) -> int:
    """`--check`: 0 identical, 1 drifted, 2 not installed here.

    "Not installed" is deliberately not a drift: a fresh clone, a CI runner and
    anyone else's machine all have nothing installed, and reporting that as a
    failure would make the check meaningless everywhere it is not the point.
    """
    if not _configured_hooks_path(runner):
        print(
            "install-git-policy: global core.hooksPath is unset -- the branch policy "
            "is not installed on this machine",
            file=sys.stderr,
        )
        return 2
    try:
        ensure_compatible_hooks_path(target, runner)
    except InstallRefusedError as error:
        print(f"install-git-policy: {error}", file=sys.stderr)
        return 2

    drifted = compare_install(source_root, target)
    if not drifted:
        print(f"install-git-policy: up to date ({target})")
        return 0
    print(render_drift(target, drifted), file=sys.stderr)
    return 1


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
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "report whether the installed runtime still matches this checkout; "
            "exit 1 when it has drifted, 2 when nothing is installed here"
        ),
    )
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    if args.check:
        return run_check(REPO_ROOT, target)
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
