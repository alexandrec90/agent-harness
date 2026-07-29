"""devkit must actually run the utilities it ships.

A harness that is only ever wired downstream is a harness nobody tests. Every check
here exists because the absence of the thing it asserts was, at some point, real:
devkit shipped hooks with no `.claude/settings.json` to fire them, a manifest describing
a different project, and a Stop hook invoking a lint runner this repo did not have.

These are contract tests, not style preferences — each one fails loudly if devkit drifts
back into shipping a utility it does not itself use.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib

from support import REPO_ROOT, TEMPLATES, harness_config, load_script

SETTINGS = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS = TEMPLATES / "core" / "dot-claude" / "settings.json.tmpl"

# Hook commands are written as `${CLAUDE_PROJECT_DIR:-.}/<path>`; this pulls the paths.
HOOK_PATH_RE = re.compile(r"\$\{CLAUDE_PROJECT_DIR:-\.\}/([^\"]+?\.(?:py|sh))")


def _settings() -> dict:
    """devkit's own settings, parsed strictly.

    Strictly on purpose: Claude Code does not accept comments in `settings.json`, and an
    unparseable file does not warn — it silently disables every hook in it, which is the
    exact failure this whole module exists to prevent.
    """
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def test_devkit_has_settings_wiring_its_own_hooks():
    assert SETTINGS.exists(), "devkit ships hook scripts but has nothing to fire them"
    hooks = _settings()["hooks"]
    # The events that carry devkit's own utilities. SessionStart provisions the venv,
    # PostToolUse auto-formats on edit, Stop runs pre-stop verification; without these
    # three the harness is inert no matter what else the file says.
    for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
        assert event in hooks, f"{event} is not wired in devkit's own settings"


def test_every_hook_devkit_wires_actually_exists():
    """A hook command pointing at a missing script fires every turn and fails silently."""
    referenced = HOOK_PATH_RE.findall(SETTINGS.read_text(encoding="utf-8"))
    assert referenced, "no hook scripts referenced — the regex or the file shape changed"
    for rel in referenced:
        assert (REPO_ROOT / rel).exists(), f"{rel} is wired as a hook but does not exist"


def test_devkit_wires_every_hook_event_the_template_does():
    """devkit must not fall behind the settings it generates for other projects.

    The template is the specification of "what a project gets". If it grows a hook event,
    devkit should have it too — otherwise the repo authoring the harness is the one repo
    running an older version of it.
    """
    template_hooks = set(re.findall(r'"(\w+)": \[', TEMPLATE_SETTINGS.read_text(encoding="utf-8")))
    # `hooks` itself is not an event, and neither is the permissions `allow` list.
    template_events = {name for name in template_hooks if name not in {"hooks", "allow"}}
    missing = template_events - set(_settings()["hooks"])
    assert not missing, f"the template wires {sorted(missing)} but devkit does not"


# --- the lint runner / Stop hook contract -------------------------------------


def _lint_runner_help(cwd, script="scripts/lint-all.py") -> str:
    result = subprocess.run(
        [sys.executable, script, "--help"], cwd=cwd, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{script} --help failed:\n{result.stderr}"
    return result.stdout


def test_lint_runner_accepts_every_flag_the_stop_hook_passes():
    """Tier 1 passes fixed flags; argparse rejecting one is a permanent lint failure.

    `stop.py` is vendored byte-identical and cannot introspect the lint runner, so the
    flags it sends are a contract. When they disagree, argparse exits 2 and the Stop hook
    reports a lint failure whose body is a usage message — on every single stop, with
    nothing in the source tree that can fix it. That is exactly what `--no-secrets` did.
    """
    stop = load_script("scripts/hooks/stop.py")
    spec = stop._command_for(stop.CHECK_LINT)
    assert spec is not None, "devkit has no lint runner for its own Stop hook to invoke"
    argv, _cwd, _artifact = spec
    help_text = _lint_runner_help(REPO_ROOT)
    for flag in [a for a in argv if a.startswith("--")]:
        assert flag in help_text, f"stop.py passes {flag} but scripts/lint-all.py rejects it"


def test_stop_hook_finds_devkits_own_lint_runner():
    stop = load_script("scripts/hooks/stop.py")
    assert stop.LINT_ALL.exists(), "the Stop hook's Tier 1 script is missing from devkit"


# --- the manifest describes devkit, not some other project --------------------


def test_manifest_paths_exist_in_this_repo():
    """`.agent-harness.toml` used to be a copy of carameli's, naming `app/` and a DB.

    Harmless as documentation, wrong as configuration: the hooks read it to decide which
    directories to lint and test, and devkit now runs those hooks on itself.
    """
    cfg = harness_config.load(REPO_ROOT)
    for label, path in (("app", cfg.app_dir), ("tests", cfg.tests_dir), ("unit", cfg.unit_tests)):
        assert (REPO_ROOT / path).is_dir(), f"[paths] {label} = {path!r} is not a directory here"


def test_manifest_declares_no_infra_devkit_does_not_have():
    cfg = harness_config.load(REPO_ROOT)
    assert not cfg.db.enabled, "devkit has no database or compose stack"
    assert not cfg.frontend.enabled, "devkit has no frontend"
    assert not (REPO_ROOT / "docker-compose.yml").exists()


def test_manifest_env_prefix_is_devkits_own():
    """A borrowed prefix silently shares control vars with another project's shell."""
    cfg = harness_config.load(REPO_ROOT)
    assert cfg.env_prefix == "DEVKIT", f"env_prefix is {cfg.env_prefix!r}"


