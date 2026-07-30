"""Contract tests: the repo satisfies what the vendored harness already assumes.

**This file is vendored into every consuming project**, so nothing here may assert
a value specific to one project. Every check derives from that repo's own
`.devkit.toml` (via `harness_config`) and from `stop.py`'s own reachability
logic, and any check that cannot be decided from config is left out rather than
guessed at.

The gap this closes. `stop.py` is vendored byte-identical everywhere and dispatches
to five sibling scripts that are *not* vendored with it, and a missing one fails in
whichever direction is least visible:

  - Where `_command_for` declines to build a command, the tier is skipped. Correct --
    a local tooling gap must never block the agent -- and also invisible: a project
    whose `lint-all.py` was never rendered has a Stop gate that reports green having
    run nothing. `stop.py`'s `_REQ_RE` not matching `uv.lock` was this exact shape,
    "silently inert in every uv-native project -- it never fired, so nothing looked
    broken."
  - Where it *does* build one, a missing script is worse than a skip. The interpreter
    exists, so the spawn succeeds and Python exits 2 with "can't open file" --
    indistinguishable from a real finding, and unfixable from the source tree. Every
    generated project blocked its own Stop on a bogus `lock-markers` failure the
    moment a lockfile changed, until `_command_for` learned to check.

Neither is something the runtime should escalate on, so CI is where it gets noticed.
These tests are that second half.

Two things are deliberately NOT asserted, because a project can legitimately lack
them and no config field says whether it should:
  - `check-lock-markers.py` -- the tier is project-owned (its sentinels name that
    project's own lockfiles), so "absent" means "no such tier", not "broken".
  - `archive-session.py` -- reached from the Stop payload, not from config, so
    there is nothing to key a requirement off.
Both are explicit skips in `stop.py` rather than accidental ones.
"""

import dataclasses
import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_module

cfg = load_module("scripts/hooks/harness_config.py")
hook = load_module("scripts/hooks/stop.py")

CFG = hook.CFG
SETTINGS = REPO_ROOT / ".claude" / "settings.json"


def _wires_stop_hook() -> bool:
    """True when this repo actually registers `stop.py` as a Stop hook.

    The gate for every check below that reads the repo's shape off its manifest.
    devkit itself is the case that needs it: it is the harness's source repo, not a
    consumer of it, and its committed `.devkit.toml` is a deliberate *test
    fixture* -- it turns on the DB and frontend tiers so the vendored suite exercises
    them here, and describes a project shaped nothing like devkit. Asserting devkit's
    files against that manifest would fail on a file the manifest never claimed
    devkit has. A repo that wires the hook is making a real claim about itself.
    """
    try:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    entries = settings.get("hooks", {}).get("Stop", [])
    if not isinstance(entries, list):
        return False
    return any(
        "stop.py" in h.get("command", "")
        for entry in entries
        if isinstance(entry, dict)
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    )


consumes_harness = pytest.mark.skipif(
    not _wires_stop_hook(),
    reason="repo does not wire stop.py as a Stop hook (harness source repo, not a consumer)",
)


# --- the committed manifest is spelled correctly ------------------------------
# Ungated: a typo in a fixture manifest is still a typo.


def _toml_schema() -> dict[str, frozenset[str]]:
    """Legal keys per `.devkit.toml` table, mirroring `from_dict`.

    The three hand-mapped tables are spelled out because `from_dict` renames their
    keys (`[paths] app` -> `app_dir`); the rest map 1:1 onto their dataclass, so
    they are derived and cannot drift as fields are added.
    """
    fields = lambda dc: frozenset(f.name for f in dataclasses.fields(dc))  # noqa: E731
    return {
        "project": frozenset({"env_prefix"}),
        "paths": frozenset({"app", "tests", "unit_tests"}),
        "stop": frozenset({"finalize_targets"}),
        "db": fields(cfg.DbConfig),
        "frontend": fields(cfg.FrontendConfig),
        "python": fields(cfg.PythonConfig),
        "bash": fields(cfg.BashConfig),
    }


def test_manifest_has_no_unknown_keys():
    """An unrecognised key silently disables the tier it was meant to configure.

    `from_dict` is all `raw.get(name, default)` and never inspects what it did not
    consume, by design -- a config typo must not break the Stop hook. The cost is
    that `db_servce = "db"` reads as "no db_service was set", the DB tier quietly
    falls back to a default that does not match the compose file, and the tier stops
    doing anything. Nothing raises, nothing logs, and CI stays green. This is the
    only place that difference is ever visible.
    """
    tomllib = pytest.importorskip("tomllib")
    manifest = REPO_ROOT / cfg.MANIFEST_NAME
    if not manifest.exists():
        pytest.skip(f"no {cfg.MANIFEST_NAME} (harness runs on neutral defaults)")

    with manifest.open("rb") as fh:
        raw = tomllib.load(fh)

    schema = _toml_schema()
    unknown = [k for k in raw if k not in schema]
    assert not unknown, f"unknown table(s) in {cfg.MANIFEST_NAME}: {sorted(unknown)}"

    for table, allowed in schema.items():
        section = raw.get(table)
        if not isinstance(section, dict):
            continue
        # `[db.test_env]` is an open map of env-var names -> defaults, so its keys
        # are the project's to choose; only the table itself must be spelled right.
        extra = sorted(set(section) - allowed)
        assert not extra, f"unknown key(s) in [{table}]: {extra} (legal: {sorted(allowed)})"


