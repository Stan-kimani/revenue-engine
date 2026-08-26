"""Integration tests for agents/qualification.py against a real Postgres
instance (TEST_DATABASE_URL, never DATABASE_URL — tests/_db_safety.py).

The Anthropic client is always a stub here — never a real API call, no
ANTHROPIC_API_KEY needed (same pattern as tests/integration/test_leadgen.py).

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import asyncpg
import pytest

from revenue_engine.agents import qualification
from revenue_engine.core import events as core_events
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import Event, JobStatus, LeadBand, LeadSource
from revenue_engine.orchestrator import router

pytestmark = pytest.mark.integration

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py"


def _load_run_worker_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_worker_script_for_qualification", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_worker = _load_run_worker_module()

# database_url fixture comes from tests/integration/conftest.py.


@pytest.fixture
async def conn(database_url: str):
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute(
            "TRUNCATE companies, contacts, leads, lead_scores, messages, meetings, "
            "events, jobs, agent_runs RESTART IDENTITY CASCADE"
        )
        await connection.close()


class _SinglePoolLike:
    """Adapts one already-connected test connection to the
    asyncpg.Pool-shaped interface dispatch_one_event() expects — same helper
    as tests/integration/test_leadgen.py."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)


class _AcquireContext:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


# ---------------------------------------------------------------------------
# Stub Anthropic client — same duck-typed shape as test_complete_json.py /
# test_leadgen.py.
# ---------------------------------------------------------------------------


@dataclass
class _StubUsage:
    input_tokens: int = 10
    output_tokens: int = 20


@dataclass
class _StubBlock:
    text: str


@dataclass
class _StubResponse:
    content: list[_StubBlock]
    usage: _StubUsage = field(default_factory=_StubUsage)


class _StubMessages:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _StubResponse:
        self.calls.append(kwargs)
        text = self._responses.pop(0)
        return _StubResponse(content=[_StubBlock(text=text)])


class _StubClient:
    def __init__(self, responses: list[str]):
        self.messages = _StubMessages(responses)


def _subscores(
    *, buying_intent: float = 0.6, seniority_fit: float = 0.7, narrative_fit: float = 0.5
) -> dict:
    def _sub(score: float) -> dict:
        return {"score": score, "evidence": ["evidence snippet"], "confidence": 0.7}

    return {
        "buying_intent": _sub(buying_intent),
        "seniority_fit": _sub(seniority_fit),
        "narrative_fit": _sub(narrative_fit),
        "overall_note": "Reasonable fit, moderate signal.",
    }


