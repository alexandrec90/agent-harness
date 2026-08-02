"""Path setup and helpers for devkit's own tests.

Two things make this file's shape non-obvious, and both are load-bearing.

**There is deliberately no `conftest.py` in this directory.** `scripts/hooks/tests/`
has one, it is vendored (it is in `sync-devkit.py`'s MANIFEST), and every test in
that tree does `from conftest import load_module`. pytest puts both test directories
on `sys.path`, so a second `conftest.py` here would race it for the top-level module
name `conftest` — whichever directory pytest collected first would win, and the other
tree's tests would fail to import. That is exactly what happened: `pytest tests/
scripts/hooks/tests/` passed while `pytest scripts/hooks/tests/ tests/` failed with
seven collection errors. A uniquely-named module cannot collide, so path setup lives
here instead, and this module is imported first by every test in this tree.

**It re-exports the modules under test.** Importing `support` for its side effect and
then importing `devkit_ports` separately would work — until the import sorter put
`devkit_ports` first (alphabetically) and the path was not yet set up. Re-exporting
makes the dependency explicit and immune to reordering.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"

# `scripts/` for the importable devkit modules; `scripts/hooks/` so a test can load
# the vendored harness_config that generated manifests must satisfy.
for _path in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "hooks"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import devkit_jsonc  # noqa: E402
import devkit_ports  # noqa: E402
import devkit_project  # noqa: E402
import devkit_render  # noqa: E402
import git_policy  # noqa: E402
import harness_config  # noqa: E402
import sweep  # noqa: E402

__all__ = [
    "REPO_ROOT",
    "TEMPLATES",
    "devkit_jsonc",
    "devkit_ports",
    "devkit_project",
    "devkit_render",
    "gh_steps_without_repo_context",
    "git_policy",
    "harness_config",
    "load_script",
    "sweep",
]


def _logical_commands(script: str) -> list[str]:
    """`script` split on newlines, with backslash-continued lines rejoined."""
    joined = script.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines()]


def gh_steps_without_repo_context(workflow: dict) -> list[str]:
    """Names of steps that call `gh` in a job that has no checkout and no repo to use.

    `gh` resolves the repository from `git remote` whenever it is not told one, so in a
    job that skipped `actions/checkout` it exits on "fatal: not a git repository"
    before doing anything — a failure that names git and says nothing about the missing
    `--repo`. Either `GH_REPO` is in scope or every invocation passes `--repo`.
    """
    offenders = []
    workflow_env = workflow.get("env") or {}
    for job_name, job in (workflow.get("jobs") or {}).items():
        steps = job.get("steps") or []
        if any("actions/checkout" in str(step.get("uses", "")) for step in steps):
            continue
        job_env = job.get("env") or {}
        for step in steps:
            script = step.get("run")
            if not script:
                continue
            env = {**workflow_env, **job_env, **(step.get("env") or {})}
            if "GH_REPO" in env:
                continue
            for command in _logical_commands(script):
                if command.split(" ", 1)[:1] == ["gh"] and "--repo" not in command:
                    offenders.append(f"{job_name} / {step.get('name', '<unnamed>')}")
                    break
    return offenders


def load_script(relpath: str):
    """Load a hyphen-named script (path relative to the repo root) as a module.

    `new-project.py` cannot be imported normally. The subtlety is the registration
    order: the module must be in `sys.modules` **before** `exec_module` runs,
    because `@dataclass` resolves its string annotations by looking the defining
    module up by name — exec'ing first raises `AttributeError: 'NoneType' object has
    no attribute '__dict__'` from inside dataclasses, which points nowhere useful.
    """
    path = REPO_ROOT / relpath
    name = path.stem.replace("-", "_")
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {relpath} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module
