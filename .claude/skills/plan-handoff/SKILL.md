---
name: plan-handoff
disable-model-invocation: true
description: 'Produces a detailed implementation plan suitable for handoff to a cheaper coding agent (GitHub Copilot, etc.) instead of executing the changes directly. Accepts either a skill name (`/plan-handoff fix-tests`) or a free-form task description (`/plan-handoff add a /health endpoint that returns DB status`). Use when the task is mechanical enough to delegate (boilerplate, multi-file rename, applying known-fix patterns, repetitive edits, scoped feature work) and you want to save Claude tokens. Skill mode runs the named skill''s diagnostic phase but stops before any edit; prompt mode researches the request directly.'
---

# Skill: Plan for Handoff

Produce a plan precise enough for a cheaper coding agent (Copilot) to execute
without judgment calls. Two input modes:

| Mode | Trigger | Example |
| --- | --- | --- |
| **Skill** | First arg token matches a directory under `.claude/skills/` | `/plan-handoff fix-tests` |
| **Prompt** | Anything else | `/plan-handoff add a /health endpoint returning DB status` |

If no argument is given, ask the user what to plan, then stop.

---

## Step 1 — Parse input and gather context

Strip a leading `/` from the first token if present. List the skill directories with the
Glob tool (`.claude/skills/*/SKILL.md` → take the directory names).

**Skill mode** (first token matches a skill dir): read these in a single
parallel call:

- `.claude/skills/<target>/SKILL.md`
- Any sibling files (`known-fixes.md`, etc.). Glob `.claude/skills/<target>/*.md`
  first if you don't already know which siblings exist.

**Prompt mode** (first token doesn't match): treat the entire argument as a task
description. Locate relevant code via `Grep`/`Glob` — read only what's needed to
write specific changes. Stop reading once you can fill in every field of the
plan template; resist the urge to over-explore.

---

## Step 2 — Execute diagnostics, NOT edits

**Skill mode:** walk the target skill's instructions step by step **up to but
not including** the point where it would call `Edit` or `Write` on a source
file. Use any log file the target reads (e.g. `logs/test-failures.log`).

**Prompt mode:** do whatever read-only investigation is needed to specify the
changes — read existing patterns, locate insertion points, identify dependencies.

In both modes, you **may** use:

- `Read`, `Glob`, `Grep`, `Bash` (read-only commands only)

You **may not** use:

- `Edit`, `Write`, `NotebookEdit` on any source path — the application package,
  `tests/`, a frontend tree, a migrations directory, or anything else the project
  builds from
- Any command that mutates state: `git commit`, `docker compose restart`,
  migration upgrades, package installs, etc.

The single allowed write target is the plan file in Step 4.

If diagnostics reveal that **no work is needed** (e.g. `logs/test-failures.log`
ends with `--- ADDRESSED`, the feature already exists, or the audit finds
nothing), report that and stop — do not produce a plan.

---

## Step 3 — Build the plan

For each change the target skill would have made, capture:

| Field | Content |
| --- | --- |
| **File** | Absolute-from-repo path with forward slashes |
| **Location** | Function/class name, or line range, or before/after anchor text |
| **Change** | Pseudo-diff (`- old` / `+ new`) or precise prose. No "consider", no "maybe" |
| **Why** | One line — the root cause or rule being applied |
| **Verify** | The exact command or test name that confirms the change worked |

Plus a top-level summary:

- One paragraph: what is being done and why
- Pattern reference: if the changes follow an existing pattern in the codebase,
  cite one concrete example with `path:line` so Copilot can mirror it
- Open questions: anything that needs a human answer **before** Copilot can
  proceed. If the list is non-empty, the plan is **blocked** — say so explicitly
  at the top.

### Quality bar

A plan is ready for handoff only if all of these are true:

1. Every file path is exact (no `~/foo` or relative-from-cwd ambiguity)
2. Every change has either a pseudo-diff or anchor text — no "update the function"
3. No instruction requires Copilot to read a file Claude hasn't already cited
4. Verification step is mechanical (a test name, a lint command) — not "looks right"
5. Open questions list is empty

If any of these fail, fix the plan before writing it. Do not hand off a plan
that requires Copilot to make architectural judgment calls — those are exactly
the cases where the round-trip cost eats the token savings.

---

## Step 4 — Write the plan

Write the plan to `logs/handoff-plan.md` (overwrite if it exists). Use this
structure:

```markdown
# Handoff Plan: <target skill> — <date>

## Summary
<one paragraph>

## Pattern reference
<path:line, or "none" if not applicable>

## Open questions
<bullet list, or "none">

## Changes

### 1. <file path>

**Location:** <function name / line range / anchor>

**Change:**
\`\`\`diff
- old line
+ new line
\`\`\`

**Why:** <one line>

**Verify:** <command or test name>

### 2. ...
```

---

## Step 5 — Report

Tell the user:

- Path to the plan file: `logs/handoff-plan.md`
- Number of changes in the plan
- Whether the plan is **ready** or **blocked** (open questions present)
- One-sentence handoff hint, e.g. *"Paste this file into Copilot Chat with the
  prompt: 'Apply the changes in this plan exactly as written.'"*

In skill mode, do not run the target skill's cleanup hooks — this run produced
a plan, not fixes, so its `Stop` hooks (e.g. session archiving in `fix-tests`)
should not fire.

---

## Hard Rules

1. **Never edit source files.** The only file you may write is `logs/handoff-plan.md`.
2. **Never run mutating commands** (git commit, docker restart, migrations, installs).
3. **Never invent file paths or line numbers.** If you didn't read it, don't cite it.
4. **Block rather than guess.** If the target skill's diagnostic reveals a decision
   that Claude would normally make mid-implementation, surface it as an open question
   and block the plan. Do not pre-decide on Copilot's behalf.
5. **Skill mode only — skip the target's hooks.** Do not fire `Stop`/`PreToolUse`
   hooks declared in the wrapped skill's frontmatter — those assume actual fixes
   happened.
6. **Stop early on no-op.** If diagnostics show nothing to do, report it and exit
   without writing an empty plan.
