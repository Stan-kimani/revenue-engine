"""Integration tests for scripts/migrate.py against a real Postgres instance.

scripts/migrate.py is not part of the src/ package, so it is loaded directly by
file path rather than imported as a module.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
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
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set (checked .env and the shell environment)")
    return url


@pytest.fixture
async def clean_schema_migrations(database_url: str):
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("DROP TABLE IF EXISTS schema_migrations")
        yield
    finally:
        await conn.execute("DROP TABLE IF EXISTS schema_migrations")
        await conn.close()


async def test_empty_migrations_dir_creates_schema_migrations_table(
    tmp_path, monkeypatch, database_url, clean_schema_migrations
):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)

    exit_code = await migrate.run()
    assert exit_code == 0

    conn = await asyncpg.connect(database_url)
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


async def test_running_twice_is_idempotent(
    tmp_path, monkeypatch, database_url, clean_schema_migrations
):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)

    first_exit = await migrate.run()
    second_exit = await migrate.run()

    assert first_exit == 0
    assert second_exit == 0

    conn = await asyncpg.connect(database_url)
    try:
        row_count = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        assert row_count == 0
    finally:
        await conn.close()


@pytest.mark.protected
async def test_migration_0001_applies_cleanly_and_is_idempotent_on_empty_database(
    database_url: str, monkeypatch
):
    """Applies the REAL migrations/0001_init.sql (MIGRATIONS_DIR unpatched)
    against a genuinely empty, disposable database — not the shared test
    database other tests in this suite reuse, and not a monkeypatched empty
    directory like the tests above. A fresh CREATE DATABASE within the same
    Postgres instance is the isolation boundary.
    """
    base_url, _, _ = database_url.rpartition("/")
    test_db_name = f"test_m0001_{uuid.uuid4().hex[:12]}"
    test_db_url = f"{base_url}/{test_db_name}"

    admin_conn = await asyncpg.connect(database_url)
    try:
        # Identifier, not a value — cannot be parameterised. Safe: machine-generated
        # uuid suffix, never user input.
        await admin_conn.execute(f'CREATE DATABASE "{test_db_name}"')
    finally:
        await admin_conn.close()

    try:
        monkeypatch.setenv("DATABASE_URL", test_db_url)

        first_exit = await migrate.run()
        assert first_exit == 0

        conn = await asyncpg.connect(test_db_url)
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
    finally:
        admin_conn = await asyncpg.connect(database_url)
        try:
            await admin_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                test_db_name,
            )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{test_db_name}"')
        finally:
            await admin_conn.close()


async def test_no_transaction_migration_applies_and_is_recorded(
    tmp_path, monkeypatch, database_url, clean_schema_migrations
):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)

    migration_file = tmp_path / "0001_no_transaction.sql"
    migration_file.write_text(
        "-- migrate: no-transaction\n"
        "CREATE TABLE no_transaction_smoke_test (id serial primary key);\n"
    )

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("DROP TABLE IF EXISTS no_transaction_smoke_test")
    finally:
        await conn.close()

    try:
        exit_code = await migrate.run()
        assert exit_code == 0

        conn = await asyncpg.connect(database_url)
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
    finally:
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute("DROP TABLE IF EXISTS no_transaction_smoke_test")
        finally:
            await conn.close()
