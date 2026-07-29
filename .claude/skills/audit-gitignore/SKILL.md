---
name: audit-gitignore
disable-model-invocation: true
description: 'Reviews and updates .gitignore for sensitive or irrelevant files and removes newly ignored files from the repo. Use when adding new tools, build outputs, or secret files to the project.'
argument-hint: 'Optional: scope (quick|deep), target paths, or ignore categories.'
---

# Skill: Audit .gitignore for Sensitive or Irrelevant Files

Use this skill to scan a repo for files that should be ignored, update `.gitignore` accordingly, and remove newly ignored tracked files from the repo.

## Step 1 — Set scope and constraints

- Ask for scan depth (quick vs deep) and any paths to exclude.
- Confirm which file types must remain tracked (e.g., `.env.example`, sample certs, or tooling configs).
- Prefer conservative ignores when uncertain.

## Step 2 — Review existing `.gitignore`

- Read `.gitignore` and note existing patterns and exceptions.
- Keep current organization and comments; extend categories only when necessary.

## Step 3 — Inventory potential ignore candidates

Check for files that are irrelevant, generated, or sensitive:

- Secrets and credentials: `.env`, `.env.*` (except `!.env.example`), `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.crt`, `*.cer`, `.npmrc`
- Build outputs: `dist/`, `build/`, `.next/`, `out/`, `*.tsbuildinfo`
- Dependencies and caches: `node_modules/`, `.venv/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `.cache/`, `.parcel-cache/`, `.turbo/`, `.eslintcache`, `.stylelintcache`
- Coverage and logs: `coverage/`, `.coverage*`, `*.lcov`, `*.log`, `logs/`
- OS/IDE artifacts: `.DS_Store`, `Thumbs.db`, `.idea/`, `.vscode/` (only if not intentionally tracked)

Use `git status` and repo inspection to determine which patterns are relevant to this codebase.

## Step 4 — Propose `.gitignore` updates

- Add minimal, specific patterns grouped by category.
- Preserve existing negations and exceptions.
- Avoid ignoring source or configuration that should be tracked.

## Step 5 — Remove newly ignored tracked files from the repo

- For each new ignore pattern, find tracked files that match.
- Remove from the index without deleting local files: `git rm --cached <path>`.
- If a file is generated or unsafe to keep, delete it after untracking.

## Step 6 — Verify and summarize

- Re-check status to ensure ignored files are no longer tracked.
- Summarize patterns added, files untracked, and any follow-up actions (e.g., secret rotation).

## Completion checklist

- `.gitignore` updated with minimal, modern patterns relevant to this repo.
- No sensitive or irrelevant files remain tracked remotely.
- Any newly ignored tracked files were removed from the index.
