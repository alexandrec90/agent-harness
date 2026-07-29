---
description: Conventions for authoring .claude rules and skills files
paths:
  - CLAUDE.md
  - "**/CLAUDE.md"
  - .claude/rules/**/*.md
  - .claude/skills/**/SKILL.md
---

# Rule: Rules & Skills Authoring

## Source of truth: `.claude/` over `.agents/`

`CLAUDE.md` and everything under `.claude/` are the **single source of truth**. The
`AGENTS.md` files and the `.agents/` tree are **generated mirrors** — a sync step copies
`CLAUDE.md` → `AGENTS.md` and `.claude/` → `.agents/` for harnesses that read those paths.

- **Only ever edit `.claude/` and `CLAUDE.md`.** Never hand-edit `.agents/**` or any
  `AGENTS.md` — those changes are overwritten on the next sync.
- **Treat the duplication as expected, not a defect.** Audits, reviews, and dedup passes
  should ignore `.agents/**` and `AGENTS.md` as mirror copies of `.claude/**` and `CLAUDE.md` —
  don't flag them as redundant or try to reconcile the two trees.

## Instruction files ship with a test, like code

An instruction file is not documentation — it is input that changes what the agent
does, so it carries the same obligation as the code in the same change. **Treat a new
or substantially changed instruction file the way you treat new code: it ships with a
test in the same change.** This is the instruction-file half of the coverage mandate
in `.claude/rules/engineering.md`; the two are one policy, not two.

The harness that measures it is **project-provided** — devkit does not vendor one, and
its shape varies (carameli runs a promptfoo suite under `evals/` with a
with-instructions vs leave-one-out-ablation comparison). Whatever the harness, the
same three properties decide whether a test is worth having:

- **It must discriminate.** The task has to measurably fail, or behave worse, when the
  file is ablated. A test that passes either way proves nothing and costs money on
  every run.
- **The baseline must be fair.** Compare against the skill's plain-English equivalent,
  not against an unresolved `/command` — otherwise you are measuring command
  resolution, not guidance. Weight correctness above efficiency.
- **Scope the run explicitly.** Eval runners typically fan a test across every
  configured model arm unless the test narrows them. Put read-only and single-edit
  tasks on the cheapest arm; reserve the capable arm for genuinely multi-step
  reasoning.

**Genuinely untestable headless?** Some skills cannot be evaluated — they read live
editor diagnostics, or they are aggregate dispatchers with no behavior of their own
(`/fix-all`). Document the exclusion and its reason alongside the harness rather than
shipping a flaky test.

## CLAUDE.md files

- **Only record non-obvious configuration** — things that can't be derived by reading
  source files (e.g. proxy routes, port mappings, env var semantics, architectural
  constraints). If Claude can find it in a config file in one read, leave it out.

## Rules (`.claude/rules/`)

Every rule file must include YAML frontmatter so Claude can scope when it applies:

```yaml
---
description: One-line summary of what the rule covers
paths:
  - <source-dir>/models/**/*.py
  - migrations/**/*.py
---
```

- `description` — brief, specific summary (used to decide relevance).
- `paths` — glob patterns for files the rule applies to. Omit only if the rule is
  truly global (rare).
- Keep rules focused on a single domain — don't mix unrelated conventions in one file.

### One rule file per variant

When a domain has interchangeable variants (themes, providers, adapters), give each
its own `.claude/rules/<domain>-<variant>.md` and **scope its paths to that variant's
directory only** — never to the domain's global tree, or every variant's rule loads on
every file in it.

Prefer **spec tables over code blocks** for values: a table of property/value/notes
survives a refactor that would leave a pasted code sample quietly wrong. Keep code
blocks for structure that only structure can convey (a hierarchy tree, a two-line
signature).

### Security / scoping rules

Cross-cutting security rules (e.g. multi-tenant auth) belong in a scoped rule file, not
in `CLAUDE.md`. Scope them tightly:

```yaml
paths:
  - <source-dir>/api/**/*.py
```

Then add a one-line pointer in `CLAUDE.md`'s guardrails cross-reference list, so the
constraint is discoverable from the root file without being restated there.

## Skills (`.claude/skills/`)

