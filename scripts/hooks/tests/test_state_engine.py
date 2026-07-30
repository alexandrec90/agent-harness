"""Unit tests for the skill state engine.

This module had no tests at all in the repo it was extracted from, which is part of
why the `audit` schema's project coupling went unnoticed: nothing exercised it
outside the one repo whose layout it hard-coded.

Vendored tier: every path here is synthesised under `tmp_path`. Nothing reads the
repo the suite happens to run in.
"""

import json
from pathlib import Path

from conftest import load_module

engine = load_module(".claude/skills/state-tools/state-engine.py")


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- check specs: the project-owned seam --------------------------------------


def test_no_spec_file_yields_no_checks(tmp_path):
    assert engine.load_check_specs(tmp_path, "audit-skill").specs == ()


def test_specs_are_read_from_project_data(tmp_path):
    _write(
        tmp_path / ".claude/skills/aud/check-specs.json",
        {"checks": [{"id": "A", "kind": "local", "include": ["src/**/*.py"]}]},
    )
    config = engine.load_check_specs(tmp_path, "aud")
    assert [s.check_id for s in config.specs] == ["A"]
    assert config.specs[0].include_globs == ("src/**/*.py",)


def test_malformed_rows_are_dropped_not_fatal():
    config = engine.parse_check_specs(
        {
            "checks": [
                {"id": "A", "kind": "local", "include": ["a/**"]},
                {"id": "B", "kind": "sideways", "include": ["b/**"]},  # bad kind
                {"kind": "local", "include": ["c/**"]},  # no id
                {"id": "D", "kind": "cross", "include": "not-a-list"},
                "nonsense",
            ]
        }
    )
    assert [s.check_id for s in config.specs] == ["A"]


def test_unparseable_spec_file_degrades_to_empty(tmp_path):
    path = tmp_path / ".claude/skills/aud/check-specs.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert engine.load_check_specs(tmp_path, "aud").specs == ()


def test_project_excludes_are_additive_and_claude_is_not_optional():
    config = engine.parse_check_specs({"checks": [], "exclude_prefixes": ["generated/"]})
    assert "generated/" in config.exclude_prefixes
    # A project cannot opt back into scanning .claude/: an audit that reads its own
    # state files has a result that depends on its own output.
    assert ".claude/" in config.exclude_prefixes


# --- signature stability ------------------------------------------------------


def test_check_signature_is_stable_for_the_same_definition():
    spec = engine.CheckSpec("A", "local", ("a/**",))
    assert engine.check_signature(spec) == engine.check_signature(spec)


def test_check_signature_changes_when_the_definition_changes():
    base = engine.CheckSpec("A", "local", ("a/**",))
    assert engine.check_signature(base) != engine.check_signature(
        engine.CheckSpec("A", "local", ("a/**", "b/**"))
    )
    assert engine.check_signature(base) != engine.check_signature(
        engine.CheckSpec("A", "cross", ("a/**",))
    )


# --- planning -----------------------------------------------------------------


def _plan(tmp_path, state=None, include=("src/**/*.py",)):
    config = engine.CheckPlanConfig(specs=(engine.CheckSpec("A", "local", include),))
    return engine.build_audit_plan(tmp_path, state or {}, config)


