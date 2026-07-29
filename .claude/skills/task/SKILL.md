---
name: task
disable-model-invocation: true
description: 'Start a new task on a fresh claude/<slug> branch cut from the latest origin default branch. Use at the start of a new piece of work, especially in a worktree where you are never on the default branch and always sit on a stale/merged branch.'
argument-hint: 'Short description of the task (used for the branch name)'
---

# Skill: Start a task on a fresh branch

Cuts a fresh `claude/<slug>-<mmdd>` branch from the latest `origin/<default>` so
each task is isolated and current.

**Usually you won't need this** — after you `/ship`, the branch-per-task hook
auto-starts the next task's branch on your next prompt (via the shipped marker
`/ship` drops). `/task` is the **manual override** for the cases the marker can't
cover: the first task of a fresh checkout, or resuming after you abandoned work
without shipping. It's explicit because the "I'm starting something new" boundary
can't be inferred safely — the local signals can't tell a freshly-cut empty
branch from a stale merged one.

Creating a *new* branch off `origin/<default>` is allowed in a worktree even while
the default branch is checked out in the primary tree.

## Step 1 — Start the branch

```bash
python scripts/start-task.py "<the task description>"
```

Pass the argument through as the description; it becomes the branch slug. The
script fetches `origin`, verifies `origin/<default>`, and checks out a fresh branch
based on it.

Non-zero exits: `4` the tree is dirty (finish the current task with `/ship` or
stash first — it will not strand or carry your changes), `7` `origin/<default>`
missing after fetch, `8` checkout failed. Report and stop on any of these.

## Step 2 — Confirm and proceed

On success it prints the new branch name. Confirm you're on it
(`git branch --show-current`) and start the work. When the task is done, run
`/ship` to lint, commit, push, and open the PR.
