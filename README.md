# devkit

A portable agent-coding harness for **Claude Code / Codex**: the project-agnostic
hook scripts (auto-lint-on-edit, capped Bash, pre-stop PR-gate verification), the
session lifecycle, and the `.claude → .agents/.codex` sync tooling — **vendored into
each project** and configured per-project through `.agent-harness.toml`.

One source of truth, tested in isolation, pulled into every repo. No submodule: each
project commits its own copy, so cloning a single project still gets everything.

> **Renamed from `agent-harness` on 2026-07-25.** The repo is being widened into a
> five-channel upstream. Two exist today — the **vendored tier** (everything described
> below) and the **[pre-commit hooks](#pre-commit-hooks-a-second-channel)**. The agent
> plugin, pip package, and reusable CI workflows are still planned.
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

## Pre-commit hooks: a second channel

devkit publishes pre-commit hooks in
[`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml). Unlike the vendored tier there is
nothing to copy in — a consumer pins a rev, and pre-commit clones it:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/alexandrec90/devkit
    rev: v0.5.0 # a tag, never a branch — see below
    hooks:
      - id: agent-harness-manifest
      - id: harness-hooks-stdlib-only
      - id: harness-drift
```

`scripts/new-project.py` renders this into every new project already, pinned to the same
devkit ref as the PR gate.

| Hook | Catches |
| --- | --- |
| `agent-harness-manifest` | A `.agent-harness.toml` the harness would silently ignore: unparseable TOML, a path prefix missing its trailing slash, a declared directory that does not exist in the repo, a `[db]`/`[frontend]` block switched on and left half-filled. |
| `harness-hooks-stdlib-only` | A third-party import in `scripts/hooks/`. Those scripts run *before* the virtualenv exists, so this cannot be caught by a test suite — which runs inside it. |
| `harness-drift` | A vendored file that differs from the pinned devkit rev. |

**Why `harness-drift` exists next to `sync-harness.py --check`.** The sync tool resolves
its source from `$AGENT_HARNESS_DIR` and **exits 0 doing nothing when that is unset** —
correct before adoption, an inert gate afterwards, and indistinguishable from success in a
log. Run through pre-commit there is nothing to configure: pre-commit has already cloned
devkit at the pinned rev, so the version being compared against is written down in the
consumer's config and moved by `pre-commit autoupdate`.

Two consequences worth knowing:

- **The hooks are `language: script`, not `language: python`.** devkit is a virtual
  project with nothing to install, and these scripts are stdlib-only, so the clone is
  already everything they need. That also means the executable bit matters — a test
  enforces it, because a missing one fails only on a consumer's machine, at commit time,
  after the rev is tagged.
- **A rev that predates the channel fails hard.** pre-commit resolves hook ids strictly:
  against an older tag the consumer's first commit aborts with "hook not found" rather
  than skipping. `new-project.py` checks the ref it is about to pin and warns when it
  cannot serve the hooks.

devkit runs these on itself via [`.pre-commit-config.yaml`](.pre-commit-config.yaml),
wired as `repo: local` — pinning a rev there would validate a released tag's hooks against
the working tree trying to change them, so a hook fix could never be tested by the hook it
fixes. `.claude/hooks/session-start.sh` runs `pre-commit install` when a config is
present, so a fresh clone or sandbox gets the gate without anyone remembering to.

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

Note that devkit's own `.agent-harness.toml` is therefore a **test fixture**, not a
description of devkit: it turns on the DB and frontend tiers so the vendored suite
exercises them here. Checks that hold a repo to its manifest have to know that — see
below.

### The repo contract

`scripts/hooks/tests/test_repo_contract.py` (vendored) closes the gap the drift check
cannot see. `sync-harness.py --check` guarantees the `MANIFEST` files are *identical*
everywhere; it says nothing about the files they depend on. `stop.py` dispatches to
five sibling scripts that are **not** vendored with it, and at runtime a missing one
is a skip — deliberately, since a local tooling gap must never block the agent. That
is also why it is invisible: a project whose `lint-all.py` was never rendered has a
Stop gate that reports green having run nothing. The same shape shipped here once
already — `_REQ_RE` did not match `uv.lock`, so the lock-marker tier was inert in
every uv-native project and nothing looked broken.

The split is the point: **the runtime degrades quietly, CI is where that gets
noticed.** The contract asserts only what a repo's own config decides —

- the scripts a reachable tier needs exist (`lint-all.py` always; `finalize-state.py`
  once `[stop] finalize_targets` is non-empty);
- `[paths]` and `[frontend]` name directories that are actually there, since every
  tier selects by `startswith` and a stale prefix matches nothing, silently;
- the manifest has no unknown keys — `from_dict` is all `raw.get(name, default)`, so
  `db_servce` reads as "unset", and the tier quietly falls back to a default that
  does not match the compose file.

Everything gated on the repo actually wiring `stop.py` as a Stop hook, which is what
keeps devkit's fixture manifest from being held to devkit's files. Tiers whose script
is project-owned (`check-lock-markers.py`, whose sentinels name that project's own
lockfiles) stay optional and skip explicitly.

### The shared instruction tier

The same argument, applied to the prose that steers the agent rather than the code that
gates it. `.claude/rules/engineering.md` (testing, scripts, the harness seam, the
instruction-feedback loop), `.claude/rules/authoring.md`, and the project-agnostic
skills (`ship`, `task`, `retro`, `test-skill`, `audit-claude-md`, `audit-gitignore`,
`audit-dockerignore`) are in the `MANIFEST` and vendored byte-identical.

They were not, and it showed. Those paragraphs lived inline in each repo's `CLAUDE.md`
and were copied forward by hand: devkit's own template had already lost a clause of the
testing mandate — the sentence closing the "but I didn't change that function" loophole
— that carameli still had, and nothing could detect it. `ship` and `task` had `master`
written through them while `task_branch.detect_default_branch()` resolved the real
branch at runtime, so in every `main`-based project the prose contradicted the script.

**A project's `CLAUDE.md` cites these files; it does not restate them.** A restatement
is a fork — it reads as authoritative, it is not in the `MANIFEST`, and so it is the one
copy nothing drift-checks. `test_repo_contract.py` fails on a `CLAUDE.md` that reproduces
a vendored clause, matching on the distinctive middle of each rather than the whole
sentence, since a verbatim-only check passes the moment someone paraphrases — which is
how the drift happened the first time.

Only genuinely portable prose belongs here. A rule naming one project's services, paths,
or default branch is that project's own; vendoring it repeats the mistake that made every
generated project fail 12 tests on its first CI run. Carameli's `rules/testing.md` (DB
savepoint isolation, paid-provider markers) and its `skin-*` rules stay where they are.

> **Adopting this in an existing project takes two `--pull` runs.** The tool iterates the
> `MANIFEST` it was imported with, so the first pull installs the new `sync-harness.py`
> and the second is what actually copies the entries it added.

Two more skills (`plan-handoff`, `fix-pre-commit`, `refactor`) vendor their **prose
only**. Their sibling `known-fixes.md` / `state.json` are that repo's accumulated
learning — hit counts are what `normalize-known-fixes.py` prunes against — so vendoring
them byte-identical would reset every project's memory on each `--pull` and hand every
repo another repo's error patterns. The generator seeds them empty instead.

Still not vendored, each for a reason worth keeping: `fix-all`, `fix-lint` (they
dispatch to `fix-tests`/`fix-docker`/`fix-e2e`, which are not portable — a vendored
dispatcher whose children don't ship is a skill that dead-ends), `audit-deps` (written
against `requirements.in`/pip-tools; generated projects are uv-native), `check-boundaries`
(one project's layering), and `triage-fixers`/`gen-fixer-eval`/`fix-instructions`/
`optimize-fixers` (bound to a promptfoo `evals/` harness devkit does not ship).

### AGENTS.md and `.agents/` are generated, never written

`sync-agents-context.py` copies every `CLAUDE.md` to a sibling `AGENTS.md` and mirrors
`.claude/` to `.agents/`, for harnesses that read those paths; `sync-codex-hooks.py`
regenerates `.codex/hooks.json` from the `settings.json` hooks block, and only fires in a
repo that has a `.codex/` directory. Both are in the `MANIFEST`, and `new-project.py`
runs the mirror at creation, so a fresh project has its Codex-facing tree from the first
commit instead of acquiring one by hand later.

**Never edit `AGENTS.md` or `.agents/**`.** The mirror is only worth having while it is
byte-identical, and a hand-edit is silent in the worst way — both files read as
authoritative, nothing regenerates on read, and the two harnesses follow different rules
from that point on. `--pull` cannot catch it, since the mirror is per-project and not in
the `MANIFEST`, so `test_repo_contract.py` compares them directly instead.

Carameli's `test_codex_hooks_contract.py` stays in carameli: it pins that repo's exact
hook topology (`codex-session-start.py`, `enforce-capped-bash.py`), which is the coupling
this whole tier exists to avoid.

## Scope note

The current `MANIFEST` is the reviewed, coupling-free core (config loader + Stop
dispatcher, the lint-fix PostToolUse hook, the known-fixes normalizer, the sync tool).
The branch-lifecycle scripts (`branch-per-task`, `session-sync`, `session-start.sh`)
are **not yet vendored** — they hardcode the default branch `master` and need that
lifted out (to `git symbolic-ref` or a manifest field) before they are portable.
