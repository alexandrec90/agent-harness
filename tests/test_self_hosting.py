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

import pytest
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
    """`.devkit.toml` used to be a copy of carameli's, naming `app/` and a DB.

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


WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TEMPLATE_WORKFLOWS = TEMPLATES / "core" / "dot-github" / "workflows"
# `uses: owner/repo@ref`, with an optional leading `- `. Local composite actions
# (`./.github/actions/...`) carry no ref and are deliberately not matched.
USES_RE = re.compile(r"uses:\s+([\w.-]+/[\w.-]+)@(\S+)")


def _yaml():
    # PyYAML arrives with pre-commit in the dev group rather than on its own, so a
    # partially-provisioned checkout can be without it. Skipping beats a spurious red.
    return pytest.importorskip("yaml")


def test_devkit_runs_the_dependency_updates_it_ships():
    """The generator hands every project a Dependabot config; devkit must carry one too.

    Same reason as every other check in this file: a config that is only ever rendered
    downstream is a config nobody has run. devkit's ecosystems are the subset it has —
    no npm, no Dockerfile — but `uv` and the actions its own workflows pin must be there
    or its toolchain and CI pins go stale exactly as silently as the templates' would.
    """
    yaml = _yaml()
    config = REPO_ROOT / ".github" / "dependabot.yml"
    assert config.exists(), "devkit ships dependency updates but does not run them"
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed["version"] == 2
    ecosystems = {u["package-ecosystem"] for u in parsed["updates"]}
    assert ecosystems == {"uv", "github-actions"}, (
        f"devkit declares {sorted(ecosystems)}; it has a uv lock and workflows, and "
        f"nothing else Dependabot can read (no package.json, no Dockerfile)"
    )


def test_devkit_automerge_waits_on_devkits_own_ci_workflow():
    """`workflow_run` matches a workflow by title. A rename makes the merge job inert.

    Nothing goes red when it does: an unmatched `workflow_run` produces no run at all,
    so every Dependabot PR just sits open with an `automerge` label nobody acts on.
    """
    yaml = _yaml()
    automerge = WORKFLOWS / "dependabot-automerge.yml"
    assert automerge.exists(), "devkit has Dependabot but nothing to merge its PRs"
    parsed = yaml.safe_load(automerge.read_text(encoding="utf-8"))
    # PyYAML is YAML 1.1, where an unquoted `on` key parses as the boolean True.
    triggers = parsed.get("on", parsed.get(True))
    watched = triggers["workflow_run"]["workflows"]
    titles = {
        yaml.safe_load(p.read_text(encoding="utf-8"))["name"]
        for p in WORKFLOWS.glob("*.yml")
        if p != automerge
    }
    for name in watched:
        assert name in titles, (
            f"dependabot-automerge.yml waits on a workflow named {name!r}, "
            f"but devkit's workflows are {sorted(titles)}"
        )


def test_workflow_action_pins_match_the_templates():
    """Dependabot bumps devkit's workflows; it cannot see the ones under `templates/`.

    The github-actions ecosystem only scans `.github/workflows/`, so a bump lands here
    and the rendered PR gate keeps pinning the old major indefinitely — invisible,
    because every generated project's gate goes on passing. This is the reminder to
    carry the bump across; it is not a rule that the two must always agree, so if a
    template genuinely needs a different pin, record the reason and adjust this test.
    """
    ours: dict[str, set[str]] = {}
    for path in WORKFLOWS.glob("*.yml"):
        for action, ref in USES_RE.findall(path.read_text(encoding="utf-8")):
            ours.setdefault(action, set()).add(ref)

    theirs: dict[str, set[str]] = {}
    for path in TEMPLATE_WORKFLOWS.glob("*.tmpl"):
        for action, ref in USES_RE.findall(path.read_text(encoding="utf-8")):
            theirs.setdefault(action, set()).add(ref)

    assert ours, "no pinned actions found in devkit's workflows — the regex or layout changed"
    for action, refs in theirs.items():
        if action not in ours:
            continue
        assert refs == ours[action], (
            f"{action} is pinned {sorted(ours[action])} in devkit's own workflows but "
            f"{sorted(refs)} in templates/ — carry the bump across"
        )


def _tasks_json() -> dict:
    raw = (REPO_ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    # tasks.json is JSONC; VS Code allows the comments, `json` does not.
    return json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE))


def test_vscode_tasks_are_valid_and_labelled():
    """Every task carries a `detail` — it is the only place a one-click action can state
    its blast radius, which is CLAUDE.md's rule for this file."""
    tasks = _tasks_json()["tasks"]
    assert tasks, "no tasks defined"
    for task in tasks:
        assert task.get("detail"), f"task {task['label']!r} has no detail"
        assert task.get("type") == "process", f"task {task['label']!r} is not type=process"


# `testSuite` predates this test and violates the rule below. It survives only
# because run-tests.py happens to use `parse_known_args`, which swallows the empty
# token instead of rejecting it — an accident of that one script, not a safe
# pattern. Listed rather than exempted silently so the deviation stays visible and
# nothing new joins it.
_EMPTY_OPTION_ALLOWED: frozenset[str] = frozenset({"testSuite"})


def test_every_picker_option_supplies_a_real_token():
    """CLAUDE.md's rule for this file: a `${input:...}` picker must produce one real
    token in every branch. An empty string does not vanish from the args array — it
    reaches argparse as a stray positional and the task fails. This is why sweep.py
    and new-project.py both carry a `--dry-run` that is redundant with their default.
    """
    for spec in _tasks_json().get("inputs", []):
        if spec.get("type") != "pickString" or spec["id"] in _EMPTY_OPTION_ALLOWED:
            continue
        for option in spec["options"]:
            # A bare string option is its own value.
            value = option if isinstance(option, str) else option["value"]
            assert value != "", f"input {spec['id']!r} has an empty option; give it a real flag"


def test_every_referenced_input_is_defined():
    """A `${input:typo}` is not an error in VS Code — it prompts for an *undefined*
    input and passes the literal through, so the task fails somewhere further down."""
    data = _tasks_json()
    defined = {spec["id"] for spec in data.get("inputs", [])}
    for task in data["tasks"]:
        for arg in task.get("args", []):
            for referenced in re.findall(r"\$\{input:([^}]+)\}", arg):
                assert referenced in defined, (
                    f"task {task['label']!r} references undefined input {referenced!r}"
                )
