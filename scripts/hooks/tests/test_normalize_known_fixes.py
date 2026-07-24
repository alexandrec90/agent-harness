"""Unit tests for the known-fixes table normalizer."""

from datetime import date

from conftest import load_module

hook = load_module("scripts/hooks/normalize-known-fixes.py")

TODAY = date(2026, 6, 19)

HEADER = "| Error pattern (substring) | Root cause | Fix | Hits | Last used | Added |"
SEP = "|---|---|---|---|---|---|"


def _table(*rows: str) -> str:
    return "\n".join(["# Known fixes", "", HEADER, SEP, *rows, "", "trailing"])


def test_split_markdown_cells_basic():
    assert hook.split_markdown_cells("| a | b | c |") == ["a", "b", "c"]


def test_split_markdown_cells_honors_escaped_pipe():
    assert hook.split_markdown_cells(r"| a \| b | c |") == [r"a \| b", "c"]


def test_no_header_returns_unchanged():
    raw = "# Just prose\n\nno table here\n"
    assert hook.normalize_table(raw, 90, TODAY) == (raw, False, 0, 0)


def test_prunes_zero_hit_stale_row():
    raw = _table(
        "| foo | bar | do x | 0 | 2025-01-01 | 2025-01-01 |",
        "| keep | bar | do y | 3 | 2026-06-01 | 2025-01-01 |",
        "| recent | bar | do z | 0 | -- | 2026-06-01 |",
    )
    new_content, changed, pruned, rows = hook.normalize_table(raw, 90, TODAY)
    assert changed is True
    assert pruned == 1
    assert rows == 2
    assert "foo" not in new_content
    assert "keep" in new_content and "recent" in new_content


def test_zero_hit_recent_row_is_kept():
    raw = _table("| recent | bar | do z | 0 | -- | 2026-06-01 |")
    _, _, pruned, rows = hook.normalize_table(raw, 90, TODAY)
    assert pruned == 0
    assert rows == 1


def test_canonicalizes_separator_and_reports_changed():
    raw = "\n".join(
        [
            HEADER,
            "| :--- | --- | --- | --- | --- | --- |",
            "| a | b | c | 5 | 2026-06-01 | 2026-06-01 |",
        ]
    )
    new_content, changed, _, _ = hook.normalize_table(raw, 90, TODAY)
    assert changed is True
    assert SEP in new_content


def test_update_file_dry_run_does_not_write(tmp_path):
    path = tmp_path / "known-fixes.md"
    raw = _table("| foo | bar | do x | 0 | 2025-01-01 | 2025-01-01 |")
    path.write_text(raw, encoding="utf-8")
    changed, pruned = hook.update_file(path, 90, TODAY, dry_run=True)
    assert changed is True and pruned == 1
    assert path.read_text(encoding="utf-8") == raw  # untouched on disk


def test_update_file_writes_when_not_dry_run(tmp_path):
    path = tmp_path / "known-fixes.md"
    path.write_text(_table("| foo | bar | do x | 0 | 2025-01-01 | 2025-01-01 |"), encoding="utf-8")
    hook.update_file(path, 90, TODAY, dry_run=False)
    assert "foo" not in path.read_text(encoding="utf-8")
