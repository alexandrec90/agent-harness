"""Tests for the project generator.

The important ones are at the bottom: they render the whole template tree for
several feature combinations and then **parse every generated file** with a real
parser. A template that renders without raising can still emit a JSON file with a
trailing comma or a compose file with a dangling `volumes:` key, and that failure
would otherwise surface only when a human ran `docker compose up` in a brand-new
repo.
"""

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest
from support import REPO_ROOT, TEMPLATES, devkit_ports, harness_config, load_script

new_project = load_script("scripts/new-project.py")
GeneratorError = new_project.GeneratorError


def make_args(**overrides):
    """The argparse namespace `plan()` expects, with the CLI's own defaults."""
    base = {
        "name": "demo_project",
        "description": "A demo.",
        "display_name": "",
        "parent": "",
        "github_owner": "alexandrec90",
        "python_version": "3.12",
        "default_branch": "main",
        "devkit_ref": "v0.1.0",
        "db_url_scheme": "postgresql+psycopg",
        "src_layout": False,
        "preset": None,
        "worktree": True,
        "remote": True,
        "dry_run": True,
        **{f: False for f in new_project.FEATURES},
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def registry():
    return devkit_ports.load(REPO_ROOT)


# --------------------------------------------------------------------------
# Naming and path conventions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [("sports-betting", "sports_betting"), ("IBKR_Trader", "ibkr_trader"), ("a.b", "a_b")],
)
def test_slugify_package_produces_an_importable_name(name, expected):
    assert new_project.slugify_package(name) == expected


@pytest.mark.parametrize("name", ["-leading", "has space", "has/slash", "", "has:colon"])
def test_invalid_names_are_rejected(name):
    # The name becomes a directory, a Python package, AND COMPOSE_PROJECT_NAME.
    # Docker rejects some of these only at `up` time, long after generation.
    with pytest.raises(GeneratorError, match="invalid project name"):
        new_project.validate_name(name)


@pytest.mark.parametrize("name", ["sports_betting", "carameli", "ibkr-trader", "x1"])
def test_valid_names_are_accepted(name):
    assert new_project.validate_name(name) == name


@pytest.mark.parametrize(
    "source,expected",
    [
        ("dot-gitignore.tmpl", ".gitignore"),
        ("dot-claude/settings.json.tmpl", ".claude/settings.json"),
        ("dot-github/workflows/pr-gate.yml.tmpl", ".github/workflows/pr-gate.yml"),
        ("scripts/notify.py", "scripts/notify.py"),
        ("Dockerfile.tmpl", "Dockerfile"),
    ],
)
def test_dot_and_tmpl_conventions_apply_to_every_path_segment(source, expected):
    # `dot-` exists because a literal `.gitignore` inside templates/ would take
    # effect on devkit's own repo and make git ignore the templates themselves.
    assert Path(new_project._destination_name(Path(source))).as_posix() == expected


def test_no_template_ships_a_literal_dotfile():
    # Guards the reason `dot-` exists — a regression here silently breaks devkit's
    # own repo rather than the generated one, which is much harder to notice.
    offenders = [
        p.relative_to(TEMPLATES).as_posix()
        for p in TEMPLATES.rglob("*")
        if any(part.startswith(".") for part in p.relative_to(TEMPLATES).parts)
    ]
    assert offenders == []


# --------------------------------------------------------------------------
# Feature selection
# --------------------------------------------------------------------------


def test_feature_directories_are_only_included_when_enabled():
    off = {f: False for f in new_project.FEATURES}
    core_only = {d for _, d in new_project.iter_template_files(off)}
    assert Path("docker-compose.yml") not in core_only
    assert Path("CLAUDE.md") in core_only

    with_docker = {d for _, d in new_project.iter_template_files({**off, "docker": True})}
    assert Path("docker-compose.yml") in with_docker


def test_archive_feature_brings_its_rule_file():
    off = {f: False for f in new_project.FEATURES}
    files = {d.as_posix() for _, d in new_project.iter_template_files({**off, "archive": True})}
    assert ".claude/rules/data-lake.md" in files


def test_alembic_implies_postgres_via_the_cli(tmp_path):
    result = new_project.main(
        ["demo_project", "--with-alembic", "--parent", str(tmp_path), "--no-remote"]
    )
    assert result == 0  # dry run


@pytest.mark.parametrize("preset", sorted(new_project.PRESETS))
def test_every_preset_names_only_real_features(preset):
    # A typo'd feature in a preset would silently do nothing — `setattr` on an
    # argparse namespace happily creates a new attribute no template ever reads.
    assert set(new_project.PRESETS[preset]) <= set(new_project.FEATURES)


