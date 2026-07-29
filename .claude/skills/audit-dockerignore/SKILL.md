---
name: audit-dockerignore
disable-model-invocation: true
description: 'Audits .dockerignore for missing patterns that bloat the Docker build context or leak secrets into images. Use after adding new directories, config files, or secret files to the project.'
argument-hint: 'No arguments needed — always scans and applies any required updates.'
---

# Skill: Audit .dockerignore

Scan the repo for files that should not enter the Docker build context, update
`.dockerignore` accordingly, and report changes.

---

## Step 1 — Read Current State

Read all of the following **in parallel**:

| File | What to extract |
| --- | --- |
| `.dockerignore` | Existing patterns and comments |
| `.gitignore` | Patterns that may also apply to Docker context |
| `Dockerfile` | `COPY` directives — what actually gets pulled in |
| `docker-compose.yml` | Volume mounts (files that bypass the build context) |

---

## Step 2 — Inventory Candidates

Check for files/directories that exist in the repo but are not in `.dockerignore`:

### Must-ignore (secrets / security)

- `.env`, `.env.*` (except `!.env.example`)
- `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.crt`, `*.cer`
- `.npmrc`, `.pypirc`

### Should-ignore (bloat / dev-only)

- **Build artifacts**: `frontend/dist/`, `frontend/node_modules/`, `node_modules/`,
  `__pycache__/`, `*.pyc`, `.venv/`
- **Test/coverage**: `tests/e2e/`, `tests/load/`, `.pytest_cache/`, `.mypy_cache/`,
  `.ruff_cache/`, `.hypothesis/`, `coverage/`, `htmlcov/`
- **AI/editor**: `.claude/`, `.claudeignore`, `.vscode/`, `.idea/`, `*.code-workspace`
- **CI/CD**: `.github/`
- **Host-only scripts**: `scripts/`, `playwright.config.ts`, `.pre-commit-config.yaml`
- **Docs/notes**: `*.md`, `charts/`, `docs/`, `todo.md`
- **Compose-mounted configs**: `prometheus.yml`, `docker-compose.yml`, `nginx.conf`
- **Root Node tooling**: root `package.json`, `package-lock.json`, `node_modules/`

### Context check

For each candidate, verify it is **not** referenced in a `COPY` directive in the
`Dockerfile`. If it is, skip it and note the conflict.

---

## Step 3 — Diff and Report

Compare candidates against existing `.dockerignore` patterns.

If nothing is missing, print:

```text
.dockerignore is up to date — no changes needed.
```

and stop.

Otherwise, print:

```text
## .dockerignore Audit

| Pattern | Reason | Status |
|---|---|---|
| tests/load/ | Dev-only load tests, not needed in image | Missing — will add |
| .github/ | CI config, not needed in image | Missing — will add |
| ... | ... | Already covered |
```

---

## Step 4 — Apply Updates

- Add missing patterns grouped by category, following the existing comment style.
- Preserve existing negations and exceptions (e.g., `!.env.example`).
- Do not remove existing patterns unless they match nothing in the repo.

---

## Step 5 — Verify

Run a quick check to estimate build context size impact:

```bash
# Show what Docker would send (approximate)
git ls-files | wc -l
```

Print a summary of patterns added and any follow-up notes.