- Every skill frontmatter must include `disable-model-invocation: true`, **except
  skills that another skill invokes programmatically via the Skill tool** (orchestrated
  sub-skills). The flag blocks *all* Skill-tool invocation — including from a parent
  skill — so an orchestrated sub-skill must omit it or the parent's call fails with
  `cannot be used with Skill tool due to disable-model-invocation`. A dispatcher like
  `/fix-all` calling its `fix-*` children, or one fixer delegating to another when a
  restart breaks the stack, means every skill on the receiving end must omit the flag —
  with a comment in its frontmatter saying why, since the omission otherwise reads as an
  oversight. Skills that are only ever started by the user — or merely *suggested* in
  another skill's prose ("re-run `/fix-tests`") — keep the flag.
- If the skill generates scripts, those scripts follow the same conventions as
  hand-written ones — see `.claude/rules/engineering.md`, plus whatever tooling rule
  the project adds (notably `-T` on `docker compose exec`, without which the
  subprocess handle can outlive the command and hang the caller).


### Environment dependencies

A skill that depends on the local environment (Docker stack, running services, git
hooks, a browser runner) must say so in a one-line blockquote at the top of the
SKILL.md, e.g.:

```markdown
> Depends on the local Docker stack and its diagnostics being available.
```

Hooks are a Windows-local performance shortcut; they must never be the only path for
any step a skill needs to complete — skills use the Glob/Grep/Read/Write/Edit tools
directly and write state files (e.g. `state.json`) themselves rather than waiting on
a Stop hook.

### Hook output byte caps (token control)

When a hook or command placeholder emits command output that will be injected into
model context, cap output bytes by default to reduce token usage.

- Prefer a shared helper script in `scripts/hooks/` for capping and truncation markers
  instead of ad-hoc per-skill snippets.
- Do not keep only the first chunk when diagnostics matter. Prefer head+tail windows (or
  at minimum tail-on-error) so terminal errors near the end are preserved.
- Preserve exit-code semantics. Truncation wrappers must not mask command failures.
- Keep cap sizes explicit and small by default (for example, 4-8 KB), and raise only when
  diagnostics require a larger window.

### SKILL.md size limit

Keep `SKILL.md` under **500 lines**. If content exceeds this, apply progressive disclosure:

1. Extract reference material into a sibling file (e.g. `writing-conventions.md`)
2. Keep all references **one level deep** — `SKILL.md` → `reference.md` (never deeper)
3. Add a table of contents to any reference file longer than 100 lines
4. Use forward slashes in all file paths — never backslashes

### Fixer skill conventions (`fix-*`)

Skills that read a log artifact and fix the reported issues must follow these patterns
to prevent investigation spirals (where the model reads dozens of files without ever
making an edit):

#### 1. Known-fixes table (mandatory)

Every `fix-*` skill must have a sibling `known-fixes.md` file with this table format:

```markdown
| Error pattern (substring) | Root cause | Fix | Hits | Last used | Added |
```

- Patterns are plain substrings, not regex
- The skill updates **Hits** and **Last used** on every match
- Rows with **Hits = 0** older than 90 days from **Added** are pruned
- New rows are added only for patterns likely to recur

#### 2. Known-fix matching must be Step 1 — before any investigation

The skill must read the log artifact and `known-fixes.md` **in parallel** as its first
action. For every error that matches a known-fix pattern, the fix is applied immediately
as a one-shot — no additional file reads, no re-derivation. This is a **mandatory
short-circuit**, not a suggestion. Add it as a hard rule.

#### 3. Addressed marker

After applying fixes, append `--- ADDRESSED` to the log artifact. On the next
invocation, if the marker is present, tell the user to re-run the diagnostic task and
stop. The diagnostic task overwrites the file, naturally clearing the marker.

#### 4. Log-quality gate — both directions (mandatory)

A fixer must never fix *from* a bad artifact. When the log is unusable, the fix belongs in
the **producing script**, not the application code. Every `fix-*` SKILL.md must have a "Log
quality gate" section that blocks on **both** failure modes:

- **Missing detail** — a failure has no self-locating `file:line`, its traceback was stripped,
  or a summary names a failure with no matching block. Root cause is invisible; editing source
  would be a blind guess.
- **Drowning in noise** — the real failures are buried under content an agent can't act on:
  passing results, expected warnings, framework chatter (React `act(...)`, `PytestWarning`
  summaries), or a single non-source file flooding a section. The signal is unfindable.

In either case the skill must **not** touch application code. It edits the producing script
named on the artifact's `# source:` header (or the shared output filter it draws on)
to widen the capture or tighten the noise, updates that script's test in the **same** change
(every script under `scripts/` ships with its test), tells the user to
regenerate the artifact, and stops. "Don't waste time on suboptimal logs" is the whole point of
this gate — state it as a hard rule in the skill.
