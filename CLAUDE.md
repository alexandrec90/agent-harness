# devkit

The source of truth for the vendored agent harness: hook scripts, the shared
instruction tier (`.claude/rules/`, `.claude/skills/`), the project generator, and the
port registry. Consuming projects commit a copy of everything in `sync-harness.py`'s
`MANIFEST` and drift-check it in CI.

## Baseline policy

`.claude/rules/engineering.md` (testing, scripts, the harness seam, the
instruction-feedback loop) and `.claude/rules/authoring.md` (writing rules and skills)
apply here too — devkit vendors them *out*, so it is also the first place they have to
hold. Everything below is what is true about devkit specifically.

## The thing that is easy to get wrong

**Two test trees, and they are not interchangeable.**

| Tree | Vendored? | May assert devkit's own shape? |
| --- | --- | --- |
| `scripts/hooks/tests/` | Yes — in the `MANIFEST` | **No.** It runs inside every consuming repo, against that repo's `.agent-harness.toml`. Derive every varying value from `CFG`, and skip tiers a project does not have. |
| `tests/` | No | Yes. Generator, port registry, renderer — devkit-only. |

This has been violated once and it was expensive: the vendored tests pinned carameli's
literal credentials, paths, env prefix, and skill list, so **every generated project
failed 12 of them on its first CI run** and no other repo could adopt the harness. The
trap is that devkit's own suite passes precisely *because* devkit's manifest is the one
being hard-coded against. CI's `generated-project` job exists to catch that class: it
renders a project of each preset and runs its suites.

**`.agent-harness.toml` in this repo is a test fixture, not a description of devkit.**
It enables the DB and frontend tiers so the vendored suite exercises them here, and it
describes a project shaped nothing like devkit (no `app/`, no compose stack). Do not
"fix" it to match devkit — that would silently drop `run_db_tests` coverage from the
source repo. Anything that holds a repo to its manifest must gate on the repo actually
wiring `stop.py` as a Stop hook; devkit does not.

## Layout

| Path | What it is |
| --- | --- |
| `scripts/hooks/` | The vendored hook scripts. **stdlib only** — they run before any venv. |
| `scripts/hooks/tests/` | The vendored test tier. Project-agnostic, see above. |
| `scripts/new-project.py` | The generator. Dry run is the **default**; `--yes` applies. |
| `scripts/devkit_ports.py` | Host-port slot registry (`ports.toml`). Prints lines to add — never edits the registry itself. |
| `scripts/sweep.py` | Cross-checkout ship sweep. Never mutates a repository. |
| `templates/` | Rendered once per project, at creation. Excluded from devkit's ruff config — it is content, not source. |
| `.claude/rules/`, `.claude/skills/` | The shared instruction tier, vendored via `MANIFEST`. |

## Working here

- **Edit here, PR, merge, then projects `--pull`.** Only `--push` from the one project
  actively authoring a change.
- **A `MANIFEST` addition ships to every project on their next pull.** Both directions
  cost something: a file that should be identical everywhere belongs in the `MANIFEST`
  (the drift check is free and already wired), and a file that must differ per project
  does not — it needs a manifest field or a contract test instead.
- **`ruff format` is gated, not just `ruff check`.** `scripts/**` ignores E501 by
  design, so an over-long line passes lint and still fails the format job. Vendored
  files are byte-identical downstream and every adopter formats on edit, so an
  unformatted file here is rewritten downstream on first touch and reported as drift
  the adopter did not cause.
- **The internal names still say `agent-harness`** — `.agent-harness.toml`,
  `$AGENT_HARNESS_DIR`, `HARNESS_VERSION`, `sync-harness.py`. Renaming them moves
  `MANIFEST` paths in lockstep across every consuming repo, so it is a deliberate
  separate migration. Use the old names; they are what the code reads.
