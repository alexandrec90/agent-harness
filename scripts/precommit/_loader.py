#!/usr/bin/env python3
"""Load a devkit module by file path. Not a hook itself — a helper for the ones here.

The pre-commit hooks run with the *consuming* repo as the cwd, and when consumed from a
pinned rev they live in pre-commit's clone of devkit. Neither makes `scripts/hooks/` or a
hyphenated filename importable by name, so the hooks reach their dependencies by path.

**The registration order is the whole reason this is a shared helper.** The module must be
in `sys.modules` *before* `exec_module` runs, because `@dataclass` resolves its string
annotations by looking the defining module up by name — and `harness_config.py` is nothing
but frozen dataclasses. Exec first and it dies inside `dataclasses` with
`AttributeError: 'NoneType' object has no attribute '__dict__'`, which points nowhere near
the real cause. This repo had already been bitten twice (`tests/support.py`,
`scripts/new-project.py`, both of which document it); writing a third loader that got it
wrong is what turned two comments into one function.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_by_path(name: str, path: Path) -> ModuleType:
    """Import `path` as a module called `name`. Raises ImportError if it cannot."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Leave no half-initialised module behind for the next importer to find.
        del sys.modules[name]
        raise
    return module
