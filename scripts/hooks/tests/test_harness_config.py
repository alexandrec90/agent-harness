"""Unit tests for the per-project harness config loader.

Covers the pure `from_dict` mapping and its tolerance of malformed/partial
manifests, plus a contract test that the committed `.agent-harness.toml` is
internally coherent (so a manifest edit that would silently change harness
behaviour fails here instead of at runtime).

**This file is vendored into every consuming project**, so nothing here may assert
a value that is specific to one project. It previously pinned carameli's literal
credentials, paths, and skill list, which made the whole vendored suite red in any
repo whose manifest differed — the exact opposite of the portability the harness
exists for. Assert *invariants* of a manifest, never one project's contents.
"""

from pathlib import Path

from conftest import REPO_ROOT, load_module

cfg = load_module("scripts/hooks/harness_config.py")


def test_defaults_are_a_minimal_neutral_harness():
    c = cfg.Config()
    assert c.env_prefix == "AGENT_HARNESS"
    assert c.finalize_targets == ()
    assert c.db.enabled is False
    assert c.frontend.enabled is False
    # Neutral defaults describe a generic Python project, not carameli.
    assert c.app_dir == "app/" and c.unit_tests == "tests/unit"


def test_env_prefixes_control_vars():
    c = cfg.Config(env_prefix="CARAMELI")
    assert c.env("SKIP_STOP_VERIFY") == "CARAMELI_SKIP_STOP_VERIFY"
    assert c.env("STOP_TESTS_AUTOSTART") == "CARAMELI_STOP_TESTS_AUTOSTART"


def test_from_dict_empty_is_defaults():
    assert cfg.from_dict({}) == cfg.Config()


def test_from_dict_maps_full_manifest():
    c = cfg.from_dict(
        {
            "project": {"env_prefix": "FOO"},
            "paths": {"app": "src/", "tests": "t/", "unit_tests": "t/unit"},
            "stop": {"finalize_targets": [["audit", "audit"], ["make-tests", "modules"]]},
            "db": {
                "enabled": True,
                "services": ["db", "cache"],
                "db_service": "db",
                "db_port": 6000,
                "redis_service": "cache",
                "redis_port": 7000,
                "user": "u",
                "password": "p",
                "name": "n",
                "url_scheme": "postgresql+asyncpg",
                "url_env": ["DATABASE_URL", "DIRECT_DATABASE_URL"],
                "redis_env": "CACHE_URL",
                "test_env": {"API_KEY_SECRET": "k"},
            },
            "frontend": {"enabled": True, "dir": "web", "src": "web/src/"},
        }
    )
    assert c.env_prefix == "FOO"
    assert c.app_dir == "src/" and c.unit_tests == "t/unit"
    assert c.finalize_targets == (("audit", "audit"), ("make-tests", "modules"))
    assert c.db.enabled is True
    assert c.db.services == ("db", "cache")
    assert c.db.db_port == 6000 and c.db.redis_service == "cache"
    assert c.db.url_env == ("DATABASE_URL", "DIRECT_DATABASE_URL")
    assert c.db.redis_env == "CACHE_URL"
    assert c.db.test_env == {"API_KEY_SECRET": "k"}
    assert c.frontend.enabled is True and c.frontend.dir == "web"


def test_from_dict_tolerates_wrong_types():
    # Non-dict sections and wrong-typed fields fall back to defaults, never raise.
    c = cfg.from_dict(
        {
            "project": "nope",
            "db": {"services": "not-a-list"},
            "frontend": [1, 2],
        }
    )
    assert c.env_prefix == "AGENT_HARNESS"
    assert c.db.services == ("db", "redis")  # bad `services` -> default
    assert c.frontend.enabled is False


def test_finalize_targets_drops_malformed_rows():
    c = cfg.from_dict(
        {"stop": {"finalize_targets": [["ok", "schema"], ["too", "many", "cols"], "bad", [1, 2]]}}
    )
    assert c.finalize_targets == (("ok", "schema"),)


def test_load_missing_manifest_returns_defaults(tmp_path: Path):
    assert cfg.load(tmp_path) == cfg.Config()


def test_load_parses_real_toml(tmp_path: Path):
    (tmp_path / cfg.MANIFEST_NAME).write_text(
        '[project]\nenv_prefix = "ZED"\n[db]\nenabled = true\nservices = ["db"]\n'
    )
    c = cfg.load(tmp_path)
    assert c.env_prefix == "ZED"
    assert c.db.enabled is True and c.db.services == ("db",)


def test_load_malformed_toml_returns_defaults(tmp_path: Path):
    (tmp_path / cfg.MANIFEST_NAME).write_text("this is = = not valid toml [[[")
    assert cfg.load(tmp_path) == cfg.Config()


# --- contract: the committed manifest reproduces carameli's constants ---------


def test_repo_manifest_loads_and_is_coherent():
    """This repo's own `.agent-harness.toml` must load and hold together.

    Portable replacement for a test that pinned carameli's literal values. It still
    catches the failures that matter — a manifest that no longer parses, or a
    half-filled block — without asserting anything project-specific.
    """
    c = cfg.load(REPO_ROOT)

    # A blank prefix would make every control var `_SKIP_STOP_VERIFY`, colliding
    # across projects on one machine.
    assert c.env_prefix and c.env_prefix.isupper()
    assert c.env("SKIP_STOP_VERIFY").startswith(f"{c.env_prefix}_")

    # Directory-ish fields must be usable as path prefixes: `stop.py` decides which
    # checks to run with `path.startswith(CFG.app_dir)`, so a missing trailing slash
    # makes `apps/` match `app/` and silently widens the test selection.
    assert c.app_dir.endswith("/")
    assert c.tests_dir.endswith("/")
    assert c.unit_tests

    # Each finalize target is a (skill, schema) pair of non-empty strings; a
    # malformed row is dropped by the loader and would otherwise vanish silently.
    for skill, schema in c.finalize_targets:
        assert skill and schema

    if c.db.enabled:
        # A half-filled DB block yields a URL like `postgresql://:@host:5432/`,
        # which fails at connect time with a message that points nowhere.
        assert c.db.user and c.db.name and c.db.password
        assert c.db.url_scheme and c.db.url_env
        assert c.db.db_service in c.db.services

    if c.frontend.enabled:
        assert c.frontend.dir and c.frontend.test_cmd
        # `src` gates the vitest tier by prefix match, same trailing-slash logic.
        assert c.frontend.src.endswith("/")
