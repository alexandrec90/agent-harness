#!/usr/bin/env python3
"""A deliberately tiny template renderer for the project templates.

Scope is set by one constraint: **stdlib only**. devkit's CI installs `ruff` and
`pytest` and nothing else, and the generator must run against a bare system Python
before any project venv exists. That rules out Jinja, so this implements the two
features the templates actually need and stops there:

    {{ name }}                 substitution — unknown names raise, so a typo in a
                               template fails the render instead of silently
                               emitting an empty string into a compose file.

    {{#postgres}}              feature blocks — kept when the flag is truthy.
    ...                        The opening, closing, and `{{^...}}` inverse lines
    {{/postgres}}              must each be alone on their line; the control lines
                               themselves are removed from the output. Blocks nest.

    {{^postgres}}...{{/postgres}}   the inverse: kept when the flag is falsy.

Why line-oriented blocks rather than inline ones: every file these templates
generate is line-structured (YAML, TOML, JSONC, Markdown, .env), and a conditional
that spans part of a line would let a render produce a syntactically broken compose
file. Forcing control lines to stand alone means a disabled block removes whole
lines and leaves valid syntax behind.

Pure and unit-tested in `tests/test_devkit_render.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A control line: optional indent, one tag, nothing else. The indent is captured but
# discarded — control lines never reach the output, so their indentation is cosmetic
# and lets templates keep blocks visually aligned with the YAML they wrap.
_CONTROL = re.compile(r"^[ \t]*\{\{([#^/])([A-Za-z_][A-Za-z0-9_]*)\}\}[ \t]*$")
_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Any block tag at all, wherever it sits. Used only to reject inline ones: a tag
# sharing a line with other content matches neither _CONTROL nor _VAR, so without
# this check it would be copied verbatim into the output and ship a literal
# "{{#alembic}}" inside a generated JSON file.
_INLINE_TAG = re.compile(r"\{\{[#^/]([A-Za-z_][A-Za-z0-9_]*)\}\}")


class TemplateError(ValueError):
    """A template is malformed, or references a name the context does not define."""


@dataclass(frozen=True)
class _Frame:
    name: str
    inverse: bool
    line_no: int
    emitting: bool


def render(text: str, context: dict[str, object]) -> str:
    """Render `text` against `context`. Raises `TemplateError` on any unknown name.

    Flags and substitution values share one namespace: `{{#postgres}}` tests the
    same key `{{ postgres }}` would interpolate. That keeps the generator's feature
    flags directly usable as template values (e.g. writing a boolean into a
    generated `.devkit.toml`) without a second dict to keep in sync.
    """
    out: list[str] = []
    stack: list[_Frame] = []
    # Trailing-newline handling: splitlines() drops the final one, so remember it and
    # restore it at the end rather than emitting a spurious blank line.
    trailing_newline = text.endswith("\n")

    for line_no, line in enumerate(text.splitlines(), start=1):
        control = _CONTROL.match(line)
        if control:
            sigil, name = control.group(1), control.group(2)
            if sigil == "/":
                if not stack:
                    raise TemplateError(f"line {line_no}: {{{{/{name}}}}} with no open block")
                frame = stack.pop()
                if frame.name != name:
                    raise TemplateError(
                        f"line {line_no}: {{{{/{name}}}}} closes {{{{#{frame.name}}}}} "
                        f"opened on line {frame.line_no}"
                    )
                continue
            if name not in context:
                raise TemplateError(
                    f"line {line_no}: block {{{{{sigil}{name}}}}} references undefined "
                    f"flag {name!r}; known: {', '.join(sorted(context)) or '(none)'}"
                )
            inverse = sigil == "^"
            active = (not bool(context[name])) if inverse else bool(context[name])
            # An inner block inside a suppressed outer one stays suppressed, so that a
            # disabled feature cannot leak its nested sub-feature into the output.
            emitting = active and (stack[-1].emitting if stack else True)
            stack.append(_Frame(name=name, inverse=inverse, line_no=line_no, emitting=emitting))
            continue

        inline = _INLINE_TAG.search(line)
        if inline:
            raise TemplateError(
                f"line {line_no}: block tag {inline.group(0)} shares a line with other "
                f"content; block tags must stand alone on their own line so a disabled "
                f"block removes whole lines and leaves valid syntax behind"
            )

        if stack and not stack[-1].emitting:
            continue
        out.append(_substitute(line, context, line_no))

    if stack:
        frame = stack[-1]
        sigil = "^" if frame.inverse else "#"
        raise TemplateError(
            f"unclosed block {{{{{sigil}{frame.name}}}}} opened on line {frame.line_no}"
        )

    body = "\n".join(out)
    return body + "\n" if trailing_newline and body else body


def _substitute(line: str, context: dict[str, object], line_no: int) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            raise TemplateError(
                f"line {line_no}: undefined template variable {name!r}; "
                f"known: {', '.join(sorted(context)) or '(none)'}"
            )
        value = context[name]
        # Booleans render lowercase so a flag can be written straight into generated
        # TOML/JSON (`enabled = {{ postgres }}`) without a parallel string context.
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _VAR.sub(replace, line)


def used_names(text: str) -> set[str]:
    """Every flag and variable name a template references. Used to lint the tree."""
    names = {m.group(2) for m in (_CONTROL.match(ln) for ln in text.splitlines()) if m}
    return names | set(_VAR.findall(text))
