# devkit

A portable agent-coding harness for **Claude Code / Codex**: the project-agnostic
hook scripts (auto-lint-on-edit, capped Bash, pre-stop PR-gate verification), the
session lifecycle, and the `.claude → .agents/.codex` sync tooling — **vendored into
each project** and configured per-project through `.agent-harness.toml`.

One source of truth, tested in isolation, pulled into every repo. No submodule: each
project commits its own copy, so cloning a single project still gets everything.

> **Renamed from `agent-harness` on 2026-07-25.** The repo is being widened into a
> five-channel upstream (agent plugin, pip package, reusable CI workflows, pre-commit
> hooks, and this vendored tier). Only the vendored tier — everything described below —
> exists today; the rest is planned.
>
> The **internal** names still use the old spelling on purpose: `.agent-harness.toml`,
> `$AGENT_HARNESS_DIR`, `HARNESS_VERSION`, `sync-harness.py`. Renaming those means moving
> `MANIFEST` paths in lockstep across every consuming repo, so it is a deliberate separate
> migration — not something to do piecemeal. Until it happens, **use the old names**; they
> are what the code reads.

## How it works

- **This repo is the source of truth.** Each consuming project commits a *vendored
  copy* of the files in [`scripts/sync-harness.py`](scripts/sync-harness.py)'s
  `MANIFEST`.
- **Everything project-specific lives in `.agent-harness.toml`** at the consuming
  repo's root, read by `scripts/hooks/harness_config.py` (stdlib `tomllib`; a
  missing/bad manifest falls back to neutral defaults). The scripts stay
  shape-agnostic — a new project drops in a manifest instead of forking the code.
- The **canonical example** manifest is
  [`templates/core/dot-agent-harness.toml.tmpl`](templates/core/dot-agent-harness.toml.tmpl),
  which is what a new project is rendered with. The `.agent-harness.toml` in *this*
  repo used to serve that role by holding a copy of carameli's; it now describes
  **devkit**, because devkit runs these hooks on itself and a hook reading another
  project's shape acts on directories that are not here.

## devkit runs its own harness

Everything devkit ships is wired up here, on itself — `.claude/settings.json` fires
the same hook set the generator emits, against devkit's own scripts.

| Utility | Wired by |
| --- | --- |
| SessionStart provisioning | `.claude/hooks/session-start.sh` (uv-native: `pyproject.toml` + `uv.lock`) |
| Branch-per-task | `scripts/hooks/branch-per-task.py` |
| Auto-lint on edit | `scripts/hooks/lint-fix.py` |
| Pre-stop verification | `scripts/hooks/stop.py` → `scripts/lint-all.py`, both test trees |
| Failure artifacts | `logs/lint-errors.log`, `logs/test-failures.log` |
| VS Code tasks | `.vscode/tasks.json` |

Not decoration — a hook that only runs downstream is a hook nobody tests. Wiring
these up surfaced four bugs that had shipped to every consumer: the Stop hook passed
`--no-secrets` to a lint runner that rejected it (argparse exit 2, so Tier 1 failed on
*every* stop in *every* generated project), it invoked a `check-lock-markers.py` no
generated project has, it treated pytest's "no tests collected" as a failure, and with
`[db] enabled = false` it never ran the project's own test suite at all.
`tests/test_self_hosting.py` is what keeps devkit from drifting back into shipping a
utility it does not use.

## Consuming it in a project

```bash
# One-time bootstrap: grab the sync tool, then pull everything it lists.
# NB: raw.githubusercontent.com does NOT follow the rename redirect — this URL
# must say devkit, even though the file it fetches is still sync-harness.py.
curl -sSfL https://raw.githubusercontent.com/alexandrec90/devkit/main/scripts/sync-harness.py \
  -o scripts/sync-harness.py
AGENT_HARNESS_DIR=/path/to/devkit python scripts/sync-harness.py --pull

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

## Creating a new project

`scripts/new-project.py` renders a whole project from `templates/` — the harness
seam, a Docker stack on registry-allocated ports, a parallel worktree, VS Code
tasks, and a PR gate whose drift check actually gates — instead of copying whichever
existing repo was nearest.

```bash
# Dry run is the DEFAULT: prints every file and command, writes nothing.
python scripts/new-project.py sports_betting --preset data --description "..."

# Apply. --no-remote stops before the GitHub repo is created.
python scripts/new-project.py sports_betting --preset data --yes
```

There is also a VS Code task, **"Project: New from devkit"**, in two places for two
different reasons. The user-level copy in `%APPDATA%/Code/User/tasks.json` is callable
from any window — which matters because the project it creates has no
`.vscode/tasks.json` yet. devkit's own `.vscode/tasks.json` carries it too, alongside
the lint/test/format tasks, so a session already open in this repo does not need to
leave it.

| Preset | Features | Shaped like |
| --- | --- | --- |
| `bare` | none — harness + CI only | — |
| `service` | docker, app, postgres, alembic | — |
| `service-redis` | + redis | — |
| `data` | docker, postgres, archive seam | `ibkr_trader` |
| `fullstack` | + redis, frontend | `carameli` |

Individual `--with-*` flags add to a preset; they never subtract.

Everything local and reversible happens before the two outward-facing steps
(creating the GitHub repo, pushing). A failure before that leaves a directory you
can delete.

### Host ports: `ports.toml`

Each checkout — a project or one of its worktrees — owns one integer **slot**, and
every published port is `conventional_base + slot`. Slot 0 gets the familiar
defaults (Postgres 5432, Vite 5173); every other checkout is a uniform offset.

This replaces prose in per-repo READMEs, which had already drifted: carameli's README
prescribes `DB_HOST_PORT=5433` for its `-b` worktree while the real `.env` uses
`5434`, and following the README to the letter would have collided with
`ibkr_trader`'s hardcoded `5433`. `validate()` rejects duplicate slots and
insufficiently-spaced service bases rather than letting either reach
`docker compose up`, where it surfaces only as "port is already allocated".

```bash
python scripts/devkit_ports.py                 # the whole registry
python scripts/devkit_ports.py carameli-b      # one checkout's *_HOST_PORT block
```

The generator **does not** edit `ports.toml` itself — it prints the lines to add.
devkit is a git repo with its own gate, and a tool that silently commits to its own
source of truth is how two sessions hand out one slot twice.

### The two test trees

| Tree | Vendored? | Must be project-agnostic? |
| --- | --- | --- |
| `scripts/hooks/tests/` | Yes — in the `MANIFEST` | **Yes.** It runs inside every consuming repo, against that repo's `.agent-harness.toml`. |
| `tests/` | No | No. Generator, port registry, renderer — devkit-only. |

That distinction was violated for a while and it mattered: the vendored tests pinned
carameli's literal credentials, paths, env prefix, and skill list, so **every
generated project failed 12 of them on its first CI run**, and no other repo could
have adopted the harness. They now derive those values from `CFG` and skip tiers a
project does not have. CI's `generated-project` job renders a project of each preset
and runs its suites, because devkit's own suite passes precisely when devkit's
manifest is the one being hard-coded against.

## Scope note

The current `MANIFEST` is the reviewed, coupling-free core (config loader + Stop
dispatcher, the lint-fix PostToolUse hook, the known-fixes normalizer, the sync tool).
The branch-lifecycle scripts (`branch-per-task`, `session-sync`, `session-start.sh`)
are **not yet vendored** — they hardcode the default branch `master` and need that
lifted out (to `git symbolic-ref` or a manifest field) before they are portable.
