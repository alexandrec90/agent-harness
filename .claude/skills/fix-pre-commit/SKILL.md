---
name: fix-pre-commit
disable-model-invocation: true
description: 'Fixes pre-commit hook errors collected in logs/pre-commit-errors.log.'
argument-hint: '(no arguments)'
---

# Skill: Fix Pre-Commit Errors

> Depends on the local git hooks / pre-commit run being available.

Fix hook errors collected in `logs/pre-commit-errors.log`.

---

## Step 1 — Collect Errors

Read `logs/pre-commit-errors.log` with the Read tool. If the file does not exist or is empty,
regenerate it by running the pre-commit hooks yourself (attempting a commit also writes the
artifact via the git hook). If you can't run them in this environment, say so and stop.

The file contains the output of failed pre-commit hooks. Each failed hook is identified by a
line ending in `Failed` (e.g. `ruff......Failed`). The actionable error output follows
immediately after each failed hook header until the next hook header or end of file.

Build a triage list grouping errors by hook:

| # | Hook | File:line | Error summary |
|---|------|-----------|---------------|
| 1 | ruff | app/main.py:12:1 | F401 unused import |
| 2 | bandit | app/core/auth.py:30 | B105 hardcoded password |
| 3 | detect-secrets | .env.example:5 | Hex high-entropy string |

### Hook-specific parsing

- **ruff**: lines matching `file:line:col: CODE message`
- **ruff-format**: files listed as reformatted (already auto-fixed — note and move on)
- **bandit**: `>> Issue: [CODE] message` + `Location: file:line:col` pairs
- **detect-secrets**: lines referencing file paths with potential secrets
- **eslint**: lines matching `file  line:col  error|warning  message  rule`
- **stylelint**: lines matching `file:line:col  error|warning  message`
- **markdownlint**: lines matching `file:line:col CODE/message`
- **dotenv-linter**: lines referencing `.env` files with the violation description

### Log quality gate

After parsing, check the triage list for these signals of incomplete diagnostics:

| Signal | What it means |
|---|---|
| A hook listed as `Failed` has no error lines in the triage list (empty block) | The hook's output wasn't captured — root cause is invisible |
| Error lines from `ruff`, `bandit`, `mypy`, or `eslint` lack a `file:line` reference | Not self-locating — the agent cannot find the source file |
| Lines under a `Failed` hook are only `Fixing <file>` reformatter output | Auto-fixed by the hook — should be classified as resolved, not failed |
| A hook's block is flooded by one non-source file (e.g. a captured transcript under `artifacts/transcripts/`) burying real errors | Noise — that file shouldn't be linted; the actionable errors are unfindable |

If **any** quality problem is found (missing detail **or** noise):

1. Identify which hook(s) are affected.
2. Update the producing pre-commit runner (and its test): fix the capture/classification when
   detail is missing (redirect hook stderr, filter `Fixing ...` lines), or narrow the hook's
   target when noise floods it (exclude the offending file/glob from that hook's config).
3. Note what was wrong and what you changed, then regenerate the log (re-run the hooks).
4. **Stop** — do not attempt fixes on a low-quality log, in either direction.

---

## Step 2 — Apply Fixes

For each error:

1. Open the relevant file and read enough context to understand the cause.
2. Apply the **smallest reasonable fix** — no refactors, no unrelated cleanup.
3. Preserve all existing `logger.*` calls; add any that are missing per
   `.claude/rules/logging.md`.
4. If a fix requires a DB schema change, note it and stop — use `/add-db-model` instead.

### Hook-specific guidance

- **ruff / eslint / stylelint / markdownlint**: straightforward code fixes.
- **ruff-format**: if the hook already auto-fixed files, just note them as resolved.
- **bandit**: security findings — apply the safe fix (e.g. replace hardcoded secret with
  env var lookup). If the finding is a false positive, add a `# nosec BXXX` comment with
  a brief justification.
- **detect-secrets**: if the finding is a real secret, remove it and use an env var. If it
  is a false positive, run:

  ```sh
  detect-secrets scan > .secrets.baseline
  ```

  to update the baseline,
  or add an inline `# pragma: allowlist secret` comment with justification.
- **dotenv-linter**: fix formatting/ordering issues in `.env` files.

**Stop conditions:**

- A fix would require a non-trivial refactor — propose a minimal safe fix and ask for
  confirmation.
- Required context is missing — ask a single clarifying question and stop.

---

## Step 3 — Verify

Regenerate the log (re-run the hooks, or attempt a commit) and re-enter to catch any newly
surfaced errors. Repeat until `logs/pre-commit-errors.log` is empty.

Diagnose from `logs/pre-commit-errors.log`. After a fix you may run the single hook you addressed
(`pre-commit run <hook-id> --files <file>`) to confirm it passes — reserve a full re-run for the
once-per-pass regenerate-and-loop.

---

## Step 4 — Report

State clearly:

- Which errors were fixed (hook, file, line, what changed).
- Which were skipped and why.
- Next step: regenerate the log (re-run the hooks) if fixes were applied.

---

## Hard Rules

1. Edit only files directly implicated by the collected errors — never pre-emptive cleanup.
2. After a fix, run at most the single hook you addressed to verify — don't re-run the full
   hook suite per edit or dump raw output.
3. One error = one minimal fix. Do not restructure surrounding code.
4. **Log quality gate is mandatory (both directions).** If any `Failed` hook has no captured
   error lines, *or* one non-source file floods a hook's block and buries real errors, update
   the producing pre-commit runner (and its test) and stop — never fix by hand from a bad log.
