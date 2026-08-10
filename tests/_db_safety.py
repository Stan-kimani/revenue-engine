"""Pure, DB-free logic enforcing that integration tests never run against
DATABASE_URL (a developer's or CI's working database) — only against a
distinct TEST_DATABASE_URL.

A prior version of this suite dropped schema_migrations from whichever
database DATABASE_URL pointed at, because a scripts/migrate.py test fixture
assumed the tracking table was disposable. It was, in the environment that
fixture was written and verified against — a database created and torn down
for that single verification session — but "verified against a disposable
Postgres instance" and "safe to run against yours" are different claims, and
they were never distinguished. See docs/decisions.md.

Not a test file itself (doesn't match pytest's test_*.py collection
pattern) — imported by tests/integration/conftest.py (enforcement) and
tested directly by tests/unit/test_db_safety.py.
"""

from __future__ import annotations

import os


class UnsafeTestDatabaseError(Exception):
    """Raised when TEST_DATABASE_URL is unset or equals DATABASE_URL."""


def resolve_test_database_url() -> str:
    """Returns TEST_DATABASE_URL, or raises with a clear, actionable message.

    Refuses on BOTH conditions — unset, or identical to DATABASE_URL — not
    just the second: a missing TEST_DATABASE_URL is exactly as unsafe as a
    duplicated one, since either way integration tests would fall back to
    DATABASE_URL.
    """
    database_url = os.environ.get("DATABASE_URL")
    test_database_url = os.environ.get("TEST_DATABASE_URL")

    if not test_database_url:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is not set. Integration tests must run against a "
            "database distinct from DATABASE_URL (your dev/CI database) — set "
            "TEST_DATABASE_URL in .env (see .env.example and docs/runbook.md). "
            "Refusing to run rather than risk corrupting your working database."
        )
    if database_url and test_database_url == database_url:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is identical to DATABASE_URL. They must point to "
            "different databases — see .env.example and docs/runbook.md. "
            "Refusing to run rather than risk corrupting your working database."
        )
    return test_database_url