@pytest.mark.parametrize("preset", sorted(new_project.PRESETS))
def test_every_preset_generates(tmp_path, preset):
    assert (
        new_project.main(
            ["demo_project", "--preset", preset, "--parent", str(tmp_path), "--no-remote"]
        )
        == 0
    )


def test_explicit_flags_add_to_a_preset_rather_than_replacing_it(tmp_path):
    args = make_args(parent=str(tmp_path), preset="data", redis=True)
    for feature in new_project.PRESETS["data"]:
        setattr(args, feature, True)
    the_plan = new_project.plan(args, registry())
    assert the_plan.context["archive"] is True  # from the preset
    assert the_plan.context["redis"] is True  # from the explicit flag


def test_worktree_env_only_offsets_services_the_project_uses():
    args = make_args(postgres=True, docker=True, app_service=True)
    the_plan = new_project.plan(args, registry())
    # No redis feature -> no REDIS_HOST_PORT to get wrong later.
    assert "REDIS_HOST_PORT" not in the_plan.worktree_env
    assert "DB_HOST_PORT" in the_plan.worktree_env
    assert the_plan.worktree_env["COMPOSE_PROJECT_NAME"] == "demo_project-b"


def test_worktree_gets_a_different_slot_than_the_primary():
    the_plan = new_project.plan(make_args(postgres=True), registry())
    assert the_plan.context["slot"] != the_plan.context["worktree_slot"]


def test_generating_into_a_non_empty_directory_is_refused(tmp_path):
    target = tmp_path / "demo_project"
    target.mkdir()
    (target / "existing.txt").write_text("keep me")
    with pytest.raises(GeneratorError, match="already exists and is not empty"):
        new_project.plan(make_args(parent=str(tmp_path)), registry())


def test_generating_into_an_empty_existing_directory_is_allowed(tmp_path):
    (tmp_path / "demo_project").mkdir()
    assert new_project.plan(make_args(parent=str(tmp_path)), registry())


# --------------------------------------------------------------------------
# The real check: render everything, then parse everything
# --------------------------------------------------------------------------

FEATURE_MATRIX = [
    pytest.param({}, id="bare"),
    pytest.param({"postgres": True, "app_service": True}, id="service"),
    pytest.param({"postgres": True, "redis": True, "app_service": True}, id="service+redis"),
    pytest.param({"postgres": True, "alembic": True, "app_service": True}, id="service+alembic"),
    pytest.param({"postgres": True, "archive": True}, id="data"),
    pytest.param({"frontend": True, "postgres": True, "app_service": True}, id="fullstack"),
    pytest.param(
        {f: True for f in new_project.FEATURES},
        id="everything",
    ),
]


