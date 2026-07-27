"""Path setup and helpers for devkit's own tests.

Two things make this file's shape non-obvious, and both are load-bearing.

**There is deliberately no `conftest.py` in this directory.** `scripts/hooks/tests/`
has one, it is vendored (it is in `sync-harness.py`'s MANIFEST), and every test in
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

import devkit_ports  # noqa: E402
import devkit_render  # noqa: E402
import harness_config  # noqa: E402
import sweep  # noqa: E402

__all__ = [
    "REPO_ROOT",
    "TEMPLATES",
    "devkit_ports",
    "devkit_render",
    "harness_config",
    "load_script",
    "sweep",
]


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
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module
