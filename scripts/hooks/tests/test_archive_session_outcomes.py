"""Unit tests for the error-outcome ledger in archive-session.py.

Covers reconcile_outcomes (the error -> fix attempt -> did-it-stick state machine),
outcome_counts (per-skill rollup), and the profile injection of fix_outcomes.
"""

from conftest import load_module

arch = load_module("scripts/hooks/archive-session.py")


def _seg(skill, errors=(), edited=()):
    return {"skill": skill, "error_snippets": list(errors), "files_edited": list(edited)}


def test_new_error_starts_open():
    ledger = {}
    arch.reconcile_outcomes(ledger, [_seg("fix-tests", ["FAILED test_a"])], "abc123", "2026-06-26")
    entry = ledger["fix-tests::FAILED test_a"]
    assert entry["status"] == "open"
    assert entry["attempts"] == 0
    assert entry["first_seen"] == "2026-06-26"
    assert entry["last_commit"] is None


def test_fix_attempt_records_commit_and_increments_attempts():
    ledger = {}
    # Session 1: sees the error and edits a file -> one attempt, still open.
    arch.reconcile_outcomes(
        ledger, [_seg("fix-tests", ["FAILED test_a"], ["app/a.py"])], "abc123", "2026-06-26"
    )
    entry = ledger["fix-tests::FAILED test_a"]
    assert entry["attempts"] == 1
    assert entry["last_commit"] == "abc123"
    assert entry["status"] == "open"


def test_disappearance_after_attempt_marks_fixed():
    ledger = {}
    arch.reconcile_outcomes(
        ledger, [_seg("fix-tests", ["FAILED test_a"], ["app/a.py"])], "abc123", "2026-06-26"
    )
    # Session 2: fix-tests runs again, this error is gone from the fresh read.
    arch.reconcile_outcomes(
        ledger, [_seg("fix-tests", ["FAILED test_b"], ["app/b.py"])], "def456", "2026-06-27"
    )
    assert ledger["fix-tests::FAILED test_a"]["status"] == "fixed"
    assert ledger["fix-tests::FAILED test_a"]["fixed_on"] == "2026-06-27"
    assert ledger["fix-tests::FAILED test_b"]["status"] == "open"


def test_disappearance_without_attempt_stays_open():
    """An error that vanishes with no fix attempt is not credited as fixed."""
    ledger = {}
    arch.reconcile_outcomes(ledger, [_seg("fix-tests", ["FAILED test_a"])], "abc123", "2026-06-26")
    arch.reconcile_outcomes(ledger, [_seg("fix-tests", ["FAILED test_b"])], "def456", "2026-06-27")
    assert ledger["fix-tests::FAILED test_a"]["status"] == "open"


def test_reappearance_after_fixed_marks_recurring():
    ledger = {}
    arch.reconcile_outcomes(
        ledger, [_seg("fix-tests", ["FAILED test_a"], ["app/a.py"])], "abc123", "2026-06-26"
    )
    arch.reconcile_outcomes(ledger, [_seg("fix-tests", ["FAILED test_b"])], "def456", "2026-06-27")
    assert ledger["fix-tests::FAILED test_a"]["status"] == "fixed"
    # Session 3: the "fixed" error comes back.
    arch.reconcile_outcomes(ledger, [_seg("fix-tests", ["FAILED test_a"])], "ghi789", "2026-06-28")
    assert ledger["fix-tests::FAILED test_a"]["status"] == "recurring"


def test_non_fixer_segments_are_ignored():
    ledger = {}
    arch.reconcile_outcomes(ledger, [_seg("refactor", ["FAILED test_a"], ["app/a.py"])], "x", "d")
    assert ledger == {}


def test_signatures_keyed_per_skill():
    ledger = {}
    arch.reconcile_outcomes(
        ledger,
        [_seg("fix-tests", ["E999"]), _seg("fix-lint", ["E999"])],
        "abc",
        "2026-06-26",
    )
    assert "fix-tests::E999" in ledger
    assert "fix-lint::E999" in ledger


def test_outcome_counts_rollup_and_success_rate():
    ledger = {
        "fix-tests::a": {"skill": "fix-tests", "status": "fixed"},
        "fix-tests::b": {"skill": "fix-tests", "status": "fixed"},
        "fix-tests::c": {"skill": "fix-tests", "status": "recurring"},
        "fix-tests::d": {"skill": "fix-tests", "status": "open"},
        "fix-lint::e": {"skill": "fix-lint", "status": "fixed"},
    }
    counts = arch.outcome_counts(ledger)
    assert counts["fix-tests"] == {"fixed": 2, "recurring": 1, "open": 1, "success_rate": 0.5}
    assert counts["fix-lint"] == {"fixed": 1, "recurring": 0, "open": 0, "success_rate": 1.0}


def test_outcome_counts_empty_ledger():
    assert arch.outcome_counts({}) == {}


