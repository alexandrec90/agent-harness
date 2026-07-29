---
name: retro
disable-model-invocation: true
description: 'On-demand session retro: digests a session transcript for failures, repeated and vacuous operations, attributes pattern waste to instruction files (skills/rules/CLAUDE.md), and proposes edits. Zero ambient cost — run only when a session felt wasteful.'
---

# Skill: Session Retro

> Depends on local session transcripts under `~/.claude/projects/<sanitized-cwd>/` (JSONL).
> No Docker or network needed.

Post-mortem for a session that felt wasteful. The complement to the always-on nudges (the
Skill/failure hooks and the CLAUDE.md "Instruction-file feedback loop" guardrail): those
catch waste in the moment; this skill finds the pattern after the fact and traces it to the
markdown that caused it. Its output is a **report with proposed instruction-file edits**,
not code changes.

## Argument

- `/retro` — digest the newest transcript (usually the current session; its tail will
  include this retro run itself — ignore that tail).
- `/retro list` — run the digest script with `--list`, show the user the 20 newest
  transcripts (mtime, size, name), and stop so they can pick one.
- `/retro <path-or-filename-prefix>` — digest that transcript.

## Step 1 — Digest, never raw-read

Run:

```
python .claude/skills/retro/extract.py [<transcript>] [--head 50 --tail 50]
```

It prints one line per tool call (`index tool ok/ERR size input-key`) plus three signal
sections: **FAILED**, **REPEATED** (same tool+input >= 2), **VACUOUS** (check-style call
that returned ~nothing — the classic silent waste, e.g. linting an empty changed-set).

## Step 2 — Identify waste candidates

From the digest only, mark:

- **Failures** — FAILED rows, especially the same error more than once.
- **Repeats** — REPEATED rows where re-running couldn't yield new information (re-reading
  the same file is often fine; re-running the same failing command is not).
- **Vacuous checks** — VACUOUS rows where a verification step inspected nothing and
  "passed".
- **Spirals** — long uninterrupted runs of reads/searches with no edit between them in the
  CALLS list.

Nothing marked → report "clean session" with the digest header line and **stop**.

## Step 3 — Attribute each candidate

Decide which of three buckets each candidate falls in. Only the first is actionable here:

1. **Instruction-caused** — a skill, rule, or CLAUDE.md line *prescribes* the wasteful
   action, or omits a guard that would have prevented it (the `/fix-prs` `--changed`
   incident: the skill's own table named a command that always lints 0 files). To confirm,
   read **only the implicated section** of the implicated file — do not re-read whole
   instruction trees.
2. **One-off model error** — a mistake no doc line would plausibly have prevented. Not an
   instruction finding; mention it only if it recurred within the session.
3. **Environment flake** — network, Docker down, transient tool error. Not actionable here.

## Step 4 — Report

One table: `evidence (digest line) | verdict | file:line | proposed edit`. For
instruction-caused findings the proposed edit must be concrete (the replacement wording,
not "clarify this"). Then stop — **apply edits only if the user agrees**. When applying:

- Edit `.claude/` / `CLAUDE.md` only — never the generated `.agents/` / `AGENTS.md` mirrors.
- A substantial rewrite of an instruction file carries the eval-coverage mandate
  (`.claude/rules/authoring.md`).

## Hard Rules

1. **Never load a raw `.jsonl` transcript into context** — digest only. If `extract.py`
   fails, fix `extract.py` (its test is `scripts/hooks/tests/test_retro_extract.py`);
   don't fall back to reading the transcript.
2. **Don't blame docs for one-off model errors.** An instruction finding needs a specific
   line that prescribed the waste or a specific missing guard that would have prevented it.
3. **Report first, edit on approval.** This skill never edits instruction files, code, or
   tests before the user says yes.
4. **Stay cheap.** No re-running producers, tests, or the wasteful commands to "confirm"
   digest findings; the digest plus one targeted read per finding is the whole budget.
