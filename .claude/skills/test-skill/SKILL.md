---
name: test-skill
disable-model-invocation: true
description: 'Validates Claude-style skill behavior by exercising the workspace PreToolUse hook and checking a timestamped artifact. Use when checking Codex/Claude/Copilot hook compatibility end-to-end.'
argument-hint: '(no arguments)'
---

# Skill: Test Skill

This is a tiny compatibility smoke test for Claude-style skill features.

- The workspace `PreToolUse` hook should write `logs/agent/test-skill-hook.txt`
  with a UTC ISO timestamp.
- The suggested verification command below is optional and helps confirm workspace diff state.

## Provenance guardrail (critical)

- The coding agent must **not** run equivalent script lines manually in a terminal.
- The coding agent must **not** directly create, edit, or overwrite any of these files:
  - `logs/agent/test-skill-hook.txt`
- This file is a test artifact and must be produced only by the workspace hook.

## Compatibility note (important)

- Invoke this as an actual skill command (for example `/test-skill`).
- If this file is only pasted/attached as plain prompt text, skill instructions
  and hook activation may be treated as plain text and not executed automatically.

Suggested verification command (optional, run manually):
```sh
gh pr diff
```

## Step 1 — Activate the hook

Create `.claude/skills/test-skill/.active` with the exact text `active`.

Do not call `write-artifacts.py`. The marker is only a gate: the next tool call's
workspace `PreToolUse` hook must produce the artifact.

Then delete `.claude/skills/test-skill/.active` with a file-edit tool. The
`PreToolUse` hook runs before that deletion, sees the marker, and writes the
artifact. Confirm the marker is gone afterward so later tool calls do not rewrite
the artifact.

## Step 2 — Verify freshness and shape

Confirm the hook artifact file was updated for **this invocation**:

- `logs/agent/test-skill-hook.txt`

Requirements:

- The line must match its expected prefix + UTC ISO 8601 timestamp (`...Z`).
- Freshness gate: the extracted timestamp must be within the last **5 minutes**.

If any timestamp is stale or missing, treat the run as failed and reply with:

> FAIL: test-skill hook timestamp was not refreshed for this invocation; the workspace hook did not execute.

---

## Step 3 — Confirm

Reply with a single short message:

> Test file written with UTC timestamp: `logs/agent/test-skill-hook.txt`.
> Optionally run either of these commands to verify the change:
>
> ```sh
> gh copilot session status
> git diff --stat
> ```

Only use the success message above if Step 1 passed the freshness gate.
