#!/usr/bin/env python3
"""Per-project configuration for the shared agent-harness hook scripts.

The hook scripts (`stop.py`, and later the rest of `scripts/hooks/`) are meant to
be **vendored unchanged into every project**. Everything that differs between
projects -- the control-env prefix, the DB credentials/ports/service names, the
frontend layout, the source-tree shape, the state-driven skills to finalize --
lives here, read from a committed `.agent-harness.toml` at the repo root. The
scripts stay shape-agnostic; a new project drops in a manifest instead of forking
the code.

Design contract:
  - **stdlib only** (`tomllib`, 3.11+). Hooks run before the venv is active.
  - **Never raises.** A missing/unparseable manifest, or an interpreter without
    `tomllib`, falls back to `Config()` defaults -- a minimal but valid harness
    (lint + script-tests, no DB tier, no frontend). A config typo must never
    break the Stop hook.
  - **Neutral defaults.** Defaults describe a generic Python project, not
    carameli; carameli's specifics come from its own `.agent-harness.toml`.

Pure and unit-tested in `scripts/hooks/tests/test_harness_config.py`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

MANIFEST_NAME = ".agent-harness.toml"


@dataclass(frozen=True)
class DbConfig:
    """The DB-backed test tier (Tier 2b). `enabled=False` skips the tier entirely."""

    enabled: bool = False
    services: tuple[str, ...] = ("db", "redis")  # what "reachable"/"up" means
    db_service: str = "db"
    db_port: int = 5432
    redis_service: str = "redis"
    redis_port: int = 6379
    user: str = ""
    password: str = ""
    name: str = ""
    url_scheme: str = "postgresql+asyncpg"
    # Env var names the DB URL is exposed under (carameli needs two).
    url_env: tuple[str, ...] = ("DATABASE_URL",)
    redis_env: str = "REDIS_URL"
    # Extra env for host pytest: name -> default (an already-set os.environ wins).
    test_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FrontendConfig:
    """The frontend (vitest/tsc) tier. `enabled=False` skips all frontend checks."""

    enabled: bool = False
    dir: str = "frontend"
    src: str = "frontend/src/"  # prefix that gates the vitest tier
    skin: str = "frontend/src/skins"  # subtree whose change triggers a typecheck
    test_cmd: tuple[str, ...] = ("run", "test:run")
    typecheck_cmd: tuple[str, ...] = ("run", "typecheck")


@dataclass(frozen=True)
class Config:
    """Shape of the project the harness scripts operate on."""

    # Prefix for harness control env vars, e.g. "CARAMELI" ->
    # CARAMELI_SKIP_STOP_VERIFY / CARAMELI_STOP_TESTS_AUTOSTART / ...
    env_prefix: str = "AGENT_HARNESS"
    app_dir: str = "app/"
    tests_dir: str = "tests/"
    unit_tests: str = "tests/unit"
    # State-driven skills finalized on every stop: (skill, schema) pairs.
    finalize_targets: tuple[tuple[str, str], ...] = ()
    db: DbConfig = field(default_factory=DbConfig)
    frontend: FrontendConfig = field(default_factory=FrontendConfig)

    def env(self, suffix: str) -> str:
        """The prefixed control-env name, e.g. env("SKIP_STOP_VERIFY")."""
        return f"{self.env_prefix}_{suffix}"


def _as_str_tuple(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return fallback


def _db_from(raw: dict[str, Any], default: DbConfig) -> DbConfig:
    test_env = raw.get("test_env")
    return replace(
        default,
        enabled=bool(raw.get("enabled", default.enabled)),
        services=_as_str_tuple(raw.get("services"), default.services),
        db_service=str(raw.get("db_service", default.db_service)),
        db_port=int(raw.get("db_port", default.db_port)),
        redis_service=str(raw.get("redis_service", default.redis_service)),
        redis_port=int(raw.get("redis_port", default.redis_port)),
        user=str(raw.get("user", default.user)),
        password=str(raw.get("password", default.password)),
        name=str(raw.get("name", default.name)),
        url_scheme=str(raw.get("url_scheme", default.url_scheme)),
        url_env=_as_str_tuple(raw.get("url_env"), default.url_env),
        redis_env=str(raw.get("redis_env", default.redis_env)),
        test_env=(
            {str(k): str(v) for k, v in test_env.items()}
            if isinstance(test_env, dict)
            else dict(default.test_env)
        ),
    )


def _frontend_from(raw: dict[str, Any], default: FrontendConfig) -> FrontendConfig:
    return replace(
        default,
        enabled=bool(raw.get("enabled", default.enabled)),
        dir=str(raw.get("dir", default.dir)),
        src=str(raw.get("src", default.src)),
        skin=str(raw.get("skin", default.skin)),
        test_cmd=_as_str_tuple(raw.get("test_cmd"), default.test_cmd),
        typecheck_cmd=_as_str_tuple(raw.get("typecheck_cmd"), default.typecheck_cmd),
    )


def _finalize_from(raw: Any, default: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """[[skill, schema], ...] from TOML -> a tuple of pairs; malformed rows dropped."""
    if not isinstance(raw, list):
        return default
    pairs: list[tuple[str, str]] = []
    for row in raw:
        if isinstance(row, list) and len(row) == 2 and all(isinstance(v, str) for v in row):
            pairs.append((row[0], row[1]))
    return tuple(pairs)


def from_dict(data: dict[str, Any]) -> Config:
    """Build a Config from an already-parsed manifest dict. Pure; never raises."""
    default = Config()
    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    paths = data.get("paths", {}) if isinstance(data.get("paths"), dict) else {}
    stop = data.get("stop", {}) if isinstance(data.get("stop"), dict) else {}
    db_raw = data.get("db", {}) if isinstance(data.get("db"), dict) else {}
    fe_raw = data.get("frontend", {}) if isinstance(data.get("frontend"), dict) else {}
    return Config(
        env_prefix=str(project.get("env_prefix", default.env_prefix)),
        app_dir=str(paths.get("app", default.app_dir)),
        tests_dir=str(paths.get("tests", default.tests_dir)),
        unit_tests=str(paths.get("unit_tests", default.unit_tests)),
        finalize_targets=_finalize_from(stop.get("finalize_targets"), default.finalize_targets),
        db=_db_from(db_raw, default.db),
        frontend=_frontend_from(fe_raw, default.frontend),
    )


def load(root: Path) -> Config:
    """Load `<root>/.agent-harness.toml`, or return defaults when absent/unreadable.

    Any failure -- no file, no `tomllib`, a parse error -- degrades to `Config()`
    so the harness stays a valid (if minimal) lint+script-test gate.
    """
    manifest = root / MANIFEST_NAME
    if not manifest.exists():
        return Config()
    try:
        import tomllib  # stdlib 3.11+; guarded for older shims
    except ModuleNotFoundError:
        return Config()
    with contextlib.suppress(OSError, ValueError), manifest.open("rb") as fh:
        return from_dict(tomllib.load(fh))
    return Config()
