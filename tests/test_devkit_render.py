"""Tests for the template renderer."""

import pytest
from support import devkit_render

TemplateError = devkit_render.TemplateError
render = devkit_render.render
used_names = devkit_render.used_names


def test_substitutes_variables():
    assert (
        render("project: {{ name }}\n", {"name": "sports_betting"}) == "project: sports_betting\n"
    )


def test_substitution_tolerates_missing_inner_whitespace():
    assert render("{{name}}", {"name": "x"}) == "x"


def test_booleans_render_lowercase_for_toml_and_json():
    # The whole point: `enabled = {{ postgres }}` must produce valid TOML, not
    # Python's `True`, so a flag can be written straight into generated config.
    out = render("enabled = {{ postgres }}\n", {"postgres": True})
    assert out == "enabled = true\n"
    assert render("enabled = {{ postgres }}\n", {"postgres": False}) == "enabled = false\n"


def test_unknown_variable_raises_rather_than_emitting_empty():
    # A silent empty substitution would put `ports: - ":5432"` into a compose file
    # and fail much later, at `docker compose up`, with no pointer to the template.
    with pytest.raises(TemplateError, match="undefined template variable 'nmae'"):
        render("{{ nmae }}", {"name": "x"})


def test_kept_block_drops_only_its_control_lines():
    template = "a\n{{#postgres}}\ndb: yes\n{{/postgres}}\nz\n"
    assert render(template, {"postgres": True}) == "a\ndb: yes\nz\n"


def test_suppressed_block_removes_whole_lines():
    template = "a\n{{#postgres}}\ndb: yes\n{{/postgres}}\nz\n"
    assert render(template, {"postgres": False}) == "a\nz\n"


def test_inverse_block_is_the_complement():
    template = "{{^postgres}}\nno database\n{{/postgres}}\n"
    assert render(template, {"postgres": False}) == "no database\n"
    assert render(template, {"postgres": True}) == ""


def test_control_lines_may_be_indented_to_match_surrounding_yaml():
    template = "services:\n  {{#redis}}\n  redis:\n    image: redis\n  {{/redis}}\n"
    assert render(template, {"redis": True}) == "services:\n  redis:\n    image: redis\n"


def test_nested_blocks_render_independently():
    template = "{{#docker}}\nup\n{{#postgres}}\ndb\n{{/postgres}}\ndown\n{{/docker}}\n"
    ctx = {"docker": True, "postgres": True}
    assert render(template, ctx) == "up\ndb\ndown\n"
    assert render(template, {**ctx, "postgres": False}) == "up\ndown\n"


def test_disabled_outer_block_suppresses_an_enabled_inner_one():
    # Without the inherited-emitting rule, `postgres` being on would leak a `db`
    # line into a project generated with `--no-docker`.
    template = "{{#docker}}\nup\n{{#postgres}}\ndb\n{{/postgres}}\n{{/docker}}\n"
    assert render(template, {"docker": False, "postgres": True}) == ""


def test_variables_inside_a_suppressed_block_are_not_resolved():
    # A block that is off must not require its variables to exist — that is what
    # lets one template serve both `--with-frontend` and `--no-frontend` renders.
    template = "{{#frontend}}\nport: {{ frontend_port }}\n{{/frontend}}\n"
    assert render(template, {"frontend": False}) == ""


def test_unknown_flag_in_a_block_raises():
    with pytest.raises(TemplateError, match="undefined flag 'postgress'"):
        render("{{#postgress}}\nx\n{{/postgress}}\n", {"postgres": True})


def test_unclosed_block_raises_with_the_opening_line():
    with pytest.raises(TemplateError, match=r"unclosed block .* line 1"):
        render("{{#postgres}}\nx\n", {"postgres": True})


def test_mismatched_close_names_the_block_it_actually_closes():
    with pytest.raises(TemplateError, match=r"closes \{\{#postgres\}\} opened on line 1"):
        render("{{#postgres}}\n{{/redis}}\n", {"postgres": True, "redis": True})


def test_stray_close_raises():
    with pytest.raises(TemplateError, match="with no open block"):
        render("{{/postgres}}\n", {"postgres": True})


def test_render_preserves_absence_of_a_trailing_newline():
    assert render("a", {}) == "a"
    assert render("a\n", {}) == "a\n"


def test_fully_suppressed_template_renders_empty_not_a_blank_line():
    assert render("{{#x}}\na\n{{/x}}\n", {"x": False}) == ""


def test_inline_block_tag_is_rejected_rather_than_copied_verbatim():
    # Regression: an inline tag matches neither the control-line nor the variable
    # pattern, so before this guard it was emitted literally — shipping the string
    # "{{#alembic}}" into a generated tasks.json, which VS Code then failed to parse.
    with pytest.raises(TemplateError, match="must stand alone on their own line"):
        render("    }{{#alembic}},\n", {"alembic": True})


def test_inline_tag_is_rejected_even_inside_a_suppressed_block():
    # The check runs before the emitting test on purpose: a malformed template
    # should fail on every render, not only on the flag combination that reaches it.
    with pytest.raises(TemplateError, match="must stand alone"):
        render("{{#a}}\nx {{#b}} y\n{{/b}}\n{{/a}}\n", {"a": False, "b": True})


def test_used_names_reports_flags_and_variables():
    template = "{{#postgres}}\n{{ name }}\n{{/postgres}}\n{{^frontend}}\n{{/frontend}}\n"
    assert used_names(template) == {"postgres", "name", "frontend"}