def test_update_outcome_ledger_persists_and_returns_counts(tmp_path):
    counts, recurring = arch.update_outcome_ledger(
        str(tmp_path),
        [_seg("fix-tests", ["FAILED test_a"], ["app/a.py"])],
        "abc123",
    )
    assert counts["fix-tests"]["open"] == 1
    assert recurring == {}  # nothing recurring on a first sighting
    assert (tmp_path / "error-ledger.json").exists()


def test_recurring_signatures_lists_recurring_only_newest_first():
    ledger = {
        "fix-tests::a": {
            "skill": "fix-tests",
            "signature": "a",
            "status": "recurring",
            "last_seen": "2026-06-26",
        },
        "fix-tests::b": {
            "skill": "fix-tests",
            "signature": "b",
            "status": "recurring",
            "last_seen": "2026-06-28",
        },
        "fix-tests::c": {
            "skill": "fix-tests",
            "signature": "c",
            "status": "fixed",
            "last_seen": "2026-06-29",
        },
        "fix-lint::d": {
            "skill": "fix-lint",
            "signature": "d",
            "status": "recurring",
            "last_seen": "2026-06-27",
        },
    }
    result = arch.recurring_signatures(ledger)
    # Only recurring entries, newest last_seen first; fixed entry "c" excluded.
    assert result["fix-tests"] == ["b", "a"]
    assert result["fix-lint"] == ["d"]


def test_recurring_signatures_caps_per_skill():
    ledger = {
        f"fix-tests::sig{i}": {
            "skill": "fix-tests",
            "signature": f"sig{i}",
            "status": "recurring",
            "last_seen": f"2026-06-{i:02d}",
        }
        for i in range(1, 16)
    }
    result = arch.recurring_signatures(ledger, cap=10)
    assert len(result["fix-tests"]) == 10
    # Newest (highest day) first.
    assert result["fix-tests"][0] == "sig15"


def test_recurring_signatures_empty_when_none():
    ledger = {"fix-tests::a": {"skill": "fix-tests", "signature": "a", "status": "open"}}
    assert arch.recurring_signatures(ledger) == {}


def test_update_profile_injects_fix_outcomes(tmp_path):
    import json

    seg = {
        "skill": "fix-tests",
        "total_reads": 1,
        "total_writes": 1,
        "total_bash": 0,
        "max_consecutive_reads": 1,
        "max_consecutive_bash": 0,
        "files_read": [],
        "files_edited": ["app/a.py"],
        "error_snippets": ["FAILED test_a"],
        "checked_known_fixes": False,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 150,
    }
    outcomes = {"fix-tests": {"fixed": 1, "recurring": 0, "open": 1, "success_rate": 0.5}}
    arch.update_profile(str(tmp_path), [seg], outcomes=outcomes)

    profile = json.loads((tmp_path / "skills-profile.json").read_text())
    assert profile["fix-tests"]["fix_outcomes"] == outcomes["fix-tests"]


def test_update_profile_without_outcomes_omits_field(tmp_path):
    import json

    seg = {
        "skill": "fix-tests",
        "total_reads": 1,
        "total_writes": 0,
        "total_bash": 0,
        "max_consecutive_reads": 1,
        "max_consecutive_bash": 0,
        "files_read": [],
        "files_edited": [],
        "error_snippets": [],
        "checked_known_fixes": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }
    arch.update_profile(str(tmp_path), [seg])  # no outcomes
    profile = json.loads((tmp_path / "skills-profile.json").read_text())
    assert "fix_outcomes" not in profile["fix-tests"]


def _profile_seg(skill="fix-tests"):
    return {
        "skill": skill,
        "total_reads": 1,
        "total_writes": 1,
        "total_bash": 0,
        "max_consecutive_reads": 1,
        "max_consecutive_bash": 0,
        "files_read": [],
        "files_edited": ["app/a.py"],
        "error_snippets": ["FAILED test_a"],
        "checked_known_fixes": False,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 150,
    }


def test_update_profile_injects_recurring_errors(tmp_path):
    import json

    outcomes = {"fix-tests": {"fixed": 1, "recurring": 1, "open": 1, "success_rate": 0.33}}
    recurring = {"fix-tests": ["FAILED test_a"]}
    arch.update_profile(str(tmp_path), [_profile_seg()], outcomes=outcomes, recurring=recurring)

    profile = json.loads((tmp_path / "skills-profile.json").read_text())
    assert profile["fix-tests"]["recurring_errors"] == ["FAILED test_a"]


def test_update_profile_without_recurring_omits_field(tmp_path):
    import json

    outcomes = {"fix-tests": {"fixed": 2, "recurring": 0, "open": 0, "success_rate": 1.0}}
    arch.update_profile(str(tmp_path), [_profile_seg()], outcomes=outcomes)  # no recurring

    profile = json.loads((tmp_path / "skills-profile.json").read_text())
    assert "recurring_errors" not in profile["fix-tests"]
