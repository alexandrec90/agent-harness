#!/usr/bin/env python3
"""One line of workspace health, for a SessionStart hook.

Answers the two questions that went unasked for a whole day of parallel work and
cost an afternoon to unpick: **is there work stranded in any checkout**, and **is
any checkout behind on devkit**. Both were already computable; nothing was asking.

Design constraints, all from the fact that this runs at the top of every session:

- **Never blocks.** Always exits 0. A status line that can fail a session start is
  a status line that gets removed the first time it is wrong.
- **No network.** `--no-fetch`, so ahead/behind may be stale -- but "3 checkouts
  have uncommitted work" does not need a fetch to be true, and a hook that costs
  seconds gets disabled.
- **Silent when healthy.** Prints nothing when there is nothing to say, so the one
  time it does speak is worth reading.
- **Absent workspace is silence, not an error.** The registry is a workstation-local
  file; on CI, a fresh clone, or anyone else's machine there is simply nothing to
  report.

Tested in `tests/test_workspace_status.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = REPO_ROOT.parent / "alex-projects.code-workspace"


def stranded_line(results: list[sweep.Result]) -> str:
    """The stranded-work half; "" when every checkout is clean.

    Names the checkouts rather than counting them: "3 checkouts need action" makes
    you go run something else to find out which, and at the top of a session the
    whole point is to not send you on an errand.
    """
    actionable = [r for r in results if r.verdict in sweep.ACTIONABLE]
    if not actionable:
        return ""
    parts = [f"{r.state.name} ({r.verdict})" for r in actionable]
    return f"stranded work: {', '.join(parts)}"


def behind_line(behind: dict[str, str], latest: str) -> str:
    """The devkit-freshness half; "" when nothing is knowably behind."""
    if not behind:
        return ""
    parts = [f"{name} on {tag}" for name, tag in sorted(behind.items())]
    return f"devkit {latest} available: {', '.join(parts)}"


def render(results: list[sweep.Result], behind: dict[str, str], latest: str) -> str:
    """The whole message, or "" when there is nothing worth saying."""
    lines = [line for line in (stranded_line(results), behind_line(behind, latest)) if line]
    if not lines:
        return ""
    return "\n".join(f"[workspace] {line}" for line in lines)


def projects_behind(root: Path, names: list[str], latest: str) -> dict[str, str]:
    """`{checkout: its vendored tag}` for those not on `latest`.

    Only reports a checkout whose vendored tag is *recorded*. An unrecorded one is
    "cannot tell" -- see `sync-devkit.stale_pin` -- and guessing would put every
    not-yet-upgraded project in this line forever until someone stopped reading it.
    """
    behind: dict[str, str] = {}
    for name in names:
        receipt = root / name / "DEVKIT_FILES.json"
        if not receipt.is_file():
            continue
        try:
            raw = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        tag = raw.get("devkit_tag") if isinstance(raw, dict) else None
        if isinstance(tag, str) and tag and tag != latest:
            behind[name] = tag
    return behind


def latest_devkit_tag(devkit: Path) -> str:
    """devkit's newest release tag, or "" -- read from refs, without running git.

    Reading `.git/refs/tags` and `packed-refs` directly keeps this hook stdlib-only
    and spawn-free at session start. A missing or unreadable ref store is "" rather
    than an error, which the caller treats as "nothing to say".
    """
    tags: list[str] = []
    loose = devkit / ".git" / "refs" / "tags"
    if loose.is_dir():
        tags.extend(p.name for p in loose.iterdir() if p.is_file())
    packed = devkit / ".git" / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                marker = " refs/tags/"
                if marker in line and not line.startswith("#"):
                    tags.append(line.split(marker, 1)[1].strip().removesuffix("^{}"))
        except OSError:
            pass
    return max(set(tags), key=_version_key, default="")


def _version_key(tag: str) -> tuple[int, ...]:
    """Sort `vMAJOR.MINOR.PATCH` numerically; anything else sorts lowest."""
    try:
        return tuple(int(part) for part in tag.lstrip("v").split("."))
    except ValueError:
        return (-1,)


def main(argv: list[str] | None = None) -> int:
    workspace = DEFAULT_WORKSPACE
    if not workspace.is_file():
        return 0
    try:
        names = sweep.parse_workspace(workspace.read_text(encoding="utf-8"))
        if not names:
            return 0
        root = workspace.parent
        results = sweep.sweep(root, names, fetch=False)
        latest = latest_devkit_tag(root / "devkit")
        behind = projects_behind(root, names, latest) if latest else {}
        message = render(results, behind, latest)
    except Exception as exc:
        print(f"[workspace] status unavailable ({type(exc).__name__})", file=sys.stderr)
        return 0
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
