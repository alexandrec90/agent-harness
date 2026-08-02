"""Every script a vendored hook dispatches to must actually ship, or be optional.

The rule this encodes: **a path a vendored script hard-codes is a promise.** Either
the file is in the MANIFEST (so every consumer gets it), or the dispatcher treats its
absence as an explicit, documented skip.

This lives in `tests/` rather than the vendored tree on purpose. It is a statement
about *devkit's* MANIFEST -- what devkit promises to ship -- which is not a question a
consuming repo can answer about itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from support import REPO_ROOT, load_script

HOOKS = REPO_ROOT / "scripts" / "hooks"

# `NAME = REPO_ROOT / "some/path.py"` -- how every dispatcher names a sibling.
REPO_ROOT_PATH_RE = re.compile(
    r'^(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*REPO_ROOT\s*/\s*"(?P<path>[^"]+)"'
)

# Dispatchers: vendored scripts that spawn other scripts in this repo.
DISPATCHERS = ("stop.py",)

# Paths a dispatcher names that are deliberately NOT vendored, each with the reason
# it is safe. Adding to this list is a design decision, which is the point of making
# it explicit rather than letting a missing file pass silently.
#
# Every entry here must ALSO be handled by an early-return in `_command_for` (or the
# equivalent), which `test_optional_tiers_skip_explicitly_when_absent` in the
# vendored contract test pins. "Optional" means the dispatcher knows it is optional,
# not merely that the subprocess call happens to fail quietly.
INTENTIONALLY_UNVENDORED = {
    # Each project's lint entrypoint knows its own tool set; rendered from
    # templates/core/scripts/lint-all.py.tmpl, then project-owned.
    "scripts/lint-all.py",
    # Sentinels name that project's own lockfiles (requirements*.txt vs uv.lock), so
    # a shared version would check the wrong files. Optional tier, skips explicitly.
    "scripts/check-lock-markers.py",
    # The test entrypoint, same story as lint-all.py: its scope is that project's own
    # testpaths, so it is rendered from templates/core/scripts/run-tests.py.tmpl and then
    # project-owned. `test_runner_argv` treats absence as an explicit fallback to
    # `-m pytest` rather than a skip, so the tier never silently disappears -- it only
    # loses the `logs/test-failures.log` artifact. `test_repo_contract.py` separately
    # requires it to exist in any repo that wires the Stop hook.
    "scripts/run-tests.py",
}

# Artifact paths, not scripts: written at runtime, absent until then.
NON_SCRIPT_SUFFIXES = (".json", ".log", ".md", ".toml")


def _manifest() -> set[str]:
    return set(load_script("scripts/sync-devkit.py").MANIFEST)


def _dispatched_paths(script: Path) -> dict[str, str]:
    """{constant name: repo-relative path} for every REPO_ROOT-anchored path."""
    found: dict[str, str] = {}
    for line in script.read_text(encoding="utf-8").splitlines():
        match = REPO_ROOT_PATH_RE.match(line)
        if match:
            found[match.group("name")] = match.group("path")
    return found


def test_the_regex_still_matches_something():
    """A silent zero-match would make every assertion below vacuously true."""
    for name in DISPATCHERS:
        assert _dispatched_paths(HOOKS / name), f"no REPO_ROOT paths found in {name}"


def test_every_dispatched_script_ships_or_is_explicitly_optional():
    manifest = _manifest()
    for name in DISPATCHERS:
        for const, rel in _dispatched_paths(HOOKS / name).items():
            if rel.endswith(NON_SCRIPT_SUFFIXES):
                continue
            if rel in INTENTIONALLY_UNVENDORED:
                continue
            assert rel in manifest, (
                f"{name} dispatches to {rel} (as {const}) but it is not in the "
                f"MANIFEST and not listed as intentionally unvendored. A consumer "
                f"that pulls the harness gets a dispatcher pointing at nothing, and "
                f"the failure is silent. Either vendor it or add it to "
                f"INTENTIONALLY_UNVENDORED with a reason."
            )


def test_every_dispatched_script_that_ships_actually_exists_here():
    """The MANIFEST can name a file devkit itself does not have."""
    for name in DISPATCHERS:
        for rel in _dispatched_paths(HOOKS / name).values():
            if rel.endswith(NON_SCRIPT_SUFFIXES) or rel in INTENTIONALLY_UNVENDORED:
                continue
            assert (REPO_ROOT / rel).exists(), f"{name} dispatches to missing {rel}"


def test_optional_list_names_only_real_dispatch_targets():
    """A stale entry here would mask a genuine gap that appeared later."""
    dispatched = {rel for name in DISPATCHERS for rel in _dispatched_paths(HOOKS / name).values()}
    stale = INTENTIONALLY_UNVENDORED - dispatched
    assert not stale, f"INTENTIONALLY_UNVENDORED lists paths nothing dispatches to: {sorted(stale)}"
