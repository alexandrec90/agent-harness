#!/usr/bin/env python3
"""Stop-hook helper: normalizes `.claude/skills/*/known-fixes.md` tables.

Rewrites each known-fixes table to a canonical 6-column shape and prunes rows
that have zero hits and are older than the stale window. Off by default; the
Stop hook only calls this when its `*_NORMALIZE_KNOWN_FIXES_ON_STOP` opt-in is
set (the prefix is per-project, from .agent-harness.toml -- CARAMELI here). This
script itself is project-agnostic: it operates on the shared `.claude/skills`
convention, so it is vendored across projects unchanged.

Pure helpers (`split_markdown_cells`, `normalize_table`) are unit-tested via
`scripts/hooks/tests/test_normalize_known_fixes.py`.
"""

import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
DEFAULT_STALE_DAYS = 90

HEADER_RE = re.compile(
    r"^\|\s*Error pattern \(substring\)\s*\|\s*Root cause\s*\|\s*Fix\s*\|"
    r"\s*Hits\s*\|\s*Last used\s*\|\s*Added\s*\|\s*$"
)
SEPARATOR_RE = re.compile(r"^\|\s*:?-{3,}:?\s*\|")
CANONICAL_HEADER = "| Error pattern (substring) | Root cause | Fix | Hits | Last used | Added |"
CANONICAL_SEPARATOR = "|---|---|---|---|---|---|"


def split_markdown_cells(row: str) -> list[str]:
    """Split a markdown table row into trimmed cells, honoring `\\|` escapes."""
    trimmed = row.strip()
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]

    cells: list[str] = []
    current: list[str] = []
    escape = False
    for char in trimmed:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\":
            current.append(char)
            escape = True
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    cells.append("".join(current).strip())
    return cells


def _int_or_none(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _date_or_none(value: str) -> date | None:
    text = value.strip()
    if not text or text in ("--", "-"):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_table(raw: str, stale_days: int, today: date) -> tuple[str, bool, int, int]:
    """Normalize the known-fixes table in `raw`.

    Returns (new_text, changed, pruned, rows). Returns the input unchanged when
    no recognizable table header is present.
    """
    if not raw:
        return raw, False, 0, 0

    lines = re.split(r"\r?\n", raw)

    header_index = next((i for i, ln in enumerate(lines) if HEADER_RE.match(ln)), -1)
    if header_index < 0:
        return raw, False, 0, 0

    separator_index = header_index + 1
    if separator_index >= len(lines):
        return raw, False, 0, 0

    row_start = separator_index + 1
    row_end = row_start
    while row_end < len(lines) and lines[row_end].lstrip().startswith("|"):
        row_end += 1

    normalized_rows: list[str] = []
    pruned = 0

    for r in range(row_start, row_end):
        line = lines[r].strip()
        if not line:
            continue
        if SEPARATOR_RE.match(line):
            continue

        cells = [c.strip() for c in split_markdown_cells(line)]
        while len(cells) < 6:
            cells.append("")
        if len(cells) > 6:
            # Collapse extra columns (an unescaped pipe split the Fix cell) back
            # into a single Fix cell, preserving the trailing metadata columns.
            fix_cell = " | ".join(cells[2 : len(cells) - 2]).strip()
            cells = [
                cells[0],
                cells[1],
                fix_cell,
                cells[-3].strip(),
                cells[-2].strip(),
                cells[-1].strip(),
            ]

        hits = _int_or_none(cells[3])
        added = _date_or_none(cells[5])
        if hits == 0 and added is not None and (today - added).days > stale_days:
            pruned += 1
            continue

        normalized_rows.append("| " + " | ".join(cells) + " |")

    prefix = lines[:header_index]
    suffix = lines[row_end:]
    new_lines = [*prefix, CANONICAL_HEADER, CANONICAL_SEPARATOR, *normalized_rows, *suffix]

    new_content = "\n".join(new_lines)
    if raw.endswith("\r\n"):
        new_content = new_content.replace("\n", "\r\n")

    changed = new_content != raw
    return new_content, changed, pruned, len(normalized_rows)


def update_file(path: Path, stale_days: int, today: date, dry_run: bool) -> tuple[bool, int]:
    """Normalize a single known-fixes file in place. Returns (changed, pruned)."""
    raw = path.read_text(encoding="utf-8-sig")
    new_content, changed, pruned, _ = normalize_table(raw, stale_days, today)
    if changed and not dry_run:
        path.write_text(new_content, encoding="utf-8")
    return changed, pruned


def main() -> int:
    stale_days = DEFAULT_STALE_DAYS
    dry_run = "--dry-run" in sys.argv

    skills_root = REPO_ROOT / ".claude/skills"
    if not skills_root.is_dir():
        print("normalize-known-fixes: skills directory not found; skipping.")
        return 0

    files = sorted(p / "known-fixes.md" for p in skills_root.iterdir() if p.is_dir())
    files = [p for p in files if p.exists()]
    if not files:
        print("normalize-known-fixes: no known-fixes files found; skipping.")
        return 0

    today = datetime.now(UTC).date()
    scanned = len(files)
    changed_count = 0
    pruned_total = 0
    for path in files:
        changed, pruned = update_file(path, stale_days, today, dry_run)
        changed_count += 1 if changed else 0
        pruned_total += pruned

    prefix = "[DRY RUN] " if dry_run else ""
    print(
        f"{prefix}normalize-known-fixes: scanned={scanned} "
        f"changed={changed_count} pruned={pruned_total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
