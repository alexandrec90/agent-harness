---
description: Baseline engineering policy shared by every devkit project — test coverage, script conventions, the vendored harness seam, and the instruction-feedback loop
---

# Rule: Baseline engineering policy

Deliberately **unscoped** (no `paths:`) — this is the small set of rules that hold
everywhere, so there is no glob that should exempt a file from them.

**This file is vendored from devkit and is byte-identical in every project.** It is in
`sync-harness.py`'s `MANIFEST`, so a local edit is reported as drift by the PR gate
rather than quietly becoming this project's private opinion. That is the point: these
paragraphs previously lived inline in each repo's `CLAUDE.md`, were copied forward by
hand, and drifted — devkit's own template had already lost a clause carameli still
had. To change the policy, change it here and let projects `--pull`.

A project's `CLAUDE.md` should **point at this file, not restate it.** A restatement is
a fork: it looks authoritative, it is not gated, and the two copies disagree the first
time either is edited.

## Testing

Every code change must include tests in the same commit. Every endpoint and every
testable unit of logic must have test coverage — gaps are not acceptable. If you add
or touch something that has no test, write the test in the same commit even if the
logic itself didn't change.

- **New unit of logic:** cover the happy path, the error cases, and the edge cases.
- **Bug fix:** write the regression test first, and watch it fail before you fix it. A
  regression test that has never failed is asserting the wrong thing.
- **Run targeted tests** to verify a change — the module you touched — plus the
  linter. Leave full-suite runs to CI: they are slow, and a fresh-venv full run
  surfaces version-skew failures that have nothing to do with your change.
- **Fix failures in the code, not in the assertion.** Relaxing an assertion to get
  green deletes the only evidence that something is wrong.
- A skipped or `xfail` test carries a linked issue or a one-line reason in the marker.

> If the local toolchain or stack isn't available, still write the required tests in
> the same change and leave execution to CI. "I couldn't run it" is a reason to defer
> the run, never a reason to skip writing it.

Instruction files — `CLAUDE.md`, `.claude/rules/*`, `.claude/skills/*` — are covered by
this same mandate. See `.claude/rules/authoring.md`.

## Scripts

All scripts under `scripts/` are Python, for cross-environment compatibility (a local
desktop and a CI runner are rarely the same OS).

- **Expose pure importable functions** guarded by `if __name__ == '__main__'`, so the
  logic can be tested without spawning a subprocess.
- **Every new script ships with its tests in the same change.**
- **Hook scripts (`scripts/hooks/`) are stdlib only** — no third-party imports. Hooks
  run before the virtualenv is active, so an import of anything installed is a crash
  in the one context that cannot report it well.
- **Side effects live behind `main()`**, never at import time: the test suite imports
  these modules.

### Failure artifacts — fix from a file, not from the terminal

Any task or script whose failures an agent is expected to act on must persist those
failures to a **parseable artifact file** under `logs/`. Never rely on streamed
terminal output — it scrolls away and buries the signal. Keep the terminal to a status
line plus the artifact path, and put everything needed to diagnose in the file. Write
the artifact on failure too, not only on success, and overwrite it per run.

## The vendored agent harness

The hook scripts, this rule, and the shared skills are **vendored from devkit, which is
the source of truth**. Each project commits its own copy, so a fresh clone gets
everything with no submodule and no install step.

- Everything project-specific lives in `.agent-harness.toml`, read by
  `scripts/hooks/harness_config.py`. **Never hard-code project specifics in a vendored
  file**: a new behaviour gets a manifest field and a default, not an `if project ==`
  branch, and not a paragraph that names one repo's paths.
- `python scripts/sync-harness.py --check` fails on drift, `--pull` adopts upstream,
  `--push` sends a change authored here back up. `HARNESS_VERSION` records which
  upstream commit the vendored copy corresponds to.
- **Every mode no-ops clean (exit 0) when `$AGENT_HARNESS_DIR` is unset.** That is
  correct before adoption and a trap after: if `--check` ever prints "nothing to do
  (skipping)" in CI, the gate is inert — fix the wiring, don't ignore it.
- A vendored script may depend on a file the project owns (`lint-all.py`,
  `run-tests.py`). Those dependencies are asserted by
  `scripts/hooks/tests/test_repo_contract.py`, because at runtime a missing one is a
  silent skip by design — the gate reports green having run nothing.

## Guardrail: the instruction-file feedback loop

If an instruction in a skill, a rule, or a `CLAUDE.md` sent you into a dead end or a
wasted operation — or a mistake you made would have been prevented by one that isn't
there — flag it in your report with the file, the line, and a proposed edit.

**Never silently work around a bad instruction.** Working around it fixes your current
turn and leaves the next agent to hit the same wall; the instruction files only improve
if the failures they cause are reported as defects in them.
