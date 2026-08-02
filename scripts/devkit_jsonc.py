#!/usr/bin/env python3
"""Loader for VS Code's JSON-with-comments dialect (`.code-workspace`, `tasks.json`).

VS Code accepts `//` and `/* */` comments and trailing commas; `json.loads` does not.
Every one of those files in this workspace is hand-edited and commented, so reading
them with the stdlib parser fails on contact — and the failure is *quiet* where it
matters: `sweep.parse_workspace` catches `JSONDecodeError` and reports no checkouts,
which reads as "nothing to ship" rather than "I could not read the file".

That is why this exists as a module rather than a regex at each call site: the
workspace file is the project registry (`sweep.py`) *and* the home of the shared task
block, so the same text is parsed by the sweep, the generator, and the drift check.

The strippers are **offset-preserving** — comments and trailing commas are blanked to
spaces, never deleted — so line/column numbers in a `JSONDecodeError` still point at
the right place in the original file.

This module is **pure and stdlib-only**: importable by a hook or a test with no
virtualenv, and it touches nothing outside the string it is handed.

Unit-tested in `tests/test_devkit_jsonc.py`.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["blank_comments", "drop_trailing_commas", "loads"]


def blank_comments(text: str) -> str:
    """Replace `//` and `/* */` comments with spaces, leaving newlines intact.

    String literals are skipped, so a `//` inside a JSON string value (a URL, a
    Windows path) survives. Backslash escapes are honoured, so `"a\\""` does not
    end the string early.
    """
    out = list(text)
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2  # skip the escaped char, whatever it is
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                j = i
                while j < n and text[j] not in "\r\n":
                    out[j] = " "
                    j += 1
                i = j
                continue
            if text[i + 1] == "*":
                j = i
                while j < n and not (text[j] == "*" and j + 1 < n and text[j + 1] == "/"):
                    # Keep newlines so line numbers do not shift.
                    if text[j] not in "\r\n":
                        out[j] = " "
                    j += 1
                for k in range(j, min(j + 2, n)):  # blank the closing */
                    out[k] = " "
                i = j + 2
                continue
        i += 1
    return "".join(out)


def drop_trailing_commas(text: str) -> str:
    """Blank any comma whose next non-whitespace character closes an object or array.

    Run this *after* `blank_comments`: a comma followed only by a comment and then
    `}` is trailing too, and blanking the comment first is what makes it visible.
    """
    out = list(text)
    i, n = 0, len(text)
    in_string = False
    pending: int | None = None  # index of the last comma seen outside a string
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            pending = None
        elif ch == ",":
            pending = i
        elif ch in "}]":
            if pending is not None:
                out[pending] = " "
            pending = None
        elif not ch.isspace():
            pending = None
        i += 1
    return "".join(out)


def loads(text: str) -> Any:
    """`json.loads`, tolerating VS Code's comment and trailing-comma dialect.

    Raises `json.JSONDecodeError` for text that is malformed for reasons the dialect
    does not excuse — callers that must not crash should catch it, as `sweep` does.
    """
    return json.loads(drop_trailing_commas(blank_comments(text)))
