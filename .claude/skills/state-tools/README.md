# state-tools

`state-engine.py` is the only thing that writes a skill's `state.json`. Skills that
accumulate knowledge across sessions — which modules have tests, which files were
refactored, which files a design audit has cleared — keep it there, and never edit it
by hand.

## The flow

1. The skill does its work and writes a small **interim artifact** beside itself.
2. On Stop, `scripts/hooks/finalize-state.py` notices the artifact and calls this
   engine, which merges it into `state.json` and deletes the artifact on success.
3. A failed merge leaves the artifact in place, so the next Stop retries.

Wiring is `[stop] finalize_targets` in `.devkit.toml` — `[skill, schema]`
pairs. An entry naming a skill that does not exist makes the Stop hook do useless
work every turn, so the list starts empty.

## The three schemas

| Schema | Interim artifact | State shape |
| --- | --- | --- |
| `modules` | `state-updates.json` | `{modules: [{module, test_file, git_hash, gaps_found, last_reviewed}]}` |
| `files` | `state-updates.json` | `{files: {path: {git_hash, refactored_at}}}` |
| `audit` | `scan-results.json` + `scan-plan.json` | `{checks: {ID: {checkSignature, files: {path: {contentHash, status}}}}}` |

`modules` and `files` are pure merges over data the skill supplies. They are portable
with nothing to configure.

## `audit` needs project data

Planning an audit scan means knowing **which files each check applies to**, and that
is one project's source layout. So the check definitions are not in the engine — they
are read from `.claude/skills/<skill>/check-specs.json`, which the consuming project
owns and which is deliberately **not** vendored:

```json
{
  "exclude_prefixes": ["alembic/versions/"],
  "checks": [
    { "id": "A", "kind": "local", "include": ["app/**/*.py", "frontend/src/**/*.ts"] },
    { "id": "B", "kind": "cross", "include": ["app/**/*.py"] }
  ]
}
```

- `kind` is `local` (each file judged alone) or `cross` (the check needs a
  project-wide fact table, so the plan sets `rebuildFacts`).
- Without this file the `audit` schema is unavailable and `plan` **exits 1 with a
  message**. It does not write an empty plan: "no checks defined" and "nothing to
  scan" are indistinguishable downstream, and the quiet version reads as *all clear*.
- `.claude/` is always excluded and a project cannot opt back in — an audit that
  reads its own state files has a result that depends on its own output.

## Incremental scanning

A file is re-scanned when its content hash changed, when it has never been seen, or
when its last status was `violation`/`stale`. A `pass`/`fixed` file with an unchanged
hash is skipped. Changing a check's definition changes its `checkSignature`, which
forces a full rescan of that check.

> The signature payload is frozen. It is hashed into every consumer's `state.json`,
> so adding a field to `check_signature()` silently invalidates every project's
> accumulated audit state.

## Testing

`scripts/hooks/tests/test_state_engine.py`, vendored alongside the engine. Everything
is synthesised under `tmp_path` — it must pass in a repo of any shape.
