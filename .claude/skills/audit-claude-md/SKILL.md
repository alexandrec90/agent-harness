---
name: audit-claude-md
disable-model-invocation: true
description: 'Audits CLAUDE.md files against Anthropic best practices for conciseness, structure, and content quality.'
argument-hint: '(no arguments)'
---

# Claude.md Auditor Rules

Audit **every** `CLAUDE.md` in the project — root and subdirectories — against the
standards below (derived from Anthropic's official best practices).

## 0. Discovery

Before auditing, find all targets:

```text
Glob pattern: **/CLAUDE.md
```

Audit each file individually. Subdirectory `CLAUDE.md` files (e.g., `frontend/CLAUDE.md`)
should contain only information specific to that subtree — flag anything that duplicates or
contradicts the root `CLAUDE.md`.

## 1. The "Golden Rule" (Conciseness)

- **Line Count**: Is the file under 200 lines? (Absolute maximum: 300).
- **Signal-to-Noise**: Does every line earn its place? If Claude could figure it out by reading the code, it should be removed.
- **Tone**: Is the advice direct and grounded? Remove "I can" or "Please" — use imperatives.

## 2. Essential Content Audit

- [ ] **Project Context**: Does it have a 1-2 line "WHY" and "WHAT" for the project?
- [ ] **Commands**: Are there explicit strings for build, test, lint, and deploy commands that aren't easily guessed?
- [ ] **Directory Map**: Is there a high-level overview of where the most important code lives?
- [ ] **Architecture**: Are there specific architectural decisions mentioned that Claude couldn't infer?
- [ ] **Non-Default Style**: Are rules limited to things linters *don't* catch? (e.g., "Always use functional components over classes").

## 3. Exclusion Audit (The "Red Flag" List)

Flag any of the following for removal:

- **Fluff**: "Write clean code," "Be helpful," or "Follow best practices."
- **Code Bloat**: Long snippets of code or full API documentation.
- **Redundancy**: Style rules already enforced by the project's `.eslintrc`, `prettierrc`, or language defaults.
- **Volatility**: Information that changes frequently (e.g., current version numbers, specific bug lists).
- **File-by-File Details**: Descriptions of what every single file does.

## 4. Organization & Disclosure

- **Structure**: Uses clear H2/H3 headers and bullet points for scannability.
- **Progressive Disclosure**: Are deep, task-specific instructions (like an API design guide) moved to `.claude/rules/` or referenced via `@` imports?
- **Emphasis**: Uses `IMPORTANT` or `YOU MUST` for high-leverage rules that Claude has struggled with in the past.

## 5. Auditor Workflow

When running `/audit-claude-md`:

1. **Count Lines**: Check file length.
2. **Scan Commands**: Verify build/test/lint commands are present and work.
3. **Identify Waste**: Highlight at least 3 lines that can be pruned or moved to `.claude/rules/`.
4. **Propose Refactor**: Provide a simplified, "ideal" version of the file.

---

### Standard Commands for this Audit

- **Find all targets**: `Glob **/CLAUDE.md`
- **Check length**: `wc -l <path>/CLAUDE.md` (for each target)
- **Verify structure**: `grep "^#" <path>/CLAUDE.md`
- **Check rules**: `ls .claude/rules/`
