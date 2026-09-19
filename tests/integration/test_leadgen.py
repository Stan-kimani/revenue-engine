"""Integration tests for agents/leadgen.py against a real Postgres instance
(TEST_DATABASE_URL, never DATABASE_URL — tests/_db_safety.py).

The Anthropic client is always a stub here — never a real API call, no
ANTHROPIC_API_KEY needed (same pattern as tests/integration/test_complete_json.py).

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
import jsonschema
import pytest

from revenue_engine.agents import leadgen
from revenue_engine.core import events as core_events
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import Event, JobStatus, LeadSource, LeadStatus
from revenue_engine.orchestrator import router

pytestmark = pytest.mark.integration

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py"
_ATTRIBUTE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "entities" / "attribute.json"
)
_ATTRIBUTE_VALIDATOR = jsonschema.Draft202012Validator(
    json.loads(_ATTRIBUTE_SCHEMA_PATH.read_text())
)


def _load_run_worker_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_worker_script_for_leadgen", SCRIPT_PATH)
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
            "TRUNCATE companies, contacts, leads, events, jobs, agent_runs RESTART IDENTITY CASCADE"
        )
        await connection.close()


class _SinglePoolLike:
    """Adapts one already-connected test connection to the
    asyncpg.Pool-shaped interface dispatch_one_event() expects, so tests can
    reuse the shared `conn` fixture directly — same helper as
    tests/integration/test_dispatch.py."""

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
# Stub Anthropic client — same duck-typed shape as test_complete_json.py.
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


class _AlwaysInvalidClient:
    """Never returns schema-valid JSON — exercises complete_json()'s own
    retry-once-then-raise, so LLMValidationError is real, not simulated."""

    def __init__(self) -> None:
        self.messages = _StubMessages([json.dumps({"industry": {}})] * 4)


# ---------------------------------------------------------------------------
# Canned, schema-valid LLM outputs for the three leadgen prompts, in call
# order (enrich_company, enrich_decision_maker, build_prospect_profile).
# ---------------------------------------------------------------------------

_COMPANY_ENRICHMENT = {
    "industry": {
        "value": "B2B services",
        "confidence": 0.8,
        "evidence": "Company site describes itself as an operations consultancy.",
    },
    "sub_industry": {"value": None, "confidence": 0, "evidence": ""},
    "business_model": {
        "value": "b2b_services",
        "confidence": 0.7,
        "evidence": "Positioned as a service firm.",
    },
    "employee_band": {"value": None, "confidence": 0, "evidence": ""},
    "revenue_signal": {"value": None, "confidence": 0, "evidence": ""},
    "positioning_summary": {
        "value": "Runs delivery operations for small agencies.",
        "confidence": 0.6,
        "evidence": "Homepage tagline.",
    },
    "tech_signals": [{"name": "Google Workspace", "evidence": "Mentioned in footer."}],
    "insufficient_context": False,
}

_CONTACT_ENRICHMENT = {
    "seniority": {
        "value": "founder_owner",
        "confidence": 0.9,
        "evidence": "Title: COO, small company.",
    },
    "decision_authority": {
        "value": "economic_buyer",
        "confidence": 0.7,
        "evidence": "COO at a small firm.",
    },
    "functional_area": {
        "value": "operations",
        "confidence": 0.8,
        "evidence": "Title references operations.",
    },
    "likely_responsibilities": [
        {"responsibility": "Owns day-to-day delivery operations", "confidence": 0.7}
    ],
    "inferred_pains": [
        {
            "pain": "Manual client intake",
            "reasoning": "Small ops-heavy firm; no system of record mentioned.",
            "confidence": 0.5,
        }
    ],
    "insufficient_context": False,
}

_PROSPECT_PROFILE = {
    "summary": "A small operations-focused agency likely managing client work manually.",
    "likely_challenges": [
        {
            "challenge": "Manual intake and follow-up",
            "basis": "No system of record found.",
            "confidence": 0.5,
        }
    ],
    "personalization_anchors": [
        {
            "anchor_id": "anchor_1",
            "fact": "COO title at an operations consultancy",
            "source": "contact title",
            "confidence": 0.8,
        }
    ],
    "disqualifying_signals": [],
    "recommended_angle": "Ask about their current intake process.",
    "insufficient_context": False,
}


def _happy_path_client() -> _StubClient:
    return _StubClient(
        [
            json.dumps(_COMPANY_ENRICHMENT),
            json.dumps(_CONTACT_ENRICHMENT),
            json.dumps(_PROSPECT_PROFILE),
        ]
    )


async def _seed_lead(
    conn: asyncpg.Connection, *, domain: str, email: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Creates company/contact/lead rows the way scripts/import_leads.py
    would, and returns (lead_id, contact_id, company_id)."""
    company = await repo.upsert_company(conn, name="Fixture Co", domain=domain)
    contact = await repo.upsert_contact(
        conn,
        email=email,
        first_name="Jordan",
        last_name="Reyes",
        title="COO",
        company_id=company.id,
    )
    result = await repo.create_lead(
        conn,
        contact_id=contact.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.MANUAL_IMPORT,
    )
    assert result.lead is not None and not result.failed and not result.deferred
    return result.lead.id, contact.id, company.id


