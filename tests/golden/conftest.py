"""Shared fixtures for tests/golden/.

A golden test calls complete_json() for real — a real Anthropic API call
(ANTHROPIC_API_KEY, costs money) — but still writes to a real Postgres
database (agent_runs, and possibly llm.validation_failed): the same
TEST_DATABASE_URL-vs-DATABASE_URL safety requirement as tests/integration/
applies here too (tests/_db_safety.py, docs/decisions.md).

Reuses tests/_db_safety.py's safety check directly rather than importing
tests/integration/conftest.py as a module (pytest's own conftest import
mechanism and a plain Python import of another directory's conftest.py can
resolve to two different module identities under this project's import
mode — safer to share the one proven, already-imported-this-way module,
`tests._db_safety`, than to import a sibling conftest.py directly).

Unlike tests/integration/conftest.py, this does NOT auto-create or migrate
TEST_DATABASE_URL's database — golden tests are invoked manually
(`make golden`), by which point `make test` or `make migrate` has already
run at least once in any real workflow. If the database doesn't exist yet,
the fixture below fails with a plain connection error naming the problem,
which is an acceptable failure mode for a manually-run, money-costing test
category — not worth duplicating scripts/migrate.py's bootstrap here too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests._db_safety import UnsafeTestDatabaseError, resolve_test_database_url

try:
    _TEST_DATABASE_URL = resolve_test_database_url()
except UnsafeTestDatabaseError as exc:
    pytest.exit(str(exc), returncode=1)


@pytest.fixture(scope="session")
def database_url() -> str:
    return _TEST_DATABASE_URL


@pytest.fixture
async def conn(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute("TRUNCATE agent_runs, events RESTART IDENTITY CASCADE")
        await connection.close()
