"""Unit tests for the portable state finalizer."""

from pathlib import Path

from conftest import load_module

hook = load_module("scripts/hooks/finalize-state.py")


def _make_state_engine(repo_root: Path, exit_code: int = 0) -> None:
    engine = repo_root / ".claude/skills/state-tools/state-engine.py"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_text(f"import sys\nsys.exit({exit_code})\n", encoding="utf-8")


def test_required_artifacts_audit_vs_other(tmp_path):
    skill_dir = tmp_path / "skill"
    audit = hook.required_artifacts(skill_dir, "audit")
    other = hook.required_artifacts(skill_dir, "modules")
    assert {p.name for p in audit} == {"scan-results.json", "scan-plan.json"}
    assert {p.name for p in other} == {"state-updates.json"}


def test_missing_skill_dir_is_noop(tmp_path):
    assert hook.finalize_state("nope", "modules", tmp_path) == 0


def test_missing_artifacts_is_noop(tmp_path):
    (tmp_path / ".claude/skills/make-tests").mkdir(parents=True)
    assert hook.finalize_state("make-tests", "modules", tmp_path) == 0


def test_success_applies_and_removes_artifacts(tmp_path):
    _make_state_engine(tmp_path, exit_code=0)
    skill_dir = tmp_path / ".claude/skills/make-tests"
    skill_dir.mkdir(parents=True)
    updates = skill_dir / "state-updates.json"
    updates.write_text('{"modules": []}', encoding="utf-8")

    assert hook.finalize_state("make-tests", "modules", tmp_path) == 0
    assert not updates.exists()


def test_audit_success_removes_both_artifacts(tmp_path):
    _make_state_engine(tmp_path, exit_code=0)
    skill_dir = tmp_path / ".claude/skills/audit-design-flaws"
    skill_dir.mkdir(parents=True)
    results = skill_dir / "scan-results.json"
    plan = skill_dir / "scan-plan.json"
    results.write_text("{}", encoding="utf-8")
    plan.write_text("{}", encoding="utf-8")

    assert hook.finalize_state("audit-design-flaws", "audit", tmp_path) == 0
    assert not results.exists() and not plan.exists()


def test_engine_failure_keeps_artifacts(tmp_path):
    _make_state_engine(tmp_path, exit_code=3)
    skill_dir = tmp_path / ".claude/skills/refactor"
    skill_dir.mkdir(parents=True)
    updates = skill_dir / "state-updates.json"
    updates.write_text('{"files": {}}', encoding="utf-8")

    assert hook.finalize_state("refactor", "files", tmp_path) == 3
    assert updates.exists()  # not consumed on failure
