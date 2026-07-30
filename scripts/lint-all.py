#!/usr/bin/env python3
"""Run every linter and write the failures to a single parseable artifact.

devkit's own copy of the lint runner it ships in `templates/core/scripts/`. The
contract is the same one CLAUDE.md describes: an agent fixing lint reads
`logs/lint-errors.log`, never the terminal. So this script keeps the terminal to a
status line plus the artifact path, and puts everything actionable in the file — on
failure *and* on success, where it writes an empty artifact so a stale run cannot
mislead the next agent.

Auto-fix runs before the reporting pass, so only genuinely unfixable errors are
reported and the agent never burns a cycle on something `ruff --fix` already solved.

**Two deliberate differences from the template version**, both because devkit is
upstream rather than a consumer:

  - It formats `scripts/hooks/` instead of protecting it. A generated project must
    not rewrite its vendored harness (`sync-devkit.py --check` fails the build over
    a byte of drift it cannot fix in source), so the template carries a
    `NO_FIX_SCOPE`. Here those files are the source of truth, CI gates them with
    `ruff format --check .`, and formatting them is the whole point.
  - `templates/` is excluded rather than linted. Its `.py` files are *content*: they
    are linted by the `ruff.toml` that ships alongside them into each generated
    project, which carries the `scripts/**` allowances they need. Linting them under
    devkit's own config reports findings that are correct there and wrong here.

Usage:
    python scripts/lint-all.py            # whole repo
    python scripts/lint-all.py --changed  # working-tree diff vs HEAD, plus untracked
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "lint-errors.log"

# What mypy type-checks. Unlike a generated project's copy, this includes
# `scripts/hooks/` — devkit owns that code, so a type error there is devkit's to fix.
MYPY_SCOPE = ["scripts", "tests"]

# Repo-relative prefixes that are content, not devkit source. `ruff.toml` and
# `pyproject.toml` already exclude these, but a config `exclude` does **not** apply to
# a path passed explicitly on the command line unless `force-exclude` is set — and
# `--changed` passes explicit paths. ruff.toml sets `force-exclude` for that reason;
# this filter is the same guard for mypy, which has no equivalent setting, and it keeps
# `--changed` from spending a pass on files neither tool will report on anyway.
EXCLUDED_PREFIXES = ("templates/",)


def changed_python_files() -> list[str]:
    """Tracked-but-modified plus untracked .py files, relative to the repo root."""
    tracked = _git("diff", "--name-only", "HEAD")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    names = {n for n in (tracked + untracked) if n.endswith(".py")}
    return sorted(
        n for n in names if (REPO_ROOT / n).exists() and not n.startswith(EXCLUDED_PREFIXES)
    )


def _git(*args: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)
    return result.stdout.splitlines() if result.returncode == 0 else []


def _missing_module(cmd: list[str]) -> bool:
    """True when `cmd` is a `-m` invocation of a module this interpreter lacks.

    The linters run as `[sys.executable, "-m", tool, ...]`, so the executable always
    exists and `subprocess.run` never raises FileNotFoundError — the interpreter
    itself exits 1 with "No module named mypy" on stderr. Without this probe that
    text lands in the artifact as an unfixable finding, which is the exact outcome
    run_tool's contract exists to prevent. Probing beats matching the message: the
    subprocess runs under sys.executable, so find_spec here answers for the very
    interpreter that would run it.
    """
    if len(cmd) < 3 or cmd[0] != sys.executable or cmd[1] != "-m":
        return False
    try:
        return importlib.util.find_spec(cmd[2]) is None
    except (ImportError, ValueError):
        return True


def run_tool(name: str, cmd: list[str], fix_hint: str) -> str:
    """Run one linter; return its artifact section, or "" when it passed or was absent.

    A missing tool is NOT a failure. Writing "command not found" into the artifact
    would hand the agent something it cannot fix in the source tree, so it degrades
    to a terminal note instead.
    """
    if _missing_module(cmd):
        print(f"  {name}: not installed — skipped")
        return ""
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"  {name}: not installed — skipped")
        return ""
    if result.returncode == 0:
        print(f"  {name}: ok")
        return ""
    body = (result.stdout + result.stderr).strip()
    print(f"  {name}: FAILED")
    return f"# {name}\n# fix: {fix_hint}\n{body}\n\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="lint only the working-tree diff")
    # Accepted, and a no-op here: devkit has no detect-secrets pass to skip. The Stop
    # hook passes `--no-secrets` unconditionally — see the same argument in
    # `templates/core/scripts/lint-all.py.tmpl` for why, and why *parsing* it is part
    # of the contract rather than optional politeness.
    parser.add_argument(
        "--no-secrets",
        action="store_true",
        help="skip the secrets pass (accepted for Stop-hook compatibility; no-op here)",
    )
    args = parser.parse_args(argv)

    targets: list[str] = []
    if args.changed:
        targets = changed_python_files()
        if not targets:
            print("lint-all: no changed Python files; nothing to do.")
            _write_artifact("")
            return 0
    scope = targets or ["."]

    print(f"lint-all: {'changed files' if args.changed else 'whole repo'}")

    # Auto-fix first, then report. Both ruff passes mutate the same files, so they
    # must stay sequential relative to each other. No `--exclude` guard here: see the
    # module docstring — devkit formats its own harness, and CI's `ruff format --check`
    # is what would fail if it did not.
    subprocess.run(
        [sys.executable, "-m", "ruff", "check", *scope, "--fix", "--unsafe-fixes"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", *scope],
        cwd=REPO_ROOT,
        capture_output=True,
    )

    sections = ""
    sections += run_tool(
        "ruff",
        [sys.executable, "-m", "ruff", "check", *scope, "--output-format=full"],
        "ruff check . --fix --unsafe-fixes",
    )
    sections += run_tool(
        "mypy",
        [sys.executable, "-m", "mypy", *(targets or MYPY_SCOPE), "--show-error-codes"],
        f"mypy {' '.join(MYPY_SCOPE)} --show-error-codes",
    )

    _write_artifact(sections)
    if sections:
        print(f"\nlint-all: FAILED — details in {ARTIFACT.relative_to(REPO_ROOT)}")
        return 1
    print(f"\nlint-all: clean (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
    return 0


def _write_artifact(sections: str) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    header = "# source: scripts/lint-all.py\n" if sections else ""
    ARTIFACT.write_text(header + sections, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
