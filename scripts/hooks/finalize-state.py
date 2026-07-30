#!/usr/bin/env python3
"""Stop-hook helper: finalizes a skill's state.json via the state engine.

Thin, portable wrapper around `.claude/skills/state-tools/state-engine.py`. When
the skill's interim artifacts are present, it applies them to state.json and
removes the consumed artifacts on success. Safe to call every stop: it exits 0
when artifacts are missing.

`finalize_state` takes an explicit repo root so it can be unit-tested with a tmp
dir (`scripts/hooks/tests/test_finalize_state.py`).
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
SCHEMAS = ("audit", "modules", "files")


def required_artifacts(skill_dir: Path, schema: str) -> list[Path]:
    """Artifacts that must all exist before finalization runs."""
    if schema == "audit":
        return [skill_dir / "scan-results.json", skill_dir / "scan-plan.json"]
    return [skill_dir / "state-updates.json"]


def consumable_artifacts(skill_dir: Path, schema: str) -> list[Path]:
    """Artifacts to delete after a successful apply."""
    if schema == "audit":
        return [skill_dir / "scan-results.json", skill_dir / "scan-plan.json"]
    return [skill_dir / "state-updates.json"]


def finalize_state(skill: str, schema: str, repo_root: Path) -> int:
    """Apply a skill's interim artifacts to its state.json. Returns exit code."""
    skill_dir = repo_root / ".claude/skills" / skill
    if not skill_dir.is_dir():
        return 0

    if not all(p.exists() for p in required_artifacts(skill_dir, schema)):
        return 0

    engine = repo_root / ".claude/skills/state-tools/state-engine.py"
    result = subprocess.run(
        [sys.executable, str(engine), "apply", "--skill", skill, "--schema", schema],
        cwd=repo_root,
    )

    if result.returncode == 0:
        for artifact in consumable_artifacts(skill_dir, schema):
            artifact.unlink(missing_ok=True)

    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Finalize a skill state.json via the state engine."
    )
    parser.add_argument("--skill", required=True)
    parser.add_argument("--schema", required=True, choices=SCHEMAS)
    args = parser.parse_args()
    return finalize_state(args.skill, args.schema, REPO_ROOT)


if __name__ == "__main__":
    sys.exit(main())