# --- the scripts stop.py dispatches to are actually there ---------------------


@consumes_harness
def test_unconditional_lint_tier_has_its_script():
    """`lint-all.py` backs the one tier that runs on every non-empty diff.

    `select_checks` adds CHECK_LINT whenever anything changed at all -- there is no
    config field that turns it off. If the script is missing, the Stop gate's only
    always-on check is a no-op in every session.
    """
    assert hook.LINT_ALL.exists(), f"{hook.LINT_ALL.relative_to(REPO_ROOT)} is missing"
    assert hook._command_for(hook.CHECK_LINT) is not None


@consumes_harness
def test_the_remediation_command_exists():
    """The failure message tells the agent to run `run-tests.py`; it must be there.

    `_print_verify_failures` signs off with "Re-run locally: ... | python
    scripts/run-tests.py --fast". A gate whose advice on failure is a path that does
    not exist sends the agent in a circle at precisely the worst moment.
    """
    assert (REPO_ROOT / "scripts" / "run-tests.py").exists()


@consumes_harness
def test_finalize_state_present_when_the_manifest_names_targets():
    """`[stop] finalize_targets` non-empty means `finalize-state.py` gets spawned.

    Unconditionally, once per Stop, per target (`main()` loops over FINALIZE_TARGETS
    before anything else). The subprocess sends both streams to DEVNULL and its exit
    code is never read, so a missing script here is the quietest failure in the
    harness: every configured skill silently stops being finalized.
    """
    if not CFG.finalize_targets:
        pytest.skip("no [stop] finalize_targets configured")
    assert hook.FINALIZE_STATE.exists(), (
        f"[stop] finalize_targets names {len(CFG.finalize_targets)} target(s) but "
        f"{hook.FINALIZE_STATE.relative_to(REPO_ROOT)} is missing"
    )


@consumes_harness
def test_optional_tiers_skip_explicitly_when_absent():
    """A tier whose project-owned script is absent must resolve to None, not argv.

    Guards the `_command_for` early-returns: without them a missing script reaches
    `subprocess.run` and is skipped by the OSError handler instead, which cannot be
    told apart from the script existing and failing to start.
    """
    for check in (hook.CHECK_LINT, hook.CHECK_LOCKS):
        spec = hook._command_for(check)
        script = Path(spec[0][1]) if spec else None
        assert spec is None or script.exists(), f"{check} resolved to a missing {script}"


# --- the manifest's paths describe files that exist ---------------------------


@consumes_harness
def test_configured_paths_exist():
    """`[paths]` drives which checks a diff selects; a stale entry silences them.

    `host_test_targets` and `select_checks` decide entirely by string prefix
    (`path.startswith(CFG.app_dir)`), so renaming `app/` to `src/` without updating
    the manifest does not error -- no changed path matches any more, the DB tier
    resolves to an empty target list, and verification passes by running nothing.
    """
    for label, value in (
        ("[paths] app", CFG.app_dir),
        ("[paths] tests", CFG.tests_dir),
        ("[paths] unit_tests", CFG.unit_tests),
    ):
        assert (REPO_ROOT / value).is_dir(), f"{label} = {value!r} is not a directory"


@consumes_harness
def test_frontend_paths_exist_when_the_tier_is_on():
    """Same prefix-matching trap, one tier over: `_is_frontend` is a `startswith`."""
    if not CFG.frontend.enabled:
        pytest.skip("project has no frontend tier")
    assert (REPO_ROOT / CFG.frontend.dir).is_dir(), f"[frontend] dir = {CFG.frontend.dir!r}"
    assert (REPO_ROOT / CFG.frontend.src).is_dir(), f"[frontend] src = {CFG.frontend.src!r}"


# --- the instruction tier -----------------------------------------------------
# The vendored prose is drift-checked by `sync-devkit.py --check` like any other
# MANIFEST file. What that cannot see is a CLAUDE.md that *restates* the vendored
# policy instead of pointing at it: the copy is not in the MANIFEST, so it drifts
# freely while looking every bit as authoritative. That is the exact failure being
# undone here -- the policy lived inline in each repo, was copied forward by hand, and
# devkit's template had already lost a clause of the testing mandate.

VENDORED_POLICY = ".claude/rules/engineering.md"

