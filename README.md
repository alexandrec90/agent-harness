# agent-harness

A portable agent-coding harness for **Claude Code / Codex**: the project-agnostic
hook scripts (auto-lint-on-edit, capped Bash, pre-stop PR-gate verification), the
session lifecycle, and the `.claude → .agents/.codex` sync tooling — **vendored into
each project** and configured per-project through `.agent-harness.toml`.

One source of truth, tested in isolation, pulled into every repo. No submodule: each
project commits its own copy, so cloning a single project still gets everything.

## How it works

- **This repo is the source of truth.** Each consuming project commits a *vendored
  copy* of the files in [`scripts/sync-harness.py`](scripts/sync-harness.py)'s
  `MANIFEST`.
- **Everything project-specific lives in `.agent-harness.toml`** at the consuming
  repo's root, read by `scripts/hooks/harness_config.py` (stdlib `tomllib`; a
  missing/bad manifest falls back to neutral defaults). The scripts stay
  shape-agnostic — a new project drops in a manifest instead of forking the code.
- The `.agent-harness.toml` in *this* repo is the **canonical example** (and what the
  vendored test-suite is calibrated against).

## Consuming it in a project

```bash
# One-time bootstrap: grab the sync tool, then pull everything it lists.
curl -sSfL https://raw.githubusercontent.com/<owner>/agent-harness/main/scripts/sync-harness.py \
  -o scripts/sync-harness.py
AGENT_HARNESS_DIR=/path/to/agent-harness python scripts/sync-harness.py --pull

# Add a .agent-harness.toml (see this repo's as the template), then commit.
```

- `--check` (default): fail on drift — wire into CI. **No-ops when
  `$AGENT_HARNESS_DIR`/`--src` is unset**, so CI is green before adoption.
- `--pull`: adopt this repo's version (stamps `HARNESS_VERSION` with the commit).
- `--push`: copy a project's version back here (author a change / seed a fresh repo).
- `--list`: print the manifest + the project's vendored version.

## Authoring changes

The harness repo is the source of truth. Edit here, open a PR, let CI test it, merge.
Projects then `--pull`. Only `--push` from the one project actively authoring a change.

## Scope note

The current `MANIFEST` is the reviewed, coupling-free core (config loader + Stop
dispatcher, the lint-fix PostToolUse hook, the known-fixes normalizer, the sync tool).
The branch-lifecycle scripts (`branch-per-task`, `session-sync`, `session-start.sh`)
are **not yet vendored** — they hardcode the default branch `master` and need that
lifted out (to `git symbolic-ref` or a manifest field) before they are portable.
