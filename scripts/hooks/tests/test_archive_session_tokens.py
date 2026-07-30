"""Unit tests for per-skill token accounting in archive-session.py."""

from conftest import load_module

arch = load_module("scripts/hooks/archive-session.py")


def _assistant(usage):
    return {"type": "assistant", "message": {"content": [], "usage": usage}}


def test_extract_usage_sums_all_four_fields():
    counts = arch.extract_usage(
        _assistant(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 50,
            }
        )
    )
    assert counts["input_tokens"] == 100
    assert counts["output_tokens"] == 20
    assert counts["cache_creation_input_tokens"] == 5
    assert counts["cache_read_input_tokens"] == 50
    assert counts["total"] == 175


def test_extract_usage_missing_or_malformed_is_zero():
    assert arch.extract_usage({"type": "assistant", "message": {}})["total"] == 0
    assert arch.extract_usage({})["total"] == 0
    # Non-int values are ignored, not summed.
    bad = arch.extract_usage(_assistant({"input_tokens": "lots", "output_tokens": 7}))
    assert bad["input_tokens"] == 0
    assert bad["output_tokens"] == 7
    assert bad["total"] == 7


def test_analyze_segment_accumulates_tokens():
    entries = [
        {
            "type": "user",
            "message": {
                "content": [{"type": "text", "text": "<command-name>fix-tests</command-name>"}]
            },
        },
        _assistant({"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 300}),
        _assistant({"input_tokens": 500, "output_tokens": 100}),
    ]
    seg = arch.analyze_segment(entries, 0, len(entries))
    assert seg["input_tokens"] == 1500
    assert seg["output_tokens"] == 300
    assert seg["cache_read_tokens"] == 300
    assert seg["cache_creation_tokens"] == 0
    assert seg["total_tokens"] == 1500 + 300 + 300


def test_update_profile_rolls_token_totals_and_average(tmp_path):
    seg = {
        "skill": "fix-tests",
        "total_reads": 1,
        "total_writes": 1,
        "total_bash": 0,
        "max_consecutive_reads": 1,
        "max_consecutive_bash": 0,
        "files_read": [],
        "files_edited": [],
        "error_snippets": [],
        "checked_known_fixes": False,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 300,
        "total_tokens": 1500,
    }

    # Two invocations -> totals add, avg_tokens is the mean of the per-run totals.
    arch.update_profile(str(tmp_path), [seg])
    arch.update_profile(str(tmp_path), [seg])

    import json

    profile = json.loads((tmp_path / "skills-profile.json").read_text())
    fix = profile["fix-tests"]
    assert fix["invocations"] == 2
    assert fix["total_tokens_all"] == 3000
    assert fix["total_input_tokens"] == 2000
    assert fix["total_cache_read_tokens"] == 600
    assert fix["avg_tokens"] == 1500
    assert fix["tokens_history"] == [1500, 1500]


def test_update_profile_backfills_tokens_on_legacy_entry(tmp_path):
    """A pre-existing profile without token fields must not crash and starts at 0."""
    import json

    legacy = {"fix-tests": {"invocations": 4, "total_reads_all": 8, "total_writes_all": 4}}
    (tmp_path / "skills-profile.json").write_text(json.dumps(legacy))

    seg = {
        "skill": "fix-tests",
        "total_reads": 2,
        "total_writes": 1,
        "total_bash": 0,
        "max_consecutive_reads": 2,
        "max_consecutive_bash": 0,
        "files_read": [],
        "files_edited": [],
        "error_snippets": [],
        "checked_known_fixes": False,
        "input_tokens": 800,
        "output_tokens": 200,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 1000,
    }
    arch.update_profile(str(tmp_path), [seg])

    fix = json.loads((tmp_path / "skills-profile.json").read_text())["fix-tests"]
    assert fix["invocations"] == 5
    assert fix["total_tokens_all"] == 1000
    assert fix["avg_tokens"] == 200  # 1000 tokens / 5 cumulative invocations
