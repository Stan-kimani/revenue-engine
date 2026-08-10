"""Shared fixtures for tests/integration/.

Every integration test connects to TEST_DATABASE_URL, never DATABASE_URL.
The check runs at module import time — i.e. as soon as pytest starts
collecting anything under tests/integration/, before any fixture or test
body executes — and aborts the whole session via pytest.exit() if it fails,
rather than skipping individual tests. See tests/_db_safety.py and
docs/decisions.md for why this exists: a fixture here used to DROP TABLE
schema_migrations against whatever DATABASE_URL pointed at, which corrupted
a developer's dev database.

TEST_DATABASE_URL's target database is created and migrated automatically
here if needed, so a fresh clone works without a separate manual setup step
or changes to docker-compose.yml/Makefile — `make migrate` still targets
DATABASE_URL only, exactly as before.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest

from tests._db_safety import UnsafeTestDatabaseError, resolve_test_database_url


def _load_migrate_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("migrate_script_for_conftest", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _ensure_test_database_ready(test_database_url: str) -> None:
    """Creates the TEST_DATABASE_URL database if it doesn't exist yet (using
    DATABASE_URL's server as the admin connection — the revenue_engine role
    already has CREATEDB, confirmed in docs/decisions.md), then applies
    migrations to it via the same scripts/migrate.py every other entry point
    uses, rather than reimplementing migration logic here.
    """
    _, _, db_name = test_database_url.rpartition("/")

    try:
        probe = await asyncpg.connect(test_database_url)
        await probe.close()
    except asyncpg.InvalidCatalogNameError:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise UnsafeTestDatabaseError(
                f"TEST_DATABASE_URL's database ({db_name!r}) does not exist, and "
                "DATABASE_URL is unset so it can't be created automatically. "
                "See .env.example and docs/runbook.md."
            ) from None
        admin_conn = await asyncpg.connect(database_url)
        try:
            # Identifier, not a value — cannot be parameterised. db_name comes
            # from TEST_DATABASE_URL, which is developer/CI configuration, not
            # user input.
            await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await admin_conn.close()

    migrate = _load_migrate_module()
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_database_url
    try:
        exit_code = await migrate.run()
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url

    if exit_code != 0:
        raise UnsafeTestDatabaseError(
            "Failed to apply migrations to TEST_DATABASE_URL — see output above."
        )


try:
    _TEST_DATABASE_URL = resolve_test_database_url()
    asyncio.run(_ensure_test_database_ready(_TEST_DATABASE_URL))
except UnsafeTestDatabaseError as exc:
    pytest.exit(str(exc), returncode=1)


@pytest.fixture(scope="session")
def database_url() -> str:
    return _TEST_DATABASE_URL


@pytest.fixture
async def disposable_database_url(database_url: str) -> AsyncIterator[str]:
    """A fresh, empty, uniquely-named database on the same Postgres server as
    TEST_DATABASE_URL — not TEST_DATABASE_URL itself. For tests that need to
    observe schema-from-scratch behaviour (e.g. scripts/migrate.py creating
    schema_migrations for the first time) without touching the tracking
    table TEST_DATABASE_URL's own other tests, and anyone inspecting it
    directly, rely on.
    """
    base_url, _, _ = database_url.rpartition("/")
    db_name = f"test_disposable_{uuid.uuid4().hex[:12]}"
    disposable_url = f"{base_url}/{db_name}"

    admin_conn = await asyncpg.connect(database_url)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin_conn.close()

    try:
        yield disposable_url
    finally:
        admin_conn = await asyncpg.connect(database_url)
        try:
            await admin_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await admin_conn.close()
