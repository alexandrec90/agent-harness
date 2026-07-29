# devkit

The portable agent-coding harness for Claude Code / Codex, and the project generator
that ships it. This repo is the **source of truth**: consuming projects commit a
vendored copy of `scripts/sync-harness.py`'s `MANIFEST` and pull changes from here.

## Tech Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| Runtime dependencies | **none** — stdlib only, by contract (see below) |
| Tests | pytest |
| Lint | ruff + mypy |

There is no Docker stack, no database, and no frontend. That is what lets CI run with
no service containers, and it is why `.agent-harness.toml` declares `[db] enabled =
false` and `[frontend] enabled = false`.

## devkit runs its own harness

Everything devkit ships to other projects is wired up **here**, on itself:

| Utility | Wired by |
| --- | --- |
| SessionStart provisioning | `.claude/settings.json` → `.claude/hooks/session-start.sh` |
| Branch-per-task | `.claude/settings.json` → `scripts/hooks/branch-per-task.py` |
| Auto-lint on edit | `.claude/settings.json` → `scripts/hooks/lint-fix.py` |
| Pre-stop verification | `.claude/settings.json` → `scripts/hooks/stop.py` |
| Lint / test wrappers | `scripts/lint-all.py`, `scripts/run-tests.py` |
| Failure artifacts | `logs/lint-errors.log`, `logs/test-failures.log` (gitignored) |
| VS Code tasks | `.vscode/tasks.json` |

This is not decoration. A hook that only runs downstream is a hook nobody tests: devkit
shipped a `lint-fix.py` that formats on every edit and then needed a dedicated commit
(`4fbda17`) to clean up the format drift that had accumulated in the one repo where the
hook was not wired.

**When you change a hook script, you are changing the thing that is running you.** A
syntax error in `stop.py` breaks the current session's Stop; a bad `lint-fix.py` blocks
every subsequent edit. Both fail loudly and immediately, which is the point — but run
`python scripts/run-tests.py` and `python -m pytest scripts/hooks/tests/ -q` before
assuming a change is good.

## Scripts

All scripts under `scripts/` are Python, for cross-environment compatibility (local
Windows desktop and GitHub Actions).

- **Expose pure importable functions** guarded by `if __name__ == '__main__'` so pytest
  can test the logic without spawning a subprocess.
- Every new script ships with tests in the same change.
- **The hook scripts are stdlib only** — no third-party packages, ever. Hooks run
  *before* the virtualenv is active; a third-party import there breaks provisioning on
  exactly the sessions the harness exists to set up.

## The two test trees

They are deliberately separate, and the distinction is load-bearing.

- **`scripts/hooks/tests/`** — the vendored tier. It ships into every consuming project
  via `MANIFEST` and must stay **project-agnostic**: every value that varies per project
  comes from `hook.CFG` (read from that project's `.agent-harness.toml`), never from a
  literal. A hardcoded path once made 12 of these fail on every generated project's
  first CI run; `scripts/` being devkit's own `app_dir` broke another. Excluded from
  `pyproject.toml`'s `testpaths`, so it runs as its own step.
- **`tests/`** — devkit-only (generator, port registry, renderer, sweep). Never
  vendored, which is what lets the generator grow without forcing a `--pull` in every
  consumer. There is **no `conftest.py` here** on purpose — see `tests/support.py` for
  why a second one would collide with the vendored tree's.

## Vendoring rules

- `MANIFEST` in `scripts/sync-harness.py` is the shared set. Every entry ships with its
  test; keep both listed so a vendored copy is verifiable in isolation.
- **`.agent-harness.toml` is never vendored** — it is the per-project seam the shared
  code reads. Same for `.claude/settings.json`, `scripts/lint-all.py` and
  `scripts/run-tests.py`: each project's copy differs (lint scope, mypy scope, OTEL
  ports), so they live in `templates/`, not `MANIFEST`.
- **Never hard-code project specifics in a hook script.** A new behaviour gets a
  manifest field and a neutral default in `harness_config.py`, not an `if project ==`
  branch.
- Vendored files are compared **byte-for-byte**, so formatting counts. CI runs
  `ruff format --check .` because an unformatted MANIFEST file gets reformatted
  downstream on first edit, and the consumer's `sync-harness.py --check` then reports
  drift it did not cause.

## `templates/` is content, not source

- `.tmpl` files are not valid Python until rendered, and the plain `.py` files under
  `templates/` are linted by the `ruff.toml` that ships *alongside* them into each
  generated project — which carries `scripts/**` allowances devkit's own config does
  not apply at those paths.
- So `templates/` is excluded from ruff (`force-exclude = true`, so the exclusion holds
  for the explicitly-named paths that `lint-fix.py` and `lint-all.py --changed` pass),
  from mypy, and from `lint-all.py`'s `--changed` scope.
- `scripts/notify.py` and `scripts/notify-wrap.py` are **byte-identical copies** of the
  files under `templates/core/scripts/`, and a test enforces that. Fix either one and
  copy it across.

## Failure artifacts (fix from a file, not from the terminal)

Any task or script whose failures an agent is expected to act on must persist the
failure to a **parseable artifact file** under `logs/`. Never rely on streamed terminal
output — it scrolls away and buries the signal. Keep the terminal to a status line plus
the artifact path, put everything needed to diagnose in the file, write it on failure
*and* on success (an empty artifact on success, so a stale run cannot mislead the next
agent), and overwrite per run.

## VS Code tasks

- Use `"type": "process"` so VS Code monitors the process directly — that is what makes
  the spinner stop and the exit-code icon appear reliably.
- Set `"close": false` in `presentation` so the terminal stays open for review.
- **Wrap with `notify-wrap.py`** for the completion toast; never call `notify.py` from
  inside a script. Notifications are a task-layer concern only.
- Label convention: `"Domain: Title Case Action"`, and **every task carries a `detail`**
  — that is the second line in the quick-pick, and the only place a one-click action can
  state its cost or blast radius.
- A `${input:...}` picker must supply **one real token in every branch**. An empty
  string reaches argparse as a stray positional and is rejected, which is why
  `new-project.py` carries the redundant-looking `--dry-run` and `--remote` flags
  alongside their negations.

## Testing

Every code change includes tests in the same commit. Every testable unit of logic needs
coverage — gaps are not acceptable.

- New unit of logic: cover happy path, error cases, and edge cases.
- Bug fix: write the regression test first.
- A change to a hook script needs a test in the *vendored* tree, written against
  `hook.CFG` rather than devkit's literal values — it has to pass in every consumer too.

## Guardrails

### Instruction-file feedback loop

If an instruction in a skill, rule, or this file sent you into a dead end or a wasted
operation — or a mistake you made would have been prevented by one — flag it in your
report with the file, the line, and a proposed edit. **Never silently work around a bad
instruction.**

### One bad commit here reddens every consumer

Generated PR gates pin a devkit **tag**, never `@main`, for this reason. When a change
alters vendored behaviour, say so in the commit message: adopters find out by running
`sync-harness.py --pull`, and the message is the only changelog they get.