def _client_with_subscores(**overrides: float) -> _StubClient:
    return _StubClient([json.dumps(_subscores(**overrides))])


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_lead(
    conn: asyncpg.Connection,
    *,
    domain: str,
    email: str,
    source: LeadSource = LeadSource.MANUAL_IMPORT,
    problem_statement: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Creates company/contact/lead rows with enough attributes for the
    deterministic scorer to have something real to read (business_model,
    seniority), and a profile so {{prospect_profile}} isn't empty. Returns
    (lead_id, contact_id, company_id)."""
    company = await repo.upsert_company(
        conn,
        name="Fixture Co",
        domain=domain,
        employee_band="11-50",
        country="US",
        attributes={
            "business_model": {
                "value": "agency",
                "confidence": 0.8,
                "evidence": "test",
                "source": "llm:test",
                "run_id": None,
                "observed_at": "2026-01-01T00:00:00Z",
            }
        },
    )
    contact = await repo.upsert_contact(
        conn,
        email=email,
        first_name="Jordan",
        last_name="Reyes",
        title="COO",
        company_id=company.id,
        attributes={
            "seniority": {
                "value": "founder_owner",
                "confidence": 0.8,
                "evidence": "test",
                "source": "llm:test",
                "run_id": None,
                "observed_at": "2026-01-01T00:00:00Z",
            }
        },
    )
    result = await repo.create_lead(
        conn,
        contact_id=contact.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=source,
        problem_statement=problem_statement,
    )
    assert result.lead is not None and not result.failed and not result.deferred
    await repo.update_lead_profile(
        conn, result.lead.id, {"summary": "test profile", "personalization_anchors": []}
    )
    return result.lead.id, contact.id, company.id


async def _emit_lead_enriched(
    conn: asyncpg.Connection, *, lead_id: uuid.UUID, contact_id: uuid.UUID, company_id: uuid.UUID
) -> Event:
    return await core_events.emit(
        conn,
        type="lead.enriched",
        payload={
            "lead_id": str(lead_id),
            "contact_id": str(contact_id),
            "company_id": str(company_id),
            "fields_enriched": ["business_model", "seniority"],
            "email_status": "unverified",
            "run_id": str(uuid.uuid4()),
        },
        correlation_id=uuid.uuid4(),
        actor="test",
        idempotency_key=f"lead:{lead_id}:enriched",
    )


# ---------------------------------------------------------------------------
# End-to-end: lead.enriched -> router -> qualification.score job -> handler
# -> lead.scored -> lead.qualified.X. Exercises the REAL dispatch_one_event/
# process_one_job path and the REAL scripts/run_worker.py HANDLERS
# registration.
# ---------------------------------------------------------------------------


async def test_lead_enriched_routes_through_worker_to_qualification_and_emits_lead_scored(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    lead_id, contact_id, company_id = await _seed_lead(
        conn, domain="e2e-fixture.example", email="e2e@e2e-fixture.example"
    )
    event = await _emit_lead_enriched(
        conn, lead_id=lead_id, contact_id=contact_id, company_id=company_id
    )

    # router.py already maps lead.enriched -> JobSpec("qualification.score")
    # (M0.3, verified live rather than assumed).
    assert (
        router.route(event.type) and router.route(event.type)[0].job_type == "qualification.score"
    )

    processed = await run_worker.dispatch_one_event(_SinglePoolLike(conn))
    assert processed is True

    jobs = await conn.fetch(
        "SELECT * FROM jobs WHERE (payload->>'source_event_id')::uuid = $1", event.event_id
    )
    assert len(jobs) == 1
    assert jobs[0]["type"] == "qualification.score"

    # scripts/run_worker.py's own HANDLERS dict is what's under test here —
    # monkeypatch only the client injection, never bypass HANDLERS itself.
    monkeypatch.setitem(
        run_worker.HANDLERS,
        "qualification.score",
        functools.partial(qualification.handle_score, client=_client_with_subscores()),
    )

    claimed = await repo.claim_jobs(conn, worker_id="w1", limit=10)
    assert len(claimed) == 1
    await run_worker.process_one_job(conn, claimed[0], "w1")

    completed_job = await repo.get_job(conn, claimed[0].id)
    assert completed_job is not None
    assert completed_job.status == JobStatus.COMPLETED

    scored = await conn.fetchrow("SELECT * FROM events WHERE type = 'lead.scored'")
    assert scored is not None
    scored_payload = json.loads(scored["payload"])
    assert scored_payload["lead_id"] == str(lead_id)
    assert scored_payload["industry_pack"] == "b2b-service-firms"

    lead = await repo.get_lead(conn, lead_id)
    assert lead is not None
    assert lead.band is not None
    assert lead.current_score is not None

    qualified = await conn.fetchrow(
        f"SELECT * FROM events WHERE type = 'lead.qualified.{lead.band.value}'"
    )
    assert qualified is not None


# ---------------------------------------------------------------------------
# Protected: inbound bypass emits BOTH the band event AND lead.routed_to_human.
# ---------------------------------------------------------------------------


@pytest.mark.protected
async def test_inbound_source_emits_both_band_event_and_routed_to_human(conn: asyncpg.Connection):
    lead_id, contact_id, company_id = await _seed_lead(
        conn,
        domain="inbound-fixture.example",
        email="inbound@inbound-fixture.example",
        source=LeadSource.WEBFORM,
        problem_statement="Our intake process is entirely manual.",
    )
    event = await _emit_lead_enriched(
        conn, lead_id=lead_id, contact_id=contact_id, company_id=company_id
    )
    job = await repo.enqueue_job(
        conn,
        type="qualification.score",
        payload={
            **event.payload,
            "source_event_id": str(event.event_id),
            "correlation_id": str(event.correlation_id),
        },
    )
    claimed = await repo.claim_jobs(conn, worker_id="w1", limit=10)
    assert len(claimed) == 1

    await qualification.handle_score(conn, claimed[0], client=_client_with_subscores())

    lead = await repo.get_lead(conn, lead_id)
    assert lead is not None and lead.band is not None

    qualified = await conn.fetchrow(
        f"SELECT * FROM events WHERE type = 'lead.qualified.{lead.band.value}'"
    )
    assert qualified is not None

    routed = await conn.fetchrow("SELECT * FROM events WHERE type = 'lead.routed_to_human'")
    assert routed is not None
    routed_payload = json.loads(routed["payload"])
    assert routed_payload["lead_id"] == str(lead_id)
    assert routed_payload["source"] == "webform"
    assert routed_payload["reason"] == "inbound_bypass"
    assert job.id  # sanity: the job we hand-enqueued was the one claimed/handled


# ---------------------------------------------------------------------------
# Standard: append-only re-scoring, deterministic+llm reconcile to total, and
# the reply.received-triggered re-score path (a job enqueued directly — jobs
# aren't schema-validated, so this needs no reply.received event schema).
# ---------------------------------------------------------------------------


async def test_lead_scores_is_append_only_across_a_reply_received_rescore(conn: asyncpg.Connection):
    lead_id, contact_id, company_id = await _seed_lead(
        conn, domain="rescore-fixture.example", email="rescore@rescore-fixture.example"
    )
    enriched_event = await _emit_lead_enriched(
        conn, lead_id=lead_id, contact_id=contact_id, company_id=company_id
    )
    first_job = await repo.enqueue_job(
        conn,
        type="qualification.score",
        payload={
            **enriched_event.payload,
            "source_event_id": str(enriched_event.event_id),
            "correlation_id": str(enriched_event.correlation_id),
        },
    )
    claimed = await repo.claim_jobs(conn, worker_id="w1", limit=10)
    await qualification.handle_score(conn, claimed[0], client=_client_with_subscores())

    scores_after_first = await repo.get_lead_scores(conn, lead_id)
    assert len(scores_after_first) == 1

    # Simulate engagement: an inbound reply, then a reply.received-shaped
    # re-score job (event-catalog.md's reply.received payload shape) —
    # enqueued directly via repo.enqueue_job, not core_events.emit(), since
    # jobs carry no schema and the sales agent that would emit a real
    # reply.received event doesn't exist yet (out of scope for M1.2).
    await conn.execute(
        "INSERT INTO messages (lead_id, contact_id, direction, channel) "
        "VALUES ($1, $2, 'inbound', 'email')",
        lead_id,
        contact_id,
    )
    # reply.received has no schemas/events/reply.received.json yet (out of
    # scope for M1.2 — no emitter for it exists until the sales agent does),
    # so this uses repo.emit_event() directly: the repository primitive
    # UNDER core_events.emit(), which persists without schema validation
    # (that's core/events.py's job). It's the honest way to get a real,
    # FK-satisfying `events` row of a type this milestone deliberately
    # doesn't ship a schema for yet, using event-catalog.md §3's own
    # documented reply.received payload shape.
    reply_event = await repo.emit_event(
        conn,
        type="reply.received",
        payload={
            "lead_id": str(lead_id),
            "contact_id": str(contact_id),
            "message_id": str(uuid.uuid4()),
            "provider_message_id": "msg-1",
            "thread_id": "thread-1",
        },
        correlation_id=uuid.uuid4(),
        actor="test",
        idempotency_key="message:msg-1:received",
    )
    second_job = await repo.enqueue_job(
        conn,
        type="qualification.score",
        payload={
            **reply_event.payload,
            "source_event_id": str(reply_event.event_id),
            "correlation_id": str(reply_event.correlation_id),
        },
    )
    claimed_second = await repo.claim_jobs(conn, worker_id="w1", limit=10)
    assert len(claimed_second) == 1
    assert claimed_second[0].id == second_job.id
    await qualification.handle_score(
        conn, claimed_second[0], client=_client_with_subscores(buying_intent=0.9)
    )

    scores_after_second = await repo.get_lead_scores(conn, lead_id)
    assert len(scores_after_second) == 2  # a new row, never an update
    assert scores_after_second[0].id == scores_after_first[0].id
    assert scores_after_second[0].total == scores_after_first[0].total  # the OLD row is untouched
    assert scores_after_second[1].id != scores_after_first[0].id

    for score in scores_after_second:
        assert score.deterministic_part is not None and score.llm_part is not None
        assert (
            score.deterministic_part + score.llm_part == score.total
        )  # exact Decimal reconciliation

    # The second run counted the reply the first run couldn't have seen yet.
    second_components = scores_after_second[1].components
    assert "replies=1" in second_components["engagement"]["evidence"]
    assert first_job.id != second_job.id  # sanity: genuinely two different jobs


async def test_deterministic_and_llm_parts_reconcile_to_total_on_a_fresh_score(
    conn: asyncpg.Connection,
):
    lead_id, contact_id, company_id = await _seed_lead(
        conn, domain="reconcile-fixture.example", email="reconcile@reconcile-fixture.example"
    )
    event = await _emit_lead_enriched(
        conn, lead_id=lead_id, contact_id=contact_id, company_id=company_id
    )
    await repo.enqueue_job(
        conn,
        type="qualification.score",
        payload={
            **event.payload,
            "source_event_id": str(event.event_id),
            "correlation_id": str(event.correlation_id),
        },
    )
    claimed = await repo.claim_jobs(conn, worker_id="w1", limit=10)
    await qualification.handle_score(conn, claimed[0], client=_client_with_subscores())

    scores = await repo.get_lead_scores(conn, lead_id)
    assert len(scores) == 1
    score = scores[0]
    assert score.deterministic_part is not None and score.llm_part is not None
    assert score.deterministic_part + score.llm_part == score.total

    lead = await repo.get_lead(conn, lead_id)
    assert lead is not None
    assert lead.current_score == score.total
    assert lead.band == score.band


async def test_disqualifier_hit_bands_the_lead_cold_end_to_end(conn: asyncpg.Connection):
    company = await repo.upsert_company(
        conn,
        name="Enterprise Fixture Co",
        domain="enterprise-fixture.example",
        employee_band="1000+",  # trips the `enterprise` disqualifier
        country="US",
    )
    contact = await repo.upsert_contact(
        conn, email="big@enterprise-fixture.example", first_name="Alex", company_id=company.id
    )
    result = await repo.create_lead(
        conn,
        contact_id=contact.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.MANUAL_IMPORT,
    )
    assert result.lead is not None
    await repo.update_lead_profile(
        conn, result.lead.id, {"summary": "big company", "personalization_anchors": []}
    )

    event = await _emit_lead_enriched(
        conn, lead_id=result.lead.id, contact_id=contact.id, company_id=company.id
    )
    job = await repo.enqueue_job(
        conn,
        type="qualification.score",
        payload={
            **event.payload,
            "source_event_id": str(event.event_id),
            "correlation_id": str(event.correlation_id),
        },
    )
    claimed = await repo.claim_jobs(conn, worker_id="w1", limit=10)
    # Even with a maximally generous LLM read, the disqualifier must win.
    await qualification.handle_score(
        conn,
        claimed[0],
        client=_client_with_subscores(buying_intent=1.0, seniority_fit=1.0, narrative_fit=1.0),
    )

    lead = await repo.get_lead(conn, result.lead.id)
    assert lead is not None
    assert lead.band == LeadBand.COLD

    scores = await repo.get_lead_scores(conn, result.lead.id)
    assert scores[0].components["disqualifiers_hit"]
    assert scores[0].components["disqualifiers_hit"][0]["id"] == "enterprise"
    assert job.id
