"""Integration tests for scripts/import_leads.py against a real Postgres
instance (TEST_DATABASE_URL, never DATABASE_URL — tests/_db_safety.py).

scripts/import_leads.py is not part of the src/ package, so it is loaded
directly by file path, same as scripts/migrate.py in
tests/integration/test_migrate.py and scripts/run_worker.py in
tests/integration/test_dispatch.py.

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest

pytestmark = pytest.mark.integration

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "import_leads.py"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
PACK = "b2b-service-firms"


def _load_import_leads_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_leads_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # import_leads.py defines a @dataclass (RowResult) — dataclasses resolves
    # its own module via sys.modules[cls.__module__] while decorating the
    # class, which requires the module to already be registered in
    # sys.modules *before* exec_module runs (unlike scripts/run_worker.py or
    # scripts/migrate.py, neither of which defines a dataclass, so this gap
    # in the file-path-loading convention used across this test suite never
    # surfaced until now — found by actually running this test, not by
    # reading the code). Without this line: "AttributeError: 'NoneType'
    # object has no attribute '__dict__'" from inside dataclasses._is_type.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


import_leads = _load_import_leads_module()

# database_url fixture comes from tests/integration/conftest.py.


@pytest.fixture
async def conn(database_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute(
            "TRUNCATE companies, contacts, leads, events, jobs, agent_runs RESTART IDENTITY CASCADE"
        )
        await connection.close()


async def _event_count(conn: asyncpg.Connection, event_type: str) -> int:
    row = await conn.fetchrow("SELECT count(*) AS n FROM events WHERE type = $1", event_type)
    assert row is not None
    return int(row["n"])


@pytest.mark.protected
async def test_reimporting_same_csv_is_a_noop(conn: asyncpg.Connection):
    csv_path = FIXTURES_DIR / "import_leads_valid.csv"

    exit_code_1 = await import_leads.run(csv_path, industry_pack=PACK)
    assert exit_code_1 == 0

    lead_count_1 = await conn.fetchval("SELECT count(*) FROM leads")
    company_count_1 = await conn.fetchval("SELECT count(*) FROM companies")
    contact_count_1 = await conn.fetchval("SELECT count(*) FROM contacts")
    captured_count_1 = await _event_count(conn, "lead.captured")

    exit_code_2 = await import_leads.run(csv_path, industry_pack=PACK)
    assert exit_code_2 == 0

    assert await conn.fetchval("SELECT count(*) FROM leads") == lead_count_1
    assert await conn.fetchval("SELECT count(*) FROM companies") == company_count_1
    assert await conn.fetchval("SELECT count(*) FROM contacts") == contact_count_1
    assert await _event_count(conn, "lead.captured") == captured_count_1  # no duplicate events


@pytest.mark.protected
async def test_row_for_company_with_active_lead_emits_deferred_and_import_continues(
    conn: asyncpg.Connection,
):
    csv_path = FIXTURES_DIR / "import_leads_deferred.csv"

    exit_code = await import_leads.run(csv_path, industry_pack=PACK)

    assert exit_code == 0  # a deferred row is a normal outcome, not a failure

    leads = await conn.fetch(
        """
        SELECT l.status FROM leads l
        JOIN companies c ON c.id = l.company_id
        WHERE c.domain = 'shared-co-fixture.example'
        ORDER BY l.created_at
        """
    )
    assert [row["status"] for row in leads] == ["new", "deferred"]
    assert await _event_count(conn, "lead.deferred") == 1
    assert await _event_count(conn, "lead.captured") == 1

    deferred_payload = await conn.fetchrow(
        "SELECT payload FROM events WHERE type = 'lead.deferred'"
    )
    assert deferred_payload is not None
    payload = json.loads(deferred_payload["payload"])
    assert payload["reason"] == "company_single_thread"
    assert payload["blocked_by_lead_id"] is not None


async def test_row_missing_domain_or_email_is_rejected_others_still_import(
    conn: asyncpg.Connection,
):
    csv_path = FIXTURES_DIR / "import_leads_missing_fields.csv"

    exit_code = await import_leads.run(csv_path, industry_pack=PACK)

    assert exit_code == 0  # rejected rows are reported, not a script failure
    assert await conn.fetchval("SELECT count(*) FROM leads") == 1
    good_contact = await conn.fetchrow(
        "SELECT * FROM contacts WHERE email = 'sam@goodco-fixture.example'"
    )
    assert good_contact is not None
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM contacts WHERE email IN ('alex@nodomain-fixture.example', '')"
        )
        == 0
    )


@pytest.mark.protected
async def test_concurrent_imports_of_same_csv_produce_one_lead_and_one_lead_captured_event(
    conn: asyncpg.Connection,
):
    """Correction 1 (docs/decisions.md, M1.1): the guard against a duplicate
    row from re-importing the SAME CSV concurrently must be structural (DB
    constraints), not a check-then-act race in Python. Two real, independent
    connections (import_leads.run() opens its own each time) race to import
    the identical single-row CSV; exactly one must win."""
    csv_path = FIXTURES_DIR / "import_leads_concurrent.csv"

    exit_codes = await asyncio.gather(
        import_leads.run(csv_path, industry_pack=PACK),
        import_leads.run(csv_path, industry_pack=PACK),
    )
    assert all(code == 0 for code in exit_codes)

    leads = await conn.fetch(
        """
        SELECT l.id FROM leads l
        JOIN contacts ct ON ct.id = l.contact_id
        WHERE ct.email = 'riley@race-co-fixture.example'
        """
    )
    assert len(leads) == 1
    assert await _event_count(conn, "lead.captured") == 1
