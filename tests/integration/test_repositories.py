"""Integration tests for db/repositories.py (companies, contacts, leads,
events, jobs — build-spec §10 M0.2) against a real Postgres instance.

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import datetime
import re
import uuid

import asyncpg
import pytest

from revenue_engine.core.errors import DuplicateActiveLeadError, InvalidAttributeEnvelopeError
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import EmailStatus, JobStatus, LeadSource, LeadStatus

pytestmark = pytest.mark.integration

# database_url fixture comes from tests/integration/conftest.py (TEST_DATABASE_URL,
# never DATABASE_URL — see docs/decisions.md).


@pytest.fixture
async def conn(database_url: str):
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        # Truncate rather than drop — migration 0001 owns the schema.
        await connection.execute(
            "TRUNCATE companies, contacts, leads, lead_scores, deals, messages, events, jobs "
            "RESTART IDENTITY CASCADE"
        )
        await connection.close()


def _envelope(value: object, source: str = "provider:test") -> dict:
    return {
        "value": value,
        "confidence": 1.0,
        "evidence": None,
        "source": source,
        "run_id": None,
        "observed_at": "2026-08-03T00:00:00Z",
    }


# ============================================================================
# Companies
# ============================================================================


async def test_upsert_company_creates_then_updates_on_same_domain(conn: asyncpg.Connection):
    first = await repo.upsert_company(conn, name="Acme Ltd", domain="acme.com")
    second = await repo.upsert_company(conn, name="Acme Limited", domain="acme.com")

    assert first.id == second.id
    assert second.name == "Acme Limited"

    count = await conn.fetchval("SELECT count(*) FROM companies")
    assert count == 1


async def test_upsert_company_without_domain_always_inserts(conn: asyncpg.Connection):
    a = await repo.upsert_company(conn, name="No Website Co")
    b = await repo.upsert_company(conn, name="No Website Co")

    assert a.id != b.id


async def test_upsert_company_merges_attributes_across_calls(conn: asyncpg.Connection):
    await repo.upsert_company(
        conn, name="Acme", domain="acme.com", attributes={"industry": _envelope("B2B SaaS")}
    )
    updated = await repo.upsert_company(
        conn,
        name="Acme",
        domain="acme.com",
        attributes={"revenue_est": _envelope(1_000_000)},
    )

    assert "industry" in updated.attributes
    assert "revenue_est" in updated.attributes


async def test_upsert_company_rejects_invalid_attribute_envelope(conn: asyncpg.Connection):
    with pytest.raises(InvalidAttributeEnvelopeError):
        await repo.upsert_company(
            conn, name="Acme", domain="acme.com", attributes={"industry": "B2B SaaS"}
        )


async def test_get_company_by_domain_exact_match_only(conn: asyncpg.Connection):
    await repo.upsert_company(conn, name="Acme", domain="acme.com")

    assert await repo.get_company_by_domain(conn, "acme.com") is not None
    assert await repo.get_company_by_domain(conn, "www.acme.com") is None


# ============================================================================
# Contacts
# ============================================================================


async def test_upsert_contact_creates_then_updates_on_same_email(conn: asyncpg.Connection):
    first = await repo.upsert_contact(conn, email="jane@acme.com", full_name="Jane Doe")
    second = await repo.upsert_contact(conn, email="jane@acme.com", title="COO")

    assert first.id == second.id
    assert second.title == "COO"
    assert second.full_name == "Jane Doe"  # preserved via COALESCE, not wiped


async def test_upsert_contact_email_is_case_insensitive(conn: asyncpg.Connection):
    first = await repo.upsert_contact(conn, email="Jane@Acme.com")
    second = await repo.upsert_contact(conn, email="jane@acme.com")

    assert first.id == second.id


async def test_upsert_contact_records_employment_history_on_company_change(
    conn: asyncpg.Connection,
):
    company_a = await repo.upsert_company(conn, name="Old Co", domain="old.com")
    company_b = await repo.upsert_company(conn, name="New Co", domain="new.com")

    await repo.upsert_contact(conn, email="jane@example.com", company_id=company_a.id)
    updated = await repo.upsert_contact(conn, email="jane@example.com", company_id=company_b.id)

    assert updated.company_id == company_b.id
    history = updated.attributes.get("employment_history")
    assert history is not None
    assert history[0]["company_id"] == str(company_a.id)


async def test_contact_email_status_defaults_to_unverified(conn: asyncpg.Connection):
    contact = await repo.upsert_contact(conn, email="jane@acme.com")
    assert contact.email_status == EmailStatus.UNVERIFIED


# ============================================================================
# Leads
# ============================================================================


async def test_create_lead_defaults_to_new_status(conn: asyncpg.Connection):
    contact = await repo.upsert_contact(conn, email="jane@acme.com")

    result = await repo.create_lead(
        conn, contact_id=contact.id, industry_pack="b2b-service-firms", source=LeadSource.DISCOVERY
    )

    assert result.deferred is False
    assert result.lead.status == LeadStatus.NEW
    assert result.lead.band is None


@pytest.mark.protected
async def test_second_active_lead_same_contact_is_rejected(conn: asyncpg.Connection):
    contact = await repo.upsert_contact(conn, email="jane@acme.com")
    await repo.create_lead(
        conn, contact_id=contact.id, industry_pack="b2b-service-firms", source=LeadSource.DISCOVERY
    )

    with pytest.raises(DuplicateActiveLeadError) as exc_info:
        await repo.create_lead(
            conn,
            contact_id=contact.id,
            industry_pack="b2b-service-firms",
            source=LeadSource.DISCOVERY,
        )
    assert exc_info.value.constraint_name == "one_active_lead_per_contact"


@pytest.mark.protected
async def test_second_active_lead_same_company_returns_deferral_not_exception(
    conn: asyncpg.Connection,
):
    company = await repo.upsert_company(conn, name="Acme", domain="acme.com")
    contact_a = await repo.upsert_contact(conn, email="a@acme.com", company_id=company.id)
    contact_b = await repo.upsert_contact(conn, email="b@acme.com", company_id=company.id)

    first = await repo.create_lead(
        conn,
        contact_id=contact_a.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.DISCOVERY,
    )
    assert first.deferred is False

    second = await repo.create_lead(
        conn,
        contact_id=contact_b.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.DISCOVERY,
    )

    assert second.deferred is True
    assert second.lead.status == LeadStatus.DEFERRED
    assert second.blocked_by_lead_id == first.lead.id


@pytest.mark.protected
async def test_both_single_thread_indexes_have_identical_exclusion_list(
    conn: asyncpg.Connection,
):
    """Reads the live catalog (pg_indexes), not the migration file — the
    catalog is what actually governs collisions, and it's exactly what a
    future migration could let drift apart without anyone noticing. If the
    two lists ever diverge, the deferred-placeholder insert in create_lead()
    can start colliding with one of these indexes again.
    """
    rows = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename = 'leads' "
        "AND indexname IN ('one_active_lead_per_contact', 'one_active_lead_per_company')"
    )
    assert len(rows) == 2

    exclusion_pattern = re.compile(r"status <> ALL \(ARRAY\[(.*?)\]\)")
    value_pattern = re.compile(r"'([a-z_]+)'::text")

    exclusions: dict[str, set[str]] = {}
    for row in rows:
        match = exclusion_pattern.search(row["indexdef"])
        assert match is not None, f"no exclusion list found in {row['indexdef']!r}"
        exclusions[row["indexname"]] = set(value_pattern.findall(match.group(1)))

    contact_exclusions = exclusions["one_active_lead_per_contact"]
    company_exclusions = exclusions["one_active_lead_per_company"]

    assert contact_exclusions == company_exclusions
    assert "deferred" in contact_exclusions
    assert contact_exclusions == {
        "deferred",
        "converted",
        "disqualified",
        "unsubscribed",
        "dormant",
    }


class _ForcedFailureTransaction:
    async def __aenter__(self) -> None:
        raise asyncpg.exceptions.UniqueViolationError("simulated: fault-injection test")

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FaultInjectingConnection:
    """Duck-typed proxy over a real asyncpg.Connection, forcing `.transaction()`
    to fail. asyncpg.Connection is a C-extension object and does not allow
    monkeypatching instance attributes directly (`'Connection' object
    attribute 'transaction' is read-only`), so a wrapper is the only way to
    inject this fault — repositories.create_lead() only ever calls
    `fetchrow` and `transaction` on the connection it's given.
    """

    def __init__(self, real: asyncpg.Connection) -> None:
        self._real = real

    async def fetchrow(self, *args: object, **kwargs: object) -> asyncpg.Record | None:
        return await self._real.fetchrow(*args, **kwargs)  # type: ignore[arg-type]

    def transaction(self, *args: object, **kwargs: object) -> _ForcedFailureTransaction:
        return _ForcedFailureTransaction()


@pytest.mark.protected
async def test_deferred_insert_failure_returns_typed_result_not_raise(
    conn: asyncpg.Connection,
):
    """The deferred-placeholder insert cannot fail via either single-thread
    index with real data today: status='deferred' is excluded from both
    indexes' predicates (confirmed live by the test above), so it cannot
    violate either one, and every other column reuses values that already
    passed on the first insert attempt — there is no real way to construct a
    failing "double collision" against the current schema. That's a property
    of the current schema, not a guarantee create_lead() should trust blindly
    forever (see docs/decisions.md). This proves the defensive path itself
    works — that create_lead() is total and never raises a raw database
    exception from that step — via fault injection, since it can't be
    reached with genuine data.
    """
    company = await repo.upsert_company(conn, name="Acme", domain="acme.com")
    contact_a = await repo.upsert_contact(conn, email="a@acme.com", company_id=company.id)
    contact_b = await repo.upsert_contact(conn, email="b@acme.com", company_id=company.id)

    await repo.create_lead(
        conn,
        contact_id=contact_a.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.DISCOVERY,
    )

    faulty_conn = _FaultInjectingConnection(conn)
    result = await repo.create_lead(
        faulty_conn,  # type: ignore[arg-type]
        contact_id=contact_b.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.DISCOVERY,
    )

    assert result.failed is True
    assert result.lead is None
    assert result.deferred is False
    assert result.error is not None


@pytest.mark.protected
async def test_outbound_lead_with_null_problem_statement_inserts_fine(conn: asyncpg.Connection):
    contact = await repo.upsert_contact(conn, email="jane@acme.com")

    result = await repo.create_lead(
        conn,
        contact_id=contact.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.DISCOVERY,
        problem_statement=None,
    )

    assert result.deferred is False
    assert result.lead.problem_statement is None


@pytest.mark.protected
async def test_inbound_lead_with_null_problem_statement_is_rejected(conn: asyncpg.Connection):
    contact = await repo.upsert_contact(conn, email="jane@acme.com")

    with pytest.raises(asyncpg.CheckViolationError):
        await repo.create_lead(
            conn,
            contact_id=contact.id,
            industry_pack="b2b-service-firms",
            source=LeadSource.WEBFORM,
            problem_statement=None,
        )


async def test_update_lead_status_transitions_and_persists(conn: asyncpg.Connection):
    contact = await repo.upsert_contact(conn, email="jane@acme.com")
    result = await repo.create_lead(
        conn, contact_id=contact.id, industry_pack="b2b-service-firms", source=LeadSource.DISCOVERY
    )

    updated = await repo.update_lead_status(conn, result.lead.id, LeadStatus.ENRICHING)

    assert updated.status == LeadStatus.ENRICHING
    reread = await repo.get_lead(conn, result.lead.id)
    assert reread is not None
    assert reread.status == LeadStatus.ENRICHING


# ============================================================================
# Events (outbox)
# ============================================================================


async def test_emit_event_reemission_with_same_idempotency_key_is_a_noop(
    conn: asyncpg.Connection,
):
    correlation_id = uuid.uuid4()
    key = f"lead:{uuid.uuid4()}:captured"

    first = await repo.emit_event(
        conn,
        type="lead.captured",
        payload={"lead_id": str(uuid.uuid4())},
        correlation_id=correlation_id,
        actor="agent:leadgen",
        idempotency_key=key,
    )
    second = await repo.emit_event(
        conn,
        type="lead.captured",
        payload={"lead_id": str(uuid.uuid4())},  # different payload, same key
        correlation_id=correlation_id,
        actor="agent:leadgen",
        idempotency_key=key,
    )

    assert first.event_id == second.event_id
    assert first.payload == second.payload  # second call's payload was discarded

    count = await conn.fetchval("SELECT count(*) FROM events WHERE idempotency_key = $1", key)
    assert count == 1


async def test_get_event_roundtrip(conn: asyncpg.Connection):
    emitted = await repo.emit_event(
        conn,
        type="lead.enriched",
        payload={"lead_id": str(uuid.uuid4())},
        correlation_id=uuid.uuid4(),
        actor="agent:leadgen",
        idempotency_key=f"lead:{uuid.uuid4()}:enriched",
    )

    fetched = await repo.get_event(conn, emitted.event_id)
    assert fetched is not None
    assert fetched.type == "lead.enriched"


@pytest.mark.protected
async def test_duplicate_idempotency_key_is_rejected(conn: asyncpg.Connection):
    """Constraint-level test, distinct from the no-op-via-emit_event test
    above: a raw duplicate INSERT (bypassing ON CONFLICT) must fail."""
    key = f"lead:{uuid.uuid4()}:captured"
    await conn.execute(
        "INSERT INTO events (type, actor, correlation_id, idempotency_key, payload) "
        "VALUES ($1, $2, $3, $4, $5::jsonb)",
        "lead.captured",
        "agent:leadgen",
        uuid.uuid4(),
        key,
        "{}",
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(
            "INSERT INTO events (type, actor, correlation_id, idempotency_key, payload) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            "lead.enriched",
            "agent:leadgen",
            uuid.uuid4(),
            key,
            "{}",
        )


# ============================================================================
# Jobs (queue)
# ============================================================================


async def test_enqueue_and_claim_job(conn: asyncpg.Connection):
    job = await repo.enqueue_job(
        conn, type="leadgen.enrich", payload={"lead_id": str(uuid.uuid4())}
    )
    assert job.status == JobStatus.PENDING

    claimed = await repo.claim_job(conn, worker_id="worker-1")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.locked_by == "worker-1"


async def test_claim_job_skips_future_run_after(conn: asyncpg.Connection):
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    await repo.enqueue_job(conn, type="leadgen.enrich", payload={}, run_after=future)

    claimed = await repo.claim_job(conn, worker_id="worker-1")
    assert claimed is None


async def test_complete_job(conn: asyncpg.Connection):
    job = await repo.enqueue_job(conn, type="leadgen.enrich", payload={})
    completed = await repo.complete_job(conn, job.id)
    assert completed.status == JobStatus.COMPLETED


async def test_mark_job_failed_with_retry_goes_back_to_pending(conn: asyncpg.Connection):
    job = await repo.enqueue_job(conn, type="leadgen.enrich", payload={})
    retry_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)

    failed = await repo.mark_job_failed(
        conn, job.id, error="provider timeout", retry_after=retry_at
    )

    assert failed.status == JobStatus.PENDING
    assert failed.attempts == 1
    assert failed.last_error == "provider timeout"


async def test_mark_job_failed_without_retry_dead_letters(conn: asyncpg.Connection):
    job = await repo.enqueue_job(conn, type="leadgen.enrich", payload={})

    failed = await repo.mark_job_failed(conn, job.id, error="fatal", retry_after=None)

    assert failed.status == JobStatus.DEAD_LETTER


# ============================================================================
# Constraints on tables outside M0.2's repository scope (messages, deals) —
# exercised via raw SQL since messages/deals repositories don't exist yet.
# ============================================================================


@pytest.mark.protected
async def test_duplicate_provider_message_id_is_rejected(conn: asyncpg.Connection):
    contact = await repo.upsert_contact(conn, email="jane@acme.com")
    lead = await repo.create_lead(
        conn, contact_id=contact.id, industry_pack="b2b-service-firms", source=LeadSource.DISCOVERY
    )

    await conn.execute(
        "INSERT INTO messages (lead_id, contact_id, direction, channel, provider_message_id) "
        "VALUES ($1, $2, 'outbound', 'email', $3)",
        lead.lead.id,
        contact.id,
        "gmail-msg-123",
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(
            "INSERT INTO messages (lead_id, contact_id, direction, channel, provider_message_id) "
            "VALUES ($1, $2, 'outbound', 'email', $3)",
            lead.lead.id,
            contact.id,
            "gmail-msg-123",
        )


@pytest.mark.protected
async def test_closing_deal_without_fx_rate_is_rejected(conn: asyncpg.Connection):
    contact = await repo.upsert_contact(conn, email="jane@acme.com")
    lead = await repo.create_lead(
        conn, contact_id=contact.id, industry_pack="b2b-service-firms", source=LeadSource.DISCOVERY
    )

    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO deals (lead_id, stage, outcome, value_amount, currency) "
            "VALUES ($1, 'closed_won', 'won', 5000.00, 'USD')",
            lead.lead.id,
        )