# --- toolchain provisioning ----------------------------------------------------


def test_pyproject_lets_session_start_provision_devkit():
    """`session-start.sh` detects the dependency model from files on disk.

    Its chain is `uv.lock` -> `requirements-dev.txt` -> `pyproject.toml` -> warn and
    skip. devkit had none of the three, so every remote session hit the final branch and
    left an empty `.venv` with no ruff, mypy or pytest — while the script it ends by
    recommending, `scripts/lint-all.py`, needs all three.
    """
    assert (REPO_ROOT / "uv.lock").exists(), "no lock: provisioning is not reproducible"
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    # A virtual project: devkit is scripts and templates, not an installable
    # distribution, and `uv sync` fails outright trying to build a wheel without this.
    assert data["tool"]["uv"]["package"] is False
    dev = data["dependency-groups"]["dev"]
    for tool in ("ruff", "mypy", "pytest"):
        assert any(tool in spec for spec in dev), f"{tool} missing from the dev group"


def test_pytest_scope_keeps_the_vendored_tier_separate():
    """`scripts/hooks/tests/` ships into every project and runs as its own step."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = data["tool"]["pytest"]["ini_options"]["testpaths"]
    assert testpaths == ["tests"], f"testpaths is {testpaths!r}"


# --- copies that must not drift -----------------------------------------------


def test_notify_scripts_are_byte_identical_to_the_template_copies():
    """devkit's task wrappers are copies, not forks — nothing renders them.

    They carry no placeholders, so there is no reason for the two to differ, and a silent
    divergence means the fix you made locally never reaches a generated project.
    """
    for name in ("notify.py", "notify-wrap.py"):
        ours = (REPO_ROOT / "scripts" / name).read_bytes()
        theirs = (TEMPLATES / "core" / "scripts" / name).read_bytes()
        assert ours == theirs, f"scripts/{name} has drifted from templates/core/scripts/{name}"


def test_vscode_tasks_are_valid_and_labelled():
    """Every task carries a `detail` — it is the only place a one-click action can state
    its blast radius, which is CLAUDE.md's rule for this file."""
    raw = (REPO_ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    # tasks.json is JSONC; VS Code allows the comments, `json` does not.
    stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
    tasks = json.loads(stripped)["tasks"]
    assert tasks, "no tasks defined"
    for task in tasks:
        assert task.get("detail"), f"task {task['label']!r} has no detail"
        assert task.get("type") == "process", f"task {task['label']!r} is not type=process"