# Sentences that belong to the vendored policy. Matching is on the distinctive middle
# of each clause, not the whole sentence, so a reworded copy is still caught -- a
# verbatim-only check would pass the moment someone paraphrased, which is precisely how
# the original drift happened.
POLICY_CLAUSES = (
    "gaps are not acceptable",
    "silently work around a bad instruction",
)


def _instruction_files() -> list[Path]:
    """Every CLAUDE.md in the repo, skipping generated mirrors and vendor trees."""
    skip = (".agents", "node_modules", ".venv", "templates")
    return [
        p
        for p in REPO_ROOT.rglob("CLAUDE.md")
        if not any(part in skip for part in p.relative_to(REPO_ROOT).parts)
    ]


@consumes_harness
def test_vendored_policy_is_present():
    """The rule every project's CLAUDE.md defers to has to actually be there.

    A dangling pointer is worse than a restatement: the CLAUDE.md says the authority
    lives elsewhere, and there is no elsewhere, so the policy silently applies nowhere.
    """
    assert (REPO_ROOT / VENDORED_POLICY).is_file(), (
        f"{VENDORED_POLICY} is missing -- run `python scripts/sync-devkit.py --pull`"
    )


@consumes_harness
def test_claude_md_defers_to_the_vendored_policy_rather_than_restating_it():
    """A CLAUDE.md that restates vendored policy has forked it.

    The copy reads as authoritative, is not in the MANIFEST, and so is not drift-checked
    -- the two diverge the first time either is edited, and the version an agent actually
    follows is whichever it happened to load. Projects add their own specifics (fixtures,
    isolation rules, what to mock); they cite the shared clauses.
    """
    policy_text = (REPO_ROOT / VENDORED_POLICY).read_text(encoding="utf-8")
    for path in _instruction_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT)
        for clause in POLICY_CLAUSES:
            assert clause in policy_text, f"stale POLICY_CLAUSES entry: {clause!r}"
            assert clause not in text, (
                f"{rel} restates vendored policy ({clause!r}). Cite "
                f"{VENDORED_POLICY} instead -- a second copy is not drift-checked."
            )


@consumes_harness
def test_agents_mirror_is_in_sync_when_present():
    """`AGENTS.md` is generated from `CLAUDE.md`; a divergence means someone edited it.

    The mirror exists so Codex-style harnesses read the same instructions Claude does,
    and it is only trustworthy while it is byte-identical. A hand-edit is silent in the
    worst way: both files look authoritative, nothing regenerates on read, and the two
    harnesses quietly follow different rules from that point on. `--pull` cannot catch
    it either, since the mirror is generated per project and not in the MANIFEST.
    """
    mirrors = [
        (p, p.with_name("AGENTS.md"))
        for p in _instruction_files()
        if p.with_name("AGENTS.md").exists()
    ]
    if not mirrors:
        pytest.skip("project does not mirror CLAUDE.md to AGENTS.md")
    for claude, agents in mirrors:
        assert agents.read_text(encoding="utf-8") == claude.read_text(encoding="utf-8"), (
            f"{agents.relative_to(REPO_ROOT)} has drifted from "
            f"{claude.relative_to(REPO_ROOT)} -- edit CLAUDE.md and re-run "
            "`python scripts/sync-agents-context.py`; never hand-edit the mirror"
        )


@consumes_harness
def test_vendored_skills_are_not_locally_edited():
    """Vendored skills carry no project's default branch, paths, or service names.

    `sync-devkit.py --check` already enforces this byte-for-byte, so this is the
    cheaper signal that says *why* when it trips: `master` was written through `ship`
    and `task` while `task_branch.detect_default_branch()` resolved the real branch at
    runtime, so the prose disagreed with the script in every `main`-based project.
    """
    skills = REPO_ROOT / ".claude" / "skills"
    if not skills.is_dir():
        pytest.skip("no vendored skills")
    vendored = {"ship", "task", "retro", "test-skill"}
    for name in sorted(vendored):
        skill = skills / name / "SKILL.md"
        if not skill.is_file():
            continue
        text = skill.read_text(encoding="utf-8")
        assert "master" not in text, (
            f"{skill.relative_to(REPO_ROOT)} names a specific default branch; the "
            "vendored copy must defer to the one detect_default_branch() resolves"
        )


def test_fix_pre_commit_has_project_known_fixes_and_reads_them_first():
    """The vendored fixer prose depends on project-owned recurring-fix state."""
    skill_dir = REPO_ROOT / ".claude" / "skills" / "fix-pre-commit"
    skill = skill_dir / "SKILL.md"
    if not skill.is_file():
        pytest.skip("fix-pre-commit skill is not installed")

    known_fixes = skill_dir / "known-fixes.md"
    assert known_fixes.is_file(), (
        f"{known_fixes.relative_to(REPO_ROOT)} is missing -- fixers require a "
        "project-owned known-fixes table"
    )

    text = skill.read_text(encoding="utf-8").lower()
    assert "single parallel" in text or "in parallel" in text
    assert "known-fix short-circuit" in text