def generate(tmp_path: Path, features: dict) -> Path:
    """Render the tree for one feature set into tmp_path and return the root."""
    args = make_args(parent=str(tmp_path), **features)
    if args.alembic:
        args.postgres = True
    if args.app_service or args.postgres or args.redis or args.frontend:
        args.docker = True
    the_plan = new_project.plan(args, registry())
    the_plan.root.mkdir(parents=True, exist_ok=True)
    new_project.render_tree(the_plan, dry_run=False)
    new_project.write_package(the_plan, dry_run=False)
    return the_plan.root


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_every_feature_combination_renders(tmp_path, features):
    root = generate(tmp_path, features)
    assert (root / "CLAUDE.md").exists()
    assert (root / ".agent-harness.toml").exists()


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_no_unrendered_template_tag_survives(tmp_path, features):
    # The failure this catches: an inline `{{#flag}}` or a typo'd `{{ var }}` that
    # ends up copied verbatim into a generated config file.
    root = generate(tmp_path, features)
    for path in root.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            leftovers = re.findall(r"\{\{[#^/]?\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}", text)
            assert leftovers == [], f"{path.relative_to(root)} kept {leftovers}"


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_toml_parses(tmp_path, features):
    root = generate(tmp_path, features)
    for name in (".agent-harness.toml", "pyproject.toml", "ruff.toml"):
        with (root / name).open("rb") as fh:
            tomllib.load(fh)


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_harness_manifest_is_readable_by_harness_config(tmp_path, features):
    # The generated seam must load in the *actual* loader the vendored hooks use —
    # not merely be valid TOML. A schema mismatch here would silently degrade every
    # new project to neutral defaults, which looks like "the harness does nothing".
    root = generate(tmp_path, features)
    config = harness_config.load(root)
    assert config.env_prefix == "DEMO_PROJECT"
    assert config.db.enabled is bool(features.get("postgres"))
    assert config.frontend.enabled is bool(features.get("frontend"))


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_json_parses(tmp_path, features):
    root = generate(tmp_path, features)
    json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    # tasks.json is JSONC — strip the line comments the way VS Code does.
    tasks = (root / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in tasks.splitlines() if not line.lstrip().startswith("//")
    )
    parsed = json.loads(stripped)
    assert parsed["version"] == "2.0.0"
    assert parsed["tasks"], "a generated project with no tasks is not wired up"


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_every_task_has_a_label_and_a_detail(tmp_path, features):
    # The convention CLAUDE.md states: `detail` is the second line of the quick-pick
    # and the only place a one-click action can declare its cost or blast radius.
    root = generate(tmp_path, features)
    tasks = (root / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in tasks.splitlines() if not line.lstrip().startswith("//")
    )
    for task in json.loads(stripped)["tasks"]:
        assert task.get("label"), task
        assert task.get("detail"), f"{task['label']} has no detail"
        assert re.match(r"^[A-Z][A-Za-z]*: [A-Z]", task["label"]), (
            f"{task['label']} breaks the 'Domain: Title Case Action' convention"
        )


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_every_task_input_referenced_is_defined(tmp_path, features):
    # A `${input:foo}` with no matching entry makes VS Code fail the task at click
    # time with an opaque error — and only for the feature combination that emits it.
    root = generate(tmp_path, features)
    text = (root / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    stripped = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    parsed = json.loads(stripped)
    defined = {i["id"] for i in parsed.get("inputs", [])}
    referenced = set(re.findall(r"\$\{input:([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(parsed)))
    assert referenced <= defined, f"undefined inputs: {referenced - defined}"


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_the_generated_diagnostic_scripts_actually_run_and_pass(tmp_path, features):
    """`lint-all.py` and `run-tests.py` must be green in a fresh project.

    These are what `tasks.json` and the PR gate invoke, so a failure here is a red
    gate on a project's first PR. Asserting `ruff`/`pytest` pass directly is not
    enough — it misses everything about how these wrappers *call* them. It did:
    `lint-all.py` ran `mypy .`, which type-checked the vendored harness (upstream
    code this repo may not edit) and reported 7 unfixable errors.
    """
    import subprocess

    root = generate(tmp_path, features)
    (root / "logs").mkdir(exist_ok=True)
    for script in ("scripts/run-tests.py", "scripts/lint-all.py"):
        result = subprocess.run(
            [sys.executable, script], cwd=root, capture_output=True, text=True
        )
        artifact = {
            "scripts/run-tests.py": root / "logs" / "test-failures.log",
            "scripts/lint-all.py": root / "logs" / "lint-errors.log",
        }[script]
        detail = artifact.read_text(encoding="utf-8") if artifact.exists() else result.stdout
        assert result.returncode == 0, f"{script} failed in a fresh project:\n{detail}"


def test_mypy_scope_excludes_the_vendored_harness(tmp_path):
    # Belt and braces, and both are load-bearing: the pyproject `exclude` covers
    # directory recursion, MYPY_SCOPE covers someone running `mypy .` by hand.
    root = generate(tmp_path, {})
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "sync-harness" in pyproject and "exclude" in pyproject
    lint_all = (root / "scripts" / "lint-all.py").read_text(encoding="utf-8")
    scope = re.search(r"MYPY_SCOPE = \[(.*?)\]", lint_all, re.S).group(1)
    assert "scripts" not in scope


def test_compose_publishes_every_port_through_a_variable(tmp_path):
    # The whole reason parallel worktrees work. A literal host port here is the bug
    # that makes two checkouts un-runnable at the same time.
    root = generate(tmp_path, {f: True for f in new_project.FEATURES})
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    published = re.findall(r'^\s+- "([^"]+)"', compose, flags=re.MULTILINE)
    assert published, "no published ports found — the assertion is not testing anything"
    for mapping in published:
        assert mapping.startswith("${"), f"hardcoded host port: {mapping}"


def test_compose_omits_the_volumes_key_when_nothing_declares_one(tmp_path):
    root = generate(tmp_path, {"app_service": True})
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "\nvolumes:" not in compose


def test_dockerignore_excludes_the_env_file(tmp_path):
    # `.env` is gitignored so it never reaches the repo, but `COPY . .` would put it
    # in the image without this line.
    root = generate(tmp_path, {"postgres": True, "app_service": True})
    assert "\n.env\n" in (root / ".dockerignore").read_text(encoding="utf-8")


def test_env_example_ports_agree_with_the_registry(tmp_path):
    root = generate(tmp_path, {"postgres": True, "app_service": True})
    text = (root / ".env.example").read_text(encoding="utf-8")
    slot = new_project.plan(
        make_args(parent=str(tmp_path / "x"), postgres=True, app_service=True), registry()
    ).context["slot"]
    expected = registry().services["db"] + slot
    assert f"DB_HOST_PORT={expected}" in text
    # The DATABASE_URL must agree with DB_HOST_PORT — that pairing is the first
    # thing that breaks when a worktree's ports are edited by hand.
    assert f"127.0.0.1:{expected}/" in text


def test_pr_gate_pins_a_devkit_tag_and_sets_the_drift_variable(tmp_path):
    root = generate(tmp_path, {})
    gate = (root / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    pinned = re.search(r"ref: (\S+)", gate).group(1)
    # Never a branch: one bad devkit commit must not redden every consuming repo.
    assert pinned not in {"main", "master", "HEAD"}
    assert pinned.startswith("v"), f"expected a version tag, got {pinned!r}"
    # Without this the drift check exits 0 having compared nothing.
    assert "AGENT_HARNESS_DIR:" in gate


def test_default_devkit_ref_is_resolved_from_the_newest_tag():
    # Regression: the default was hardcoded `v0.1.0` and stayed there after v0.2.0
    # shipped, so a generated project pinned a tag whose vendored harness predated
    # the fix — its drift job failed on the first PR.
    tag = new_project.latest_devkit_tag()
    assert tag is not None, "devkit has no tags; the fallback would be used"
    assert tag != "v0.1.0" or new_project.FALLBACK_DEVKIT_REF != "v0.1.0"


def test_fallback_devkit_ref_tracks_the_newest_tag():
    """The fallback must not rot the way the old hardcoded default did.

    It only fires when git cannot resolve a tag, so a stale value here is invisible
    to everyone on the fast path and breaks exactly the users who are not — the
    hardest kind of staleness to notice. Failing here at release time is the cheap
    version of that discovery. If this is red, bump FALLBACK_DEVKIT_REF to the tag
    you just pushed.
    """
    tag = new_project.latest_devkit_tag()
    if tag is None:
        pytest.skip("no tags to compare against")
    assert tag == new_project.FALLBACK_DEVKIT_REF, (
        f"FALLBACK_DEVKIT_REF is {new_project.FALLBACK_DEVKIT_REF!r} but devkit's "
        f"newest tag is {tag!r} — bump the constant"
    )


def test_latest_devkit_tag_is_none_outside_a_git_repo(tmp_path):
    assert new_project.latest_devkit_tag(tmp_path) is None


def test_manifest_paths_are_read_from_the_sync_tool():
    # Read from sync-harness.py rather than duplicated, so the two cannot disagree.
    manifest = new_project._read_manifest_paths(REPO_ROOT)
    assert "scripts/sync-harness.py" in manifest
    assert "scripts/hooks/harness_config.py" in manifest


def test_harness_comparison_returns_none_for_an_unknown_ref():
    # Unknown ref must be reported as "could not compare", never as "no differences" —
    # a silent empty list would suppress the stale-pin warning entirely.
    assert new_project.harness_files_matching_ref("v99.99.99-nope") is None


def test_harness_comparison_reports_no_differences_against_a_clean_tree(tmp_path):
    # Build a throwaway repo whose committed content matches its working tree, and
    # confirm the comparison finds nothing. Proves the check can return [] at all —
    # otherwise the warning would fire forever and be trained away as noise.
    repo = tmp_path / "fake_devkit"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "sync-harness.py").write_text(
        'MANIFEST = ("scripts/hooks/harness_config.py",)\n', encoding="utf-8"
    )
    (repo / "scripts" / "hooks" / "harness_config.py").write_text("x = 1\n", encoding="utf-8")
    import subprocess

    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
        ["git", "tag", "v1.0.0"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    assert new_project.harness_files_matching_ref("v1.0.0", repo) == []

    (repo / "scripts" / "hooks" / "harness_config.py").write_text("x = 2\n", encoding="utf-8")
    assert new_project.harness_files_matching_ref("v1.0.0", repo) == [
        "scripts/hooks/harness_config.py"
    ]


def test_claude_settings_only_wires_hooks_that_are_actually_vendored(tmp_path):
    # A hook command pointing at a script the MANIFEST does not ship fires on every
    # turn and fails silently. Carameli's settings reference several such
    # project-local hooks; a generated project must not inherit those.
    manifest_text = (REPO_ROOT / "scripts" / "sync-harness.py").read_text(encoding="utf-8")
    root = generate(tmp_path, {})
    settings = (root / ".claude" / "settings.json").read_text(encoding="utf-8")
    for referenced in re.findall(r"\$\{CLAUDE_PROJECT_DIR:-\.\}/([^\"]+?\.(?:py|sh))", settings):
        assert referenced in manifest_text, (
            f"{referenced} is wired as a hook but is not in sync-harness.py's MANIFEST"
        )
