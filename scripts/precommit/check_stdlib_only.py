#!/usr/bin/env python3
"""pre-commit hook: the agent-harness hook scripts must import stdlib only.

This is the one contract in the harness that cannot be enforced by a test in the repo
that breaks it. Hook scripts run **before the virtualenv is active** — `session-start.sh`
is what creates the venv, and `stop.py`/`lint-fix.py` are wired as bare `python3`. So a
third-party import in `scripts/hooks/` does not fail a test suite (which runs *inside* the
venv, where the package is installed); it fails at hook time, in a session, with a
ModuleNotFoundError from a script whose entire job was to provision the thing that was
missing. CLAUDE.md states the rule; this makes it true.

The check is an AST scan for absolute imports whose root module is neither stdlib nor a
first-party sibling. First-party is derived from the files on disk — `stop.py` legitimately
does `import harness_config`, and `task_slug.py` `import task_branch` — so the hook
needs no allowlist to maintain.

Test files are excluded by the hook config, not by this script: they import pytest by
definition, and a script that silently ignores whole paths is a script that stops
protecting them.

Usage (pre-commit passes the filenames):
    python scripts/precommit/check_stdlib_only.py scripts/hooks/stop.py ...

stdlib only, naturally.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# `__future__` is not in stdlib_module_names but is always importable.
ALWAYS_ALLOWED = frozenset({"__future__"})


def first_party_modules(paths: list[Path]) -> set[str]:
    """Module names importable as siblings of the files being checked.

    A hook script's own directory is `sys.path[0]` at runtime, and several deliberately
    put `scripts/` on the path too (that is why `harness_config` and `task_branch`
    resolve). Deriving the set from disk means adding a new shared helper never requires
    touching an allowlist here.
    """
    names: set[str] = set()
    for directory in {p.resolve().parent for p in paths}:
        for candidate in (directory, directory.parent):
            if not candidate.is_dir():
                continue
            for entry in candidate.glob("*.py"):
                names.add(entry.stem)
            for entry in candidate.iterdir():
                if entry.is_dir() and (entry / "__init__.py").exists():
                    names.add(entry.name)
    return names


def imported_roots(source: str) -> set[str]:
    """Root module names of every absolute import in `source`.

    Relative imports are skipped: they resolve inside the package being committed, so
    they cannot be a third-party dependency.
    """
    roots: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def check(path: Path, allowed_first_party: set[str]) -> list[str]:
    """Non-stdlib, non-first-party imports found in `path`."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot be read ({exc})"]
    try:
        roots = imported_roots(source)
    except SyntaxError as exc:
        return [f"is not parseable Python ({exc})"]

    allowed = set(sys.stdlib_module_names) | ALWAYS_ALLOWED | allowed_first_party
    return [
        f"imports {root!r}, which is neither stdlib nor a first-party sibling"
        for root in sorted(roots - allowed)
    ]


def main(argv: list[str] | None = None) -> int:
    paths = [Path(a) for a in (argv if argv is not None else sys.argv[1:])]
    paths = [p for p in paths if p.suffix in {".py", ".pyi"}]
    if not paths:
        return 0

    allowed_first_party = first_party_modules(paths)
    failed = False
    for path in paths:
        for problem in check(path, allowed_first_party):
            failed = True
            print(f"{path}: {problem}")
    if failed:
        print(
            "\nHook scripts run before the virtualenv exists — a third-party import here "
            "fails at hook time, not at test time. Use the stdlib, or move the logic into "
            "a script the venv runs."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
