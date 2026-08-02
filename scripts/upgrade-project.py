#!/usr/bin/env python3
"""Adopt a devkit release in a consuming project, as one reviewable change.

A devkit upgrade moves four things that describe one upstream revision: the
vendored files, the `DEVKIT_VERSION` stamp, the `rev:` in the project's
`.pre-commit-config.yaml`, and the `ref:` in its PR gate. `sync-devkit.py --pull`
now moves all four atomically or refuses; this script is what puts the result on
its own branch and into its own PR.

**It is deliberately not part of shipping.** A harness upgrade is dozens of files
of upstream churn, and folding it into `/ship` or `sweep --ship` would mix it into
whatever change was actually being shipped -- which is how a consumer ended up with
an unfinished upgrade buried in 364 uncommitted files, discovered only when its own
commit gate refused. An upgrade is its own operation with its own diff.

Refuses a dirty target for the same reason: a branch cut under uncommitted work
carries that work along, and the commit here would have to guess which files were
part of the upgrade.

Pure and stdlib-only; every decision is an importable function tested in
`tests/test_upgrade_project.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = REPO_ROOT.parent / "alex-projects.code-workspace"
SYNC_SCRIPT = "scripts/sync-devkit.py"

# The per-project files an upgrade moves besides the MANIFEST itself. Shown in the
# dry run so the plan names them; the commit stages with `add -A`, which is exact
# rather than lax because `refusal()` guarantees a clean tree beforehand.
UPGRADE_PATHS: tuple[str, ...] = (
    "DEVKIT_VERSION",
    "DEVKIT_FILES.json",
    ".pre-commit-config.yaml",
    ".github/workflows/pr-gate.yml",
)


def branch_name(today: _dt.date | None = None) -> str:
    """The branch an upgrade lands on: `claude/devkit-upgrade-<mmdd>`."""
    return tb.branch_name(tb.slugify("devkit upgrade"), set(), today)


def commit_message(tag: str, files: int) -> str:
    """Subject for the upgrade commit. Names the release, because for this one
    change the version *is* the description -- unlike a swept commit, nothing here
    is a guess about content."""
    return f"Adopt devkit {tag} ({files} vendored file(s))"


def pr_body(tag: str, previous: str, changed: list[str]) -> str:
    """PR body: what moved, and the one thing a reviewer has to check."""
    lines = [
        f"Adopts devkit **{tag}** (was `{previous}`), via `{SYNC_SCRIPT} --pull`.",
        "",
        "The four things that describe the vendored revision move together, so the "
        "commit-time drift gate and the PR gate now measure against the same tag:",
        "",
        "- vendored files from the `MANIFEST`",
        "- `DEVKIT_VERSION`",
        "- `.pre-commit-config.yaml` → `rev:`",
        "- `.github/workflows/pr-gate.yml` → the harness checkout `ref:`",
        "",
        "Review this as an upstream adoption, not as authored work: the file "
        "contents come from devkit and belong upstream if they are wrong.",
    ]
    if changed:
        lines += ["", f"Changed paths ({len(changed)}):", ""]
        lines += [f"- `{path}`" for path in changed[: sweep.PR_BODY_FILE_LIMIT]]
        if len(changed) > sweep.PR_BODY_FILE_LIMIT:
            lines.append(f"- …and {len(changed) - sweep.PR_BODY_FILE_LIMIT} more")
    return "\n".join(lines)


def refusal(state: sweep.State, tag: str | None) -> str:
    """Why this project cannot be upgraded right now, or "" when it can.

    Ordered by what the operator has to do about it, cheapest first.
    """
    if not state.is_git:
        return "not a git checkout"
    if not tag:
        return (
            "devkit HEAD is not tagged -- there is no release to adopt. "
            "Cut a tag in devkit first (see RELEASING.md)"
        )
    if state.dirty:
        return (
            f"{state.dirty} uncommitted file(s). An upgrade is its own change; "
            f"commit or ship the work in progress first"
        )
    if sweep.is_task_branch(state.branch):
        return (
            f"already on the task branch {state.branch}. Upgrade from the home "
            f"branch so the adoption is not mixed into unrelated work"
        )
    return ""


def plan(state: sweep.State, tag: str | None, today: _dt.date | None = None) -> sweep.Plan:
    """The git steps for one project's upgrade, or a refusal.

    The `--pull` itself is not a git step, so it is not in `steps`: it runs between
    the branch and the commit, and the commit is only meaningful if it succeeded.
    """
    reason = refusal(state, tag)
    if reason:
        return sweep.Plan(refusal=reason)
    return sweep.Plan(steps=(("checkout", "-b", branch_name(today)),), anchor=state.branch)


def changed_paths(git) -> list[str]:
    """Everything the pull touched.

    `refusal()` guarantees the tree was clean before the pull ran, so every dirty
    path afterwards came from it. That precondition is what lets the commit below
    stage with `add -A` instead of guessing at a path list -- and it is why the
    clean-tree refusal is a correctness requirement, not politeness.
    """
    result = git("status", "--porcelain")
    return list(sweep.parse_porcelain(result.stdout if result.returncode == 0 else ""))


def run_pull(project: Path, devkit: Path) -> subprocess.CompletedProcess[str]:
    """`sync-devkit.py --pull` inside `project`, sourced from `devkit`.

    Runs the *project's own* vendored copy: it is the one whose MANIFEST describes
    what that project has, and an older copy upgrading itself is the normal case.
    """
    return subprocess.run(
        [sys.executable, SYNC_SCRIPT, "--pull", "--src", str(devkit)],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project", help="checkout name to upgrade, as listed in the workspace")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--devkit", type=Path, default=REPO_ROOT, help="devkit checkout to pull from"
    )
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    apply_mode.add_argument("--yes", dest="dry_run", action="store_false")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.workspace.is_file():
        print(f"upgrade: no workspace file at {args.workspace}", file=sys.stderr)
        return 2
    names = sweep.parse_workspace(args.workspace.read_text(encoding="utf-8"))
    if args.project not in names:
        print(
            f"upgrade: {args.project} is not in {args.workspace.name}. "
            f"Known checkouts: {', '.join(names)}",
            file=sys.stderr,
        )
        return 2

    project = args.workspace.parent / args.project
    state = sweep.inspect(args.project, project, fetch=False)
    tag = _devkit_tag(args.devkit)
    upgrade = plan(state, tag, None)

    if upgrade.refusal:
        print(f"upgrade: {args.project} -- {upgrade.refusal}", file=sys.stderr)
        return 1

    previous = (project / "DEVKIT_VERSION").read_text(encoding="utf-8").strip()
    print(f"upgrade: {args.project} {previous} -> {tag}")
    for step in upgrade.steps:
        print(f"  1. git -C {args.project} {' '.join(step)}")
    print(f"  2. {SYNC_SCRIPT} --pull --src {args.devkit}")
    print(f"  3. git -C {args.project} add {' '.join(UPGRADE_PATHS)} + the MANIFEST paths")
    print(f"  4. git -C {args.project} commit -m {commit_message(tag or '', 0)!r}")
    print("  5. git push -u origin, then gh pr create")
    if args.dry_run:
        print("\nDry run -- nothing was changed. Re-run with --yes to apply.")
        return 0

    git = sweep.git_for(project)
    applied = sweep.apply_plan(args.project, project, upgrade, git=git)
    if not applied.ok:
        print(f"upgrade: FAILED at `{applied.failed}`\n{applied.error}", file=sys.stderr)
        return 2

    pulled = run_pull(project, args.devkit)
    print(pulled.stdout.rstrip())
    if pulled.returncode != 0:
        print(pulled.stderr.rstrip(), file=sys.stderr)
        print("upgrade: the pull refused; the branch is cut but empty.", file=sys.stderr)
        return 2

    changed = changed_paths(git)
    if not changed:
        print("upgrade: already current -- nothing to commit.")
        return 0

    for step in (
        # Safe only because the tree was clean before the pull -- see `changed_paths`.
        ("add", "-A"),
        ("commit", "-m", commit_message(tag or "", len(changed))),
        ("push", "-u", "origin", branch_name()),
    ):
        result = git(*step)
        if result.returncode != 0:
            print(f"upgrade: FAILED at `git {' '.join(step)}`", file=sys.stderr)
            print((result.stderr or result.stdout).rstrip(), file=sys.stderr)
            return 2

    url, created, error = sweep.ensure_pr(
        sweep.gh_for(project),
        sweep.Plan(
            pr_title=commit_message(tag or "", len(changed)),
            pr_body=pr_body(tag or "", previous, changed),
            pr_head=branch_name(),
            pr_base=state.default_branch,
        ),
    )
    if error:
        print(f"upgrade: pushed, but the PR failed: {error}", file=sys.stderr)
        return 2
    print(f"upgrade: PR {'opened' if created else 'already open'}: {url}")
    return 0


def _devkit_tag(devkit: Path) -> str | None:
    """The tag on the devkit checkout's HEAD -- the release being adopted."""
    result = subprocess.run(
        ["git", "-C", str(devkit), "describe", "--tags", "--exact-match", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


if __name__ == "__main__":
    sys.exit(main())
