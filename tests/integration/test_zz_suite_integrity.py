"""Suite-wide regression coverage for the defect logged in docs/decisions.md:
a scripts/migrate.py test fixture used to DROP TABLE schema_migrations against
whatever DATABASE_URL pointed at, which corrupted a developer's actual dev
database (all 22 domain/infra tables survived; only the tracking table was
lost, then a second migrate.py run failed with "relation companies already
exists" trying to re-apply 0001_init.sql onto the now-untracked schema).

Named `test_zz_...` so it collects and runs after every other file in
tests/integration/ under pytest's default (deterministic, alphabetical)
collection order — this specific test's whole point is to check the state
schema_migrations is left in *after* everything else in the suite has run.
"""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = pytest.mark.integration


@pytest.mark.protected
async def test_schema_migrations_survives_the_full_suite(database_url: str):
    conn = await asyncpg.connect(database_url)
    try:
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'schema_migrations')"
        )
        assert exists is True, (
            "schema_migrations does not exist after the test suite ran — "
            "something in this suite dropped or never created it against "
            "TEST_DATABASE_URL."
        )

        applied = await conn.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE filename = '0001_init.sql'"
        )
        assert applied == 1, (
            "schema_migrations no longer records 0001_init.sql as applied after "
            "the test suite ran (found count != 1) — either the row was removed, "
            "or migrations were never applied to TEST_DATABASE_URL before running "
            "the suite (see README.md / docs/runbook.md: `make migrate` targets "
            "DATABASE_URL, not TEST_DATABASE_URL)."
        )
    finally:
        await conn.close()
