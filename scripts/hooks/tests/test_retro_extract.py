"""Tests for .claude/skills/retro/extract.py — the /retro transcript digester.

Covers the pure parse/render pipeline against synthetic transcript JSONL. The
transcript-directory discovery (`default_transcript_dir`) depends on cwd/home
and is exercised via `--dir` overrides instead.
"""

import json

from conftest import load_module

mod = load_module(".claude/skills/retro/extract.py")


def _tool_use(tool_id, name, tool_input):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]
            },
        }
    )


def _tool_result(tool_id, text, is_error=False):
    block = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        block["is_error"] = True
    return json.dumps({"type": "user", "message": {"content": [block]}})


def _write_transcript(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _sample_transcript(tmp_path):
    return _write_transcript(
        tmp_path / "session.jsonl",
        [
            _tool_use("t1", "Bash", {"command": "git status --porcelain"}),
            _tool_result("t1", "M app/main.py"),
            # Vacuous: check-style call, empty result, not an error
            _tool_use("t2", "Bash", {"command": "python scripts/lint-all.py --changed"}),
            _tool_result("t2", ""),
            # Failure with snippet
            _tool_use("t3", "Edit", {"file_path": "app/main.py"}),
            _tool_result("t3", "old_string not found in file", is_error=True),
            # Repeated call (2x)
            _tool_use("t4", "Bash", {"command": "docker compose ps"}),
            _tool_result("t4", "app running"),
            _tool_use("t5", "Bash", {"command": "docker compose ps"}),
            _tool_result("t5", "app running"),
            "not json at all",  # malformed lines are skipped, not fatal
            json.dumps({"type": "summary", "summary": "no message key"}),
        ],
    )


def test_parse_collects_calls_results_and_errors(tmp_path):
    digest = mod.parse_transcript(_sample_transcript(tmp_path))
    assert [c.tool for c in digest.calls] == ["Bash", "Bash", "Edit", "Bash", "Bash"]
    assert digest.calls[0].result_chars == len("M app/main.py")
    assert digest.calls[1].result_chars == 0 and not digest.calls[1].is_error
    assert digest.calls[2].is_error
    assert "old_string not found" in digest.calls[2].error_snippet


def test_render_sections_flag_failed_repeated_and_vacuous(tmp_path):
    digest = mod.parse_transcript(_sample_transcript(tmp_path))
    out = mod.render(digest, head=50, tail=50)
    assert "(5 tool calls, 1 failures)" in out
    failed = out.split("== FAILED ==")[1].split("== REPEATED")[0]
    assert "app/main.py" in failed and "(none)" not in failed
    assert "2x Bash  docker compose ps" in out
    vacuous = out.split("== VACUOUS")[1]
    assert "lint-all.py --changed" in vacuous
    # The successful, non-empty, non-repeated call appears in no signal section
    for section in (failed, vacuous):
        assert "git status" not in section


def test_render_caps_call_list_head_tail(tmp_path):
    lines = []
    for i in range(30):
        lines.append(_tool_use(f"t{i}", "Read", {"file_path": f"app/f{i}.py"}))
        lines.append(_tool_result(f"t{i}", "x" * 10))
    digest = mod.parse_transcript(_write_transcript(tmp_path / "big.jsonl", lines))
    out = mod.render(digest, head=5, tail=5)
    assert "... 20 calls omitted ..." in out
    assert "app/f0.py" in out and "app/f29.py" in out
    assert "app/f15.py" not in out


def test_render_empty_sections_say_none(tmp_path):
    digest = mod.parse_transcript(
        _write_transcript(
            tmp_path / "clean.jsonl",
            [
                _tool_use("t1", "Read", {"file_path": "README.md"}),
                _tool_result("t1", "contents here"),
            ],
        )
    )
    out = mod.render(digest, head=50, tail=50)
    assert out.count("(none)") == 3  # FAILED, REPEATED, VACUOUS all clean


def test_input_key_prefers_known_fields_and_truncates():
    assert mod._input_key({"command": "  ls   -la  "}) == "ls -la"
    assert mod._input_key({"skill": "fix-lint"}) == "fix-lint"
    long = mod._input_key({"command": "x" * 500})
    assert len(long) == mod.KEY_MAX
    # Unknown shape falls back to compact JSON
    assert mod._input_key({"weird": 1}) == '{"weird": 1}'


def test_main_selects_newest_and_supports_prefix_and_list(tmp_path, capsys):
    import os

    old = _write_transcript(
        tmp_path / "aaa-old.jsonl",
        [_tool_use("t1", "Bash", {"command": "old cmd"}), _tool_result("t1", "x")],
    )
    new = _write_transcript(
        tmp_path / "bbb-new.jsonl",
        [_tool_use("t1", "Bash", {"command": "new cmd"}), _tool_result("t1", "x")],
    )
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    assert mod.main(["--dir", str(tmp_path)]) == 0
    assert "new cmd" in capsys.readouterr().out

    assert mod.main(["--dir", str(tmp_path), "aaa"]) == 0
    assert "old cmd" in capsys.readouterr().out

    assert mod.main(["--dir", str(tmp_path), "--list"]) == 0
    listing = capsys.readouterr().out
    assert listing.splitlines()[0].endswith("bbb-new.jsonl")

    assert mod.main(["--dir", str(tmp_path), "zzz"]) == 1  # no prefix match
    assert mod.main(["--dir", str(tmp_path / "missing")]) == 1  # bad dir