async def _emit_lead_captured(
    conn: asyncpg.Connection, *, lead_id: uuid.UUID, contact_id: uuid.UUID, company_id: uuid.UUID
) -> Event:
    return await core_events.emit(
        conn,
        type="lead.captured",
        payload={
            "lead_id": str(lead_id),
            "contact_id": str(contact_id),
            "company_id": str(company_id),
            "campaign_id": None,
            "source": "manual_import",
            "industry_pack": "b2b-service-firms",
        },
        correlation_id=uuid.uuid4(),
        actor="test",
        idempotency_key=f"lead:{contact_id}:manual:captured",
    )


# ---------------------------------------------------------------------------
# End-to-end: lead.captured -> router -> leadgen.enrich job -> handler ->
# lead.enriched. Exercises the REAL dispatch_one_event/process_one_job path
# and the REAL scripts/run_worker.py HANDLERS registration, not just a
# direct call to leadgen.handle_enrich() in isolation.
# ---------------------------------------------------------------------------


async def test_lead_captured_routes_through_worker_to_leadgen_and_emits_lead_enriched(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    lead_id, contact_id, company_id = await _seed_lead(
        conn, domain="e2e-fixture.example", email="e2e@e2e-fixture.example"
    )
    event = await _emit_lead_captured(
        conn, lead_id=lead_id, contact_id=contact_id, company_id=company_id
    )

    # router.py already maps lead.captured -> JobSpec("leadgen.enrich")
    # (M0.3, verified live rather than assumed).
    assert router.route(event.type) and router.route(event.type)[0].job_type == "leadgen.enrich"

    processed = await run_worker.dispatch_one_event(_SinglePoolLike(conn))
    assert processed is True

    jobs = await conn.fetch(
        "SELECT * FROM jobs WHERE (payload->>'source_event_id')::uuid = $1", event.event_id
    )
    assert len(jobs) == 1
    assert jobs[0]["type"] == "leadgen.enrich"

    # scripts/run_worker.py's own HANDLERS dict is what's under test here —
    # monkeypatch only the client injection, never bypass HANDLERS itself.
    monkeypatch.setitem(
        run_worker.HANDLERS,
        "leadgen.enrich",
        functools.partial(leadgen.handle_enrich, client=_happy_path_client()),
    )

    claimed = await repo.claim_jobs(conn, worker_id="w1", limit=10)
    assert len(claimed) == 1
    await run_worker.process_one_job(conn, claimed[0], "w1")

    completed_job = await repo.get_job(conn, claimed[0].id)
    assert completed_job is not None
    assert completed_job.status == JobStatus.COMPLETED

    enriched = await conn.fetchrow("SELECT * FROM events WHERE type = 'lead.enriched'")
    assert enriched is not None
    enriched_payload = json.loads(enriched["payload"])
    assert enriched_payload["lead_id"] == str(lead_id)
    assert enriched_payload["email_status"] == "unverified"
    assert "industry" in enriched_payload["fields_enriched"]

    lead = await repo.get_lead(conn, lead_id)
    assert lead is not None
    assert lead.profile is not None
    assert lead.profile["summary"] == _PROSPECT_PROFILE["summary"]
    assert lead.profile["personalization_anchors"][0]["anchor_id"] == "anchor_1"


@pytest.mark.protected
async def test_enrichment_writes_provenance_envelope_not_bare_scalar(conn: asyncpg.Connection):
    lead_id, contact_id, company_id = await _seed_lead(
        conn, domain="provenance-fixture.example", email="provenance@provenance-fixture.example"
    )
    event = await _emit_lead_captured(
        conn, lead_id=lead_id, contact_id=contact_id, company_id=company_id
    )
    job = await repo.enqueue_job(
        conn,
        type="leadgen.enrich",
        payload={
            **event.payload,
            "source_event_id": str(event.event_id),
            "correlation_id": str(event.correlation_id),
        },
    )

    await leadgen.handle_enrich(conn, job, client=_happy_path_client())

    company_row = await conn.fetchrow("SELECT attributes FROM companies WHERE id = $1", company_id)
    contact_row = await conn.fetchrow("SELECT attributes FROM contacts WHERE id = $1", contact_id)
    assert company_row is not None and contact_row is not None
    company_attributes = json.loads(company_row["attributes"])
    contact_attributes = json.loads(contact_row["attributes"])

    assert company_attributes, (
        "at least one company field should have cleared the confidence threshold"
    )
    assert contact_attributes, (
        "at least one contact field should have cleared the confidence threshold"
    )

    for field_name, envelope in {**company_attributes, **contact_attributes}.items():
        # Never a bare scalar (entity-model.md §2) — every value is the full
        # envelope, and it must independently pass the real validator, not
        # just "look like a dict".
        assert isinstance(envelope, dict), (
            f"{field_name} was written as a bare scalar: {envelope!r}"
        )
        errors = [e.message for e in _ATTRIBUTE_VALIDATOR.iter_errors(envelope)]
        assert not errors, f"{field_name} envelope failed schemas/entities/attribute.json: {errors}"
        assert envelope["source"].startswith("llm:enrich_")
        assert envelope["run_id"] is not None
        assert envelope["observed_at"] is not None

    # A field the stub returned with confidence below min_confidence_to_store
    # (sub_industry: confidence 0) must never have been written.
    assert "sub_industry" not in company_attributes


async def test_enrichment_failure_after_retries_emits_lead_enrichment_failed_no_partial_write(
    conn: asyncpg.Connection,
):
    lead_id, contact_id, company_id = await _seed_lead(
        conn, domain="failure-fixture.example", email="failure@failure-fixture.example"
    )
    event = await _emit_lead_captured(
        conn, lead_id=lead_id, contact_id=contact_id, company_id=company_id
    )
    job = await repo.enqueue_job(
        conn,
        type="leadgen.enrich",
        payload={
            **event.payload,
            "source_event_id": str(event.event_id),
            "correlation_id": str(event.correlation_id),
        },
    )

    await leadgen.handle_enrich(conn, job, client=_AlwaysInvalidClient())

    failed_event = await conn.fetchrow("SELECT * FROM events WHERE type = 'lead.enrichment_failed'")
    assert failed_event is not None
    payload = json.loads(failed_event["payload"])
    assert payload["lead_id"] == str(lead_id)
    assert payload["reason"] == "provider_error"
    assert payload["attempts"] >= 1

    enriched_event = await conn.fetchrow("SELECT * FROM events WHERE type = 'lead.enriched'")
    assert enriched_event is None

    company_row = await conn.fetchrow("SELECT attributes FROM companies WHERE id = $1", company_id)
    assert company_row is not None
    assert json.loads(company_row["attributes"]) == {}

    lead = await repo.get_lead(conn, lead_id)
    assert lead is not None
    assert lead.status == LeadStatus.ENRICH_FAILED
    assert lead.profile is None


async def test_lead_with_no_company_emits_enrichment_failed_reason_no_domain(
    conn: asyncpg.Connection,
):
    contact = await repo.upsert_contact(
        conn, email="orphan@orphan-fixture.example", first_name="No", last_name="Company"
    )
    result = await repo.create_lead(
        conn,
        contact_id=contact.id,
        company_id=None,
        industry_pack="b2b-service-firms",
        source=LeadSource.MANUAL_IMPORT,
    )
    assert result.lead is not None
    event = await core_events.emit(
        conn,
        type="lead.captured",
        payload={
            "lead_id": str(result.lead.id),
            "contact_id": str(contact.id),
            "company_id": None,
            "campaign_id": None,
            "source": "manual_import",
            "industry_pack": "b2b-service-firms",
        },
        correlation_id=uuid.uuid4(),
        actor="test",
        idempotency_key=f"lead:{contact.id}:manual:captured",
    )
    job = await repo.enqueue_job(
        conn,
        type="leadgen.enrich",
        payload={
            **event.payload,
            "source_event_id": str(event.event_id),
            "correlation_id": str(event.correlation_id),
        },
    )

    await leadgen.handle_enrich(conn, job)  # no client needed — never reached

    failed_event = await conn.fetchrow("SELECT * FROM events WHERE type = 'lead.enrichment_failed'")
    assert failed_event is not None
    assert json.loads(failed_event["payload"])["reason"] == "no_domain"


# ManualCsvProvider.verify_email() was removed at M1.4a: verification moved to
# integrations/email_verification.py, called once at import. The test that
# lived here asserted the old provider-level behaviour; its replacement is
# tests/unit/test_email_verification.py plus the import-time tests in
# tests/integration/test_email_verification.py.