def test_unseen_files_are_targets(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("x", encoding="utf-8")
    plan = _plan(tmp_path)
    assert plan["checks"]["A"]["targets"] == ["src/a.py"]


def test_unchanged_passing_file_is_skipped(tmp_path):
    (tmp_path / "src").mkdir()
    src = tmp_path / "src/a.py"
    src.write_text("x", encoding="utf-8")

    first = _plan(tmp_path)
    state = {
        "checks": {
            "A": {
                "checkSignature": first["checks"]["A"]["checkSignature"],
                "files": {
                    "src/a.py": {"contentHash": engine.file_hash(src), "status": "pass"},
                },
            }
        }
    }
    second = _plan(tmp_path, state)
    assert second["checks"]["A"]["targets"] == []
    assert second["checks"]["A"]["skipped"] == ["src/a.py"]


def test_active_violation_is_rescanned_even_when_unchanged(tmp_path):
    (tmp_path / "src").mkdir()
    src = tmp_path / "src/a.py"
    src.write_text("x", encoding="utf-8")

    first = _plan(tmp_path)
    state = {
        "checks": {
            "A": {
                "checkSignature": first["checks"]["A"]["checkSignature"],
                "files": {
                    "src/a.py": {"contentHash": engine.file_hash(src), "status": "violation"},
                },
            }
        }
    }
    assert _plan(tmp_path, state)["checks"]["A"]["targets"] == ["src/a.py"]


def test_deleted_file_is_reported_as_removed(tmp_path):
    (tmp_path / "src").mkdir()
    state = {"checks": {"A": {"checkSignature": "x", "files": {"src/gone.py": {}}}}}
    assert _plan(tmp_path, state)["checks"]["A"]["removed"] == ["src/gone.py"]


def test_excluded_directories_never_become_targets(tmp_path):
    for bad in ("node_modules", "__pycache__", ".venv"):
        d = tmp_path / "src" / bad
        d.mkdir(parents=True)
        (d / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "src/real.py").write_text("x", encoding="utf-8")
    assert _plan(tmp_path)["checks"]["A"]["targets"] == ["src/real.py"]


# --- apply: modules -----------------------------------------------------------


def test_modules_merge_preserves_unlisted_rows():
    state = {"modules": [{"module": "a", "gaps_found": 3}]}
    out = engine.apply_module_state(
        state=state, updates={"modules": [{"module": "b"}]}, today="2026-07-30"
    )
    assert [m["module"] for m in out["modules"]] == ["a", "b"]
    assert out["modules"][0]["gaps_found"] == 3


def test_modules_merge_updates_in_place_and_sorts():
    state = {"modules": [{"module": "b"}, {"module": "a", "gaps_found": 1}]}
    out = engine.apply_module_state(
        state=state,
        updates={"modules": [{"module": "a", "gaps_found": 0, "test_file": "t.py"}]},
        today="2026-07-30",
    )
    assert [m["module"] for m in out["modules"]] == ["a", "b"]
    assert out["modules"][0] == {
        "module": "a",
        "gaps_found": 0,
        "test_file": "t.py",
        "last_reviewed": "2026-07-30",
    }


def test_modules_ignores_malformed_rows():
    out = engine.apply_module_state(
        state={"modules": "not a list"},
        updates={"modules": [{"module": ""}, "junk", {"no_module": 1}]},
        today="d",
    )
    assert out["modules"] == []


# --- apply: files -------------------------------------------------------------


def test_files_string_patch_sets_hash_and_date():
    out = engine.apply_files_state(state={}, updates={"files": {"a.py": "abc"}}, today="2026-07-30")
    assert out["files"]["a.py"] == {"git_hash": "abc", "refactored_at": "2026-07-30"}


def test_files_remove_flag_deletes_the_entry():
    state = {"files": {"a.py": {"git_hash": "x"}}}
    out = engine.apply_files_state(
        state=state, updates={"files": {"a.py": {"remove": True}}}, today="d"
    )
    assert "a.py" not in out["files"]


def test_files_null_patch_is_a_noop_not_a_delete():
    """None means "no update", which must not be confused with {"remove": true}."""
    state = {"files": {"a.py": {"git_hash": "x"}}}
    out = engine.apply_files_state(state=state, updates={"files": {"a.py": None}}, today="d")
    assert out["files"]["a.py"] == {"git_hash": "x"}


# --- CLI ----------------------------------------------------------------------


def test_plan_without_specs_fails_loudly_rather_than_writing_an_empty_plan(tmp_path, capsys):
    rc = engine.main(["plan", "--repo-root", str(tmp_path), "--skill", "aud"])
    assert rc == 1
    assert "check-specs.json" in capsys.readouterr().err
    assert not (tmp_path / ".claude/skills/aud/scan-plan.json").exists()


def test_apply_files_roundtrip_through_the_cli(tmp_path):
    _write(tmp_path / ".claude/skills/refactor/state-updates.json", {"files": {"a.py": "sha"}})
    rc = engine.main(
        ["apply", "--repo-root", str(tmp_path), "--skill", "refactor", "--schema", "files"]
    )
    assert rc == 0
    state = json.loads((tmp_path / ".claude/skills/refactor/state.json").read_text())
    assert state["files"]["a.py"]["git_hash"] == "sha"
