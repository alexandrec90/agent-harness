#!/usr/bin/env python3
"""pre-commit hook: the vendored harness in this repo matches the devkit rev it pins.

This is the drift check `scripts/sync-harness.py --check` performs, with the awkward part
removed. That script resolves its source from `$AGENT_HARNESS_DIR`, and **no-ops clean
(exit 0) when it is unset** — correct before a project adopts the shared repo, and an
inert gate afterwards. Anyone who has not exported the variable is running a check that
checks nothing, and it says so in a line that is easy to miss.

Run through pre-commit there is nothing to configure, because pre-commit has already
cloned devkit at the `rev` the config pins: this file *is* the source of truth for that
comparison, so the version being compared against is the one written down in
`.pre-commit-config.yaml` and updated by `pre-commit autoupdate`.

In devkit itself the comparison is vacuous (the clone and the working tree are the same
repo), so the hook reports that and exits 0 rather than pretending to verify something.

Usage (no filenames; the manifest decides what is compared):
    python scripts/precommit/check_harness_drift.py

stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_by_path

DEVKIT_ROOT = Path(__file__).resolve().parents[2]


def _load_sync_harness():
    """Import `scripts/sync-harness.py` by path — the hyphen makes it unimportable."""
    return load_by_path("_sync_harness", DEVKIT_ROOT / "scripts" / "sync-harness.py")


def is_same_repo(a: Path, b: Path) -> bool:
    """True when both paths are the same directory, resolving links."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    # pre-commit runs hooks from the root of the repo being committed.
    repo_root = Path.cwd()

    if is_same_repo(DEVKIT_ROOT, repo_root):
        print("harness-drift: this IS devkit — nothing to compare against. Skipped.")
        return 0

    sync = _load_sync_harness()
    drifted, missing_upstream, ok = sync.classify(DEVKIT_ROOT, repo_root, sync.MANIFEST)

    if missing_upstream:
        # devkit does not have a file its own manifest lists: an upstream packaging bug,
        # not something this repo can fix. Report it rather than counting it as in-sync.
        print(f"harness-drift: {len(missing_upstream)} manifest file(s) missing from devkit:")
        for rel in missing_upstream:
            print(f"  ? {rel}")

    if drifted:
        print(f"harness-drift: {len(drifted)} vendored file(s) differ from devkit:")
        for rel in drifted:
            state = "absent here" if not (repo_root / rel).exists() else "modified"
            print(f"  ✗ {rel} ({state})")
        print(
            "\nThe vendored harness is upstream code; edit it in devkit, not here.\n"
            "  adopt upstream:        python scripts/sync-harness.py --pull\n"
            "  send a change up:      python scripts/sync-harness.py --push\n"
            "(both need AGENT_HARNESS_DIR pointing at a devkit checkout)"
        )
        return 1

    if missing_upstream:
        return 1

    print(f"harness-drift: all {len(ok)} vendored files match devkit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
