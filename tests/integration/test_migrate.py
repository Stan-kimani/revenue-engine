"""Integration tests for scripts/migrate.py against a real Postgres instance.

scripts/migrate.py is not part of the src/ package, so it is loaded directly by
file path rather than imported as a module.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest

pytestmark = pytest.mark.integration

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "migrate.py"


def _load_migrate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrate = _load_migrate_module()


@pytest.fixture
async def clean_schema_migrations():
    database_url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("DROP TABLE IF EXISTS schema_migrations")
        yield
    finally:
        await conn.execute("DROP TABLE IF EXISTS schema_migrations")
        await conn.close()


async def test_empty_migrations_dir_creates_schema_migrations_table(
    tmp_path, monkeypatch, clean_schema_migrations
):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)

    exit_code = await migrate.run()
    assert exit_code == 0

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'schema_migrations')"
        )
        assert exists is True

        row_count = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        assert row_count == 0
    finally:
        await conn.close()


async def test_running_twice_is_idempotent(tmp_path, monkeypatch, clean_schema_migrations):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)

    first_exit = await migrate.run()
    second_exit = await migrate.run()

    assert first_exit == 0
    assert second_exit == 0

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        row_count = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        assert row_count == 0
    finally:
        await conn.close()
