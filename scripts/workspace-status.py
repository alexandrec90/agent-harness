#!/usr/bin/env python3
"""One line of workspace health, for a SessionStart hook.

Answers the questions that went unasked for a whole day of parallel work and cost
an afternoon to unpick: **is there work stranded in any checkout**, **is any
checkout behind on devkit**, and **is the branch policy being enforced still the
one in this checkout**. All three were already computable; nothing was asking.

The third is the quietest of them. The global hooks are a *copy*, so a stale one
still fires and simply enforces an older policy -- there is no failure to notice,
which is why it needs a line here rather than a check someone remembers to run.

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
# Where `install-git-policy.py` puts the runtime the global hooks actually execute.
POLICY_TARGET = Path.home() / ".devkit" / "git-hooks"

# `install-git-policy.py` is hyphenated and so cannot be imported by name. Going
# through the shared loader keeps the file list and the comparison in one place --
# duplicating them here would put a second copy inside the very hook whose job is
# to notice copies going stale. Guarded because nothing in this file may break a
# session start: an unavailable loader just means this half stays silent.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
    # Resolved by the sys.path insert above; `scripts/precommit/` is not an
    # importable package.
    from _loader import load_by_path
except ImportError:  # pragma: no cover - the repo always ships _loader.py
    # mypy DOES resolve the import above, so it knows the real signature and reads
    # this fallback as a type error rather than the guard it is. The ignore has to
    # be here, on the assignment -- an `import-not-found` ignore on the import
    # itself is what used to be here, and it was dead.
    load_by_path = None  # type: ignore[assignment]


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


def policy_line(
    source_root: Path = REPO_ROOT, target: Path = POLICY_TARGET, latest: str = ""
) -> str:
    """The branch-policy half; "" when there is nothing to say.

    The global hooks are a *copy* of `scripts/git_policy.py`, so they go stale
    invisibly: the hooks keep firing, they just enforce an older policy. Nothing
    else in the workspace would ever mention it, which is how a runtime came to be
    missing its escape hatch for two days while the source had it -- the only
    symptom was an env var that appeared to do nothing.

    Two different questions, because they have different answers. *Modified* means
    the installed bytes no longer match the receipt, which should never happen and
    is worth saying loudly. *Behind* means it was installed from an older release,
    which is ordinary and just wants a re-run.

    Neither is a comparison against the working tree, so editing `git_policy.py` on
    a branch stays silent -- the check would otherwise warn continuously while the
    policy is being worked on, and a line that always warns is one nobody reads.

    Spawn-free, per this file's contract: the receipt carries hashes so verifying
    it costs a read, and `latest` is resolved by the caller from the ref store.
    Silent when nothing is installed (a fresh clone, CI, anyone else's machine)
    and silent on any error.
    """
    if load_by_path is None or not target.is_dir():
        return ""
    try:
        installer = load_by_path(
            "_install_git_policy", source_root / "scripts" / "install-git-policy.py"
        )
        receipt = installer.read_receipt(target)
        drifted = installer.compare_install(target, receipt)
        behind = installer.behind_ref(receipt, latest)
    except Exception:
        return ""
    parts = []
    if drifted:
        parts.append(", ".join(f"{d.name} {d.reason}" for d in drifted))
    if behind:
        parts.append(f"installed from {behind}, {latest} available")
    if not parts:
        return ""
    return (
        f"branch policy: {'; '.join(parts)} "
        f"(fix: python devkit/scripts/install-git-policy.py --yes)"
    )


def render(
    results: list[sweep.Result], behind: dict[str, str], latest: str, policy: str = ""
) -> str:
    """The whole message, or "" when there is nothing worth saying."""
    halves = (stranded_line(results), behind_line(behind, latest), policy)
    lines = [line for line in halves if line]
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
        message = render(results, behind, latest, policy_line(latest=latest))
    except Exception as exc:
        print(f"[workspace] status unavailable ({type(exc).__name__})", file=sys.stderr)
        return 0
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
