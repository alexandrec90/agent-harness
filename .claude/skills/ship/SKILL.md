---
name: ship
disable-model-invocation: true
description: 'Ship the current task: lint pre-flight, commit, push, open a PR to the default branch, and start the autofix loop. Use when a task is complete and the user asks to ship / open a PR for the current change.'
argument-hint: 'Optional PR title'
---

# Skill: Ship the current task

Takes the finished work on the current feature branch all the way to an open PR
against the repo's default branch, then hands the PR to the CI-autofix loop. Explicit-invocation
only (`/ship`) — it pushes and opens a PR, so it must never fire mid-task. The
branch was created at the start of the task — by `/task` (worktrees) or the
branch-per-task hook (primary checkout on the default branch).

Run the steps in order. Stop and report if a step fails — do not open a PR from
a branch that failed pre-flight.

## Step 1 — Preflight (fail fast)

```bash
python scripts/ship.py --preflight
```

Exit `3` means you are on the default branch or detached HEAD — there is nothing to ship
from. Report that and stop; the work needs to be on a `claude/...` branch first.

## Step 2 — Confirm tests

The Stop hook already reproduces the PR-gate checks on the diff, and Step 4's
`ship.py` runs the changed-scope lint gate before it pushes — so do **not** run
`lint-all.py` here (that would just repeat Step 4's pass). Instead, make sure the
code you touched has tests and they pass (project rule: tests ship in the same
commit) — run the targeted suite for what you changed.

## Step 3 — Commit

Stage the change and write a clear, descriptive commit message (imperative
subject, a body explaining the why). Include the project's commit footers.

```bash
git add -A
git commit -F - <<'EOF'
<subject>

<body>
EOF
```

## Step 4 — Push (lint gate + retry)

```bash
python scripts/ship.py
```

This re-asserts the branch, requires a clean tree (so commit first), runs the
changed-scope lint gate, and pushes with network-error backoff. Non-zero exit
codes: `4` dirty tree, `5` lint failed (see `logs/lint-errors.log`), `6` push
failed. Resolve and re-run.

This step does **not** arm the shipped marker — that happens in Step 6, *after*
the PR is open. If a run dies between here and the PR, nothing is orphaned:
re-running `/ship` on the pushed branch no-ops the push and continues to the PR
(the whole skill is idempotent/resumable from any step).

## Step 5 — Open the PR

Check for a PR template first (`.github/pull_request_template.md` and the other
standard locations). Create the PR against the default branch with the GitHub MCP tool,
filling the template if one exists:

- `mcp__github__create_pull_request` — base = the default branch, head = current branch,
  title from the argument or the commit subject, body summarizing the change.

Do **not** create the PR if any earlier step failed.

## Step 6 — Arm the next task branch (only after the PR is open)

```bash
python scripts/ship.py --mark-shipped
```

Run this **only after Step 5 succeeded** and you have a PR URL. It drops the
per-worktree shipped marker so your next prompt auto-starts a fresh task branch
(no `/task` needed). Arming it here — not at push time — is deliberate: if the
marker were set at push and the PR step failed, the next prompt would branch
away and silently orphan the pushed branch. If Step 5 did not produce a PR, skip
this — leave the branch un-marked so the work isn't abandoned.

## Step 7 — Start the autofix loop

Ask the user whether to watch the PR (unless they already said to). If yes:

- `subscribe_pr_activity` for the new PR — CI failures and review comments then
  wake this session and you autofix per the PR-activity workflow.

This repo has no branch protection; `dependabot-automerge.yml` merges once the
PR Gate passes. If the user wants hands-off merge, enable it with
`mcp__github__enable_pr_auto_merge`.

**Cost note (autofix loop).** The autofix loop is the expensive part of the
workflow — each round wakes a fresh turn that reloads context, and it runs in
parallel across every open PR. Keep it cheap:

- Batch *all* failures from one PR-Gate run into a single fix + push (each push
  re-runs the full gate).
- Read the filtered artifact (`logs/lint-errors.log`, `logs/test-failures.log`),
  never the raw CI job log.
- **Default a mechanical-fix round to a lower model/effort.** When the failure is
  clearly mechanical (a lint nit, a missing import, a snapshot/format update),
  run that autofix turn at a cheaper model — don't reserve Opus/high for it.
  Escalate back to the capable model only when a failure needs real diagnosis.
  See `.claude/rules/tooling.md` (CI feedback loop) for the full rationale.

## Report

Reply with the PR URL, its number, and whether the autofix subscription is
active. That PR URL is the deliverable.
