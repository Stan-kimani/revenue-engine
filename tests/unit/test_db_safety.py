"""Unit tests for tests/_db_safety.py — pure, no I/O, no database
(build-spec §8.1). This is the actual refusal mechanism
tests/integration/conftest.py calls at collection time, not a proxy for it.
"""

from __future__ import annotations

import pytest

from tests._db_safety import UnsafeTestDatabaseError, resolve_test_database_url


@pytest.mark.protected
def test_refuses_when_test_database_url_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/dev")

    with pytest.raises(UnsafeTestDatabaseError, match="not set"):
        resolve_test_database_url()


@pytest.mark.protected
def test_refuses_when_test_database_url_equals_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/dev")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@host/dev")

    with pytest.raises(UnsafeTestDatabaseError, match="identical"):
        resolve_test_database_url()


def test_accepts_a_distinct_test_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/dev")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@host/dev_test")

    assert resolve_test_database_url() == "postgresql://u:p@host/dev_test"


def test_accepts_test_database_url_when_database_url_itself_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DATABASE_URL being unset isn't the unsafe condition — TEST_DATABASE_URL
    being unset, or matching DATABASE_URL, is."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@host/dev_test")

    assert resolve_test_database_url() == "postgresql://u:p@host/dev_test"
