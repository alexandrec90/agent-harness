#!/usr/bin/env python3
"""Digest a Claude Code session transcript (.jsonl) into a compact waste report.

Prints one line per tool call plus signal sections (failures, repeats, vacuous
results) so the /retro skill can judge a session without ever loading the raw
transcript into model context. Output is ASCII-only and capped head+tail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Input fields that best identify what a tool call did, in priority order.
KEY_FIELDS = ("command", "file_path", "pattern", "skill", "description", "prompt", "query", "url")
KEY_MAX = 110
# Result sizes at or below this, for these tools, are flagged as vacuous:
# a check that inspected nothing still "succeeds" with an empty result.
VACUOUS_TOOLS = ("Bash", "PowerShell", "Grep", "Glob")
VACUOUS_MAX_CHARS = 5


@dataclass
class Call:
    index: int
    tool: str
    key: str
    result_chars: int = -1  # -1 = no result seen
    is_error: bool = False
    error_snippet: str = ""


@dataclass
class Digest:
    path: Path
    calls: list[Call] = field(default_factory=list)


def _input_key(tool_input: dict) -> str:
    for f in KEY_FIELDS:
        v = tool_input.get(f)
        if isinstance(v, str) and v.strip():
            key = " ".join(v.split())
            break
    else:
        key = json.dumps(tool_input, ensure_ascii=True, default=str)
    return key[:KEY_MAX]


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def parse_transcript(path: Path) -> Digest:
    digest = Digest(path=path)
    by_id: dict[str, Call] = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    call = Call(
                        index=len(digest.calls) + 1,
                        tool=str(block.get("name", "?")),
                        key=_input_key(block.get("input") or {}),
                    )
                    digest.calls.append(call)
                    if isinstance(block.get("id"), str):
                        by_id[block["id"]] = call
                elif block.get("type") == "tool_result":
                    caller = by_id.get(block.get("tool_use_id", ""))
                    if caller is None:
                        continue
                    text = _result_text(block.get("content"))
                    caller.result_chars = len(text)
                    if block.get("is_error"):
                        caller.is_error = True
                        caller.error_snippet = " ".join(text.split())[:120]
    return digest


def _size(chars: int) -> str:
    if chars < 0:
        return "?"
    if chars < 1024:
        return f"{chars}B"
    return f"{chars / 1024:.1f}KB"


def _call_line(c: Call) -> str:
    status = "ERR" if c.is_error else "ok"
    return f"{c.index:04d} {c.tool:<12} {status:<4} {_size(c.result_chars):>7}  {c.key}"


def render(digest: Digest, head: int, tail: int) -> str:
    calls = digest.calls
    failed = [c for c in calls if c.is_error]
    out = [
        "# source: .claude/skills/retro/extract.py",
        f"# transcript: {digest.path} ({len(calls)} tool calls, {len(failed)} failures)",
        f"== CALLS ({min(len(calls), head + tail)} of {len(calls)}) ==",
    ]
    if len(calls) <= head + tail:
        out.extend(_call_line(c) for c in calls)
    else:
        out.extend(_call_line(c) for c in calls[:head])
        out.append(f"... {len(calls) - head - tail} calls omitted ...")
        out.extend(_call_line(c) for c in calls[-tail:])

    out.append("== FAILED ==")
    out.extend(f"{_call_line(c)} | {c.error_snippet}" for c in failed)
    if not failed:
        out.append("(none)")

    out.append("== REPEATED (same tool+input >=2) ==")
    counts: dict[tuple[str, str], int] = {}
    for c in calls:
        counts[(c.tool, c.key)] = counts.get((c.tool, c.key), 0) + 1
    repeats = sorted(((n, t, k) for (t, k), n in counts.items() if n >= 2), key=lambda r: -r[0])[
        :15
    ]
    out.extend(f"{n}x {t}  {k}" for n, t, k in repeats)
    if not repeats:
        out.append("(none)")

    out.append("== VACUOUS (check-style call returned ~nothing) ==")
    vacuous = [
        c
        for c in calls
        if c.tool in VACUOUS_TOOLS and 0 <= c.result_chars <= VACUOUS_MAX_CHARS and not c.is_error
    ]
    out.extend(_call_line(c) for c in vacuous)
    if not vacuous:
        out.append("(none)")
    return "\n".join(out)


def default_transcript_dir() -> Path:
    sanitized = re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd()))
    return Path.home() / ".claude" / "projects" / sanitized


def list_transcripts(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "transcript", nargs="?", help="transcript path or filename prefix (default: newest)"
    )
    parser.add_argument("--dir", type=Path, default=None, help="transcript directory override")
    parser.add_argument(
        "--list", action="store_true", help="list transcripts newest-first and exit"
    )
    parser.add_argument("--head", type=int, default=50)
    parser.add_argument("--tail", type=int, default=50)
    args = parser.parse_args(argv)

    directory = args.dir or default_transcript_dir()
    if not directory.is_dir():
        print(f"error: transcript directory not found: {directory}", file=sys.stderr)
        return 1
    transcripts = list_transcripts(directory)
    if args.list:
        for p in transcripts[:20]:
            print(f"{p.stat().st_mtime:.0f}  {_size(p.stat().st_size):>9}  {p.name}")
        return 0
    if args.transcript:
        candidate = Path(args.transcript)
        if not candidate.is_file():
            matches = [p for p in transcripts if p.name.startswith(args.transcript)]
            if len(matches) != 1:
                n = len(matches)
                print(f"error: {n} transcripts match '{args.transcript}'", file=sys.stderr)
                return 1
            candidate = matches[0]
    elif transcripts:
        candidate = transcripts[0]
    else:
        print(f"error: no .jsonl transcripts in {directory}", file=sys.stderr)
        return 1

    print(render(parse_transcript(candidate), head=args.head, tail=args.tail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
