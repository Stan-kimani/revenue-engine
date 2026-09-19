"""Integration tests for import-time email verification (M1.4a) against a real
Postgres instance (TEST_DATABASE_URL — tests/_db_safety.py).

The verification provider is ALWAYS a stub: credits are finite and charged per
address. tests/conftest.py additionally makes the real provider's verify()
raise, so a test that forgets to inject a stub fails loudly rather than
spending money.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest

from revenue_engine.db import repositories as repo
from revenue_engine.db.models import EmailStatus
from revenue_engine.integrations.email_verification import VerificationResult

pytestmark = pytest.mark.integration

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "import_leads.py"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
PACK = "b2b-service-firms"


def _load_import_leads_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_leads_for_verification", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # RowResult is a dataclass — see test_import_leads.py
    spec.loader.exec_module(module)
    return module


import_leads = _load_import_leads_module()


@pytest.fixture
async def conn(database_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute(
            "TRUNCATE companies, contacts, leads, messages, events, jobs, agent_runs "
            "RESTART IDENTITY CASCADE"
        )
        await connection.close()


@dataclass
class StubVerifier:
    result: VerificationResult
    calls: list[str] = field(default_factory=list)

    async def verify(self, email: str) -> VerificationResult:
        self.calls.append(email)
        return self.result


def _install(monkeypatch: pytest.MonkeyPatch, verifier: StubVerifier | None) -> None:
    monkeypatch.setattr(import_leads, "default_provider", lambda *a, **k: verifier)


async def _import(csv_name: str = "import_leads_valid.csv") -> int:
    return await import_leads.run(FIXTURES_DIR / csv_name, industry_pack=PACK)


async def test_import_verifies_each_contact_once_and_stores_the_verdict(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    verifier = StubVerifier(
        VerificationResult(
            status=EmailStatus.CATCH_ALL,
            attributes={"email_verification_score": 61, "email_is_free_provider": False},
            raw_status="valid",
        )
    )
    _install(monkeypatch, verifier)

    assert await _import() == 0

    assert verifier.calls, "the importer never called the verifier"
    for email in verifier.calls:
        contact = await repo.get_contact_by_email(conn, email)
        assert contact is not None
        assert contact.email_status == EmailStatus.CATCH_ALL
        # Vendor metadata is stored as ordinary provenance-carrying attributes.
        assert contact.attributes["email_verification_score"]["value"] == 61
        assert contact.attributes["email_verification_score"]["source"] == (
            "provider:emaillistverify"
        )
        assert contact.attributes["email_is_free_provider"]["value"] is False


@pytest.mark.protected
async def test_a_contact_with_a_verdict_is_never_re_verified(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    """Credits are charged per address: a stored verdict is never paid for
    twice, and a re-import must not downgrade it either."""
    first = StubVerifier(VerificationResult(status=EmailStatus.VALID, raw_status="valid"))
    _install(monkeypatch, first)
    await _import()
    verified_emails = list(first.calls)
    assert verified_emails

    second = StubVerifier(VerificationResult(status=EmailStatus.INVALID, raw_status="invalid"))
    _install(monkeypatch, second)
    await _import()

    assert second.calls == []  # not one credit spent on the re-import
    for email in verified_emails:
        contact = await repo.get_contact_by_email(conn, email)
        assert contact is not None
        assert contact.email_status == EmailStatus.VALID  # untouched


@pytest.mark.protected
async def test_a_verification_failure_leaves_the_contact_unverified_never_valid(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    """API down or out of credits: unverified, which is the never-send tier."""
    verifier = StubVerifier(
        VerificationResult(status=EmailStatus.UNVERIFIED, raw_status="http_error")
    )
    _install(monkeypatch, verifier)

    assert await _import() == 0

    for email in verifier.calls:
        contact = await repo.get_contact_by_email(conn, email)
        assert contact is not None
        assert contact.email_status == EmailStatus.UNVERIFIED


async def test_import_without_an_api_key_still_imports_but_sends_nothing(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    _install(monkeypatch, None)  # EMAILLISTVERIFY_API_KEY unset

    assert await _import() == 0

    contacts = await conn.fetch("SELECT email_status FROM contacts")
    assert contacts
    assert {row["email_status"] for row in contacts} == {"unverified"}


@pytest.mark.protected
async def test_email_status_is_write_once_and_never_regresses(conn: asyncpg.Connection):
    """upsert_contact preserves a verdict — the clobbering path that made a
    paid verification look like a broken verifier (docs/decisions.md)."""
    await repo.upsert_contact(conn, email="pat@acme.example")
    await repo.set_email_status(conn, email="pat@acme.example", status=EmailStatus.VALID)

    # Any later upsert (re-import, re-enrichment) passes the UNVERIFIED default.
    await repo.upsert_contact(conn, email="pat@acme.example", title="COO")

    contact = await repo.get_contact_by_email(conn, "pat@acme.example")
    assert contact is not None
    assert contact.email_status == EmailStatus.VALID
    assert contact.title == "COO"  # other columns still update

    with pytest.raises(ValueError, match="never be set back to 'unverified'"):
        await repo.set_email_status(conn, email="pat@acme.example", status=EmailStatus.UNVERIFIED)
