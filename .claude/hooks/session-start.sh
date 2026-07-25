#!/bin/bash
# SessionStart hook — provisions the sandbox so `scripts/lint-all.py` and the
# pytest suites are runnable from turn one. Without this, a Claude Code on the
# web session boots without the pinned toolchain (no ruff/mypy/pytest, no
# frontend node_modules), so the full lint suite can only run in CI — surfacing
# lint drift one slow gate round at a time instead of in a single local pass.
#
# Synchronous + idempotent. Remote-only: local dev machines already have the
# venv + node_modules, and container state is cached after this completes, so
# re-runs are cheap (venv reused, pip/npm no-op when satisfied).
set -uo pipefail

# --- LOCAL sessions only: keep this branch current with origin/master ---------
# Parallel worktrees drift from master the longer their branches live; on a
# local session this rebases the checked-out branch onto origin/master so it
# begins current, with no manual command. scripts/hooks/session-sync.py refuses
# a dirty tree, disables autostash, and re-signs (--gpg-sign) so replayed commits
# stay Verified.
#
# This MUST NOT run in cloud/remote (Claude Code on the web) sessions: there the
# platform owns the branch lifecycle (branch, commit, signing, PR), and rebasing
# from inside rewrites commit SHAs, diverges the branch from the remote the
# mobile app tracks, and strips signatures. So it lives inside the local branch
# of the remote guard, and the cloud-provisioning steps below never reach it.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  (
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    branch="$(git branch --show-current 2>/dev/null)"
    # Resolve the repo's default branch (main/master/...) rather than assuming one:
    # origin/HEAD (set at clone), else probe origin/main then origin/master.
    default_branch="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"
    default_branch="${default_branch#origin/}"
    if [ -z "$default_branch" ]; then
      if git rev-parse --verify --quiet refs/remotes/origin/main >/dev/null 2>&1; then
        default_branch="main"
      else
        default_branch="master"
      fi
    fi
    # Nothing to do on the default branch itself, or with detached HEAD.
    if [ -n "$branch" ] && [ "$branch" != "$default_branch" ]; then
      echo "[session-start] Syncing '$branch' onto origin/$default_branch (no-op if tree is dirty)..."
      if ! python3 scripts/hooks/session-sync.py; then
        # On conflicts the rebase is left in progress. Auto-abort so the session
        # starts in a known-clean state; resolve conflicts manually instead.
        rebase_merge="$(git rev-parse --git-path rebase-merge 2>/dev/null)"
        rebase_apply="$(git rev-parse --git-path rebase-apply 2>/dev/null)"
        if [ -d "$rebase_merge" ] || [ -d "$rebase_apply" ]; then
          git rebase --abort 2>/dev/null
          echo "[session-start] origin/$default_branch had conflicting changes — auto-sync aborted, branch left untouched. Sync manually to resolve."
        else
          echo "[session-start] Branch sync skipped (dirty tree or offline) — sync manually when ready."
        fi
      fi
    fi
  ) || true
  # Local machines already have the venv + node_modules; nothing to provision.
  exit 0
fi

# --- REMOTE (Claude Code on the web) sandbox provisioning only below ----------
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# Install the Python toolchain into a project venv. A venv (rather than the
# system interpreter) avoids the Debian-managed-package conflict — pip refuses
# to upgrade distro-owned packages like PyYAML — and `scripts/lint-all.py`
# already prepends ./.venv/bin to PATH, so ruff/mypy/pytest resolve without any
# extra wiring. Match CI: runtime locks + dev linters together.
#
# uv (the same resolver that compiled the locks) does the heavy install — an
# order of magnitude faster than pip on a cold sandbox. One small pip install
# bootstraps uv at the version pinned in the dev lock, so the installer version
# is single-sourced from requirements-dev.txt rather than duplicated here.
echo "[session-start] Installing Python toolchain into .venv (runtime + dev linters)..."
[ -d .venv ] || python3 -m venv .venv
uv_version="$(sed -nE 's/^uv==([^ ;]+).*/\1/p' requirements-dev.txt | head -n 1)"
./.venv/bin/python -m pip install --quiet --disable-pip-version-check \
  "uv==${uv_version:?requirements-dev.txt has no uv pin}" \
  || echo "[session-start] WARN: uv bootstrap failed — dependency install may fail"
./.venv/bin/python -m uv pip install --quiet --python ./.venv/bin/python \
  -r requirements.txt -r requirements-dev.txt \
  || echo "[session-start] WARN: uv install failed — ruff/mypy/pytest may be unavailable"

# Persist the venv on PATH for every turn, so bare ruff/pytest/python resolve to
# it (not only under lint-all.py's internal PATH shim).
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"${CLAUDE_PROJECT_DIR:-.}/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

echo "[session-start] Installing frontend toolchain (eslint/stylelint/tsc)..."
# npm install (not ci) so a warm cached container reuses node_modules. The
# container's npm may differ from the lockfile author's and rewrite lockfile
# metadata on install; that churn trips the stop hook's dirty-tree check on
# otherwise read-only sessions, so restore the lockfile if it was clean before.
LOCKFILE=frontend/package-lock.json
lockfile_was_clean=false
git diff --quiet -- "$LOCKFILE" 2>/dev/null && lockfile_was_clean=true
npm install --prefix frontend --no-audit --no-fund \
  || echo "[session-start] WARN: npm install failed — frontend linters may be unavailable"
if $lockfile_was_clean && ! git diff --quiet -- "$LOCKFILE" 2>/dev/null; then
  git checkout -- "$LOCKFILE" \
    && echo "[session-start] Restored $LOCKFILE (npm install metadata churn)"
fi

# External lint binaries lint-all.py shells out to, installed to a PATH dir so
# `shutil.which(...)` finds them. Best-effort: the runner skips a missing tool
# cleanly and CI installs them regardless, but having them here keeps a local
# `lint-all.py` run faithful to the gate. NB positional args:
# download-actionlint.bash takes [[VERSION] DIR], NOT a -b flag.
BIN_DIR=/usr/local/bin
[ -w "$BIN_DIR" ] || BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
if ! command -v actionlint >/dev/null 2>&1; then
  echo "[session-start] Installing actionlint -> $BIN_DIR..."
  curl -sSfL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash \
    | bash -s -- latest "$BIN_DIR" \
    || echo "[session-start] WARN: actionlint install skipped"
fi
if ! command -v dotenv-linter >/dev/null 2>&1; then
  echo "[session-start] Installing dotenv-linter -> $BIN_DIR..."
  curl -sSfL https://raw.githubusercontent.com/dotenv-linter/dotenv-linter/master/install.sh \
    | sh -s -- -b "$BIN_DIR" \
    || echo "[session-start] WARN: dotenv-linter install skipped"
fi

echo "[session-start] Done. Run 'python scripts/lint-all.py' before pushing a gated branch."
