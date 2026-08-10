"""Integration tests for scripts/migrate.py against a real Postgres instance.

scripts/migrate.py is not part of the src/ package, so it is loaded directly by
file path rather than imported as a module.

Every test here that needs to observe schema-from-scratch behaviour (creating
schema_migrations for the first time) uses the disposable_database_url
fixture (tests/integration/conftest.py) — a throwaway database, never
TEST_DATABASE_URL itself. TEST_DATABASE_URL's own schema_migrations table is
never dropped by this file — see docs/decisions.md for why that used to
happen and what it corrupted.
"""

from __future__ import annotations

import importlib.util
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


async def test_empty_migrations_dir_creates_schema_migrations_table(
    tmp_path, monkeypatch, disposable_database_url
):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setenv("DATABASE_URL", disposable_database_url)

    exit_code = await migrate.run()
    assert exit_code == 0

    conn = await asyncpg.connect(disposable_database_url)
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


async def test_running_twice_is_idempotent(tmp_path, monkeypatch, disposable_database_url):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setenv("DATABASE_URL", disposable_database_url)

    first_exit = await migrate.run()
    second_exit = await migrate.run()

    assert first_exit == 0
    assert second_exit == 0

    conn = await asyncpg.connect(disposable_database_url)
    try:
        row_count = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        assert row_count == 0
    finally:
        await conn.close()


@pytest.mark.protected
async def test_migration_0001_applies_cleanly_and_is_idempotent_on_empty_database(
    disposable_database_url: str, monkeypatch
):
    """Applies the REAL migrations/0001_init.sql (MIGRATIONS_DIR unpatched)
    against a genuinely empty, disposable database.
    """
    monkeypatch.setenv("DATABASE_URL", disposable_database_url)

    first_exit = await migrate.run()
    assert first_exit == 0

    conn = await asyncpg.connect(disposable_database_url)
    try:
        tables = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    finally:
        await conn.close()
    expected_tables = {
        "companies",
        "contacts",
        "leads",
        "deals",
        "events",
        "jobs",
        "schema_migrations",
    }
    assert expected_tables.issubset(tables)

    second_exit = await migrate.run()
    assert second_exit == 0


async def test_no_transaction_migration_applies_and_is_recorded(
    tmp_path, monkeypatch, disposable_database_url
):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setenv("DATABASE_URL", disposable_database_url)

    migration_file = tmp_path / "0001_no_transaction.sql"
    migration_file.write_text(
        "-- migrate: no-transaction\n"
        "CREATE TABLE no_transaction_smoke_test (id serial primary key);\n"
    )

    exit_code = await migrate.run()
    assert exit_code == 0

    conn = await asyncpg.connect(disposable_database_url)
    try:
        recorded = await conn.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE filename = $1",
            "0001_no_transaction.sql",
        )
        assert recorded == 1

        table_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'no_transaction_smoke_test')"
        )
        assert table_exists is True
    finally:
        await conn.close()
