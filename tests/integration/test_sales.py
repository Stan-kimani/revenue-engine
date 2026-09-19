"""Integration tests for agents/sales.py's draft path and the full M1.4a flow,
against a real Postgres instance (TEST_DATABASE_URL — tests/_db_safety.py).

The Anthropic client and the Gmail transport are always stubs: no model call,
and never a real send.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import asyncpg
import pytest

from revenue_engine.agents import sales
from revenue_engine.core import approvals
from revenue_engine.core import events as core_events
from revenue_engine.core.config import DeliverabilityConfig, load_config
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import (
    ApprovalStatus,
    EmailStatus,
    LeadSource,
    SendState,
)

pytestmark = pytest.mark.integration

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py"


def _load_run_worker_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_worker_script_for_sales", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_worker = _load_run_worker_module()


@pytest.fixture
async def conn(database_url: str):
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute(
            "TRUNCATE companies, contacts, leads, messages, approvals, suppressions, "
            "sending_pauses, events, jobs, agent_runs RESTART IDENTITY CASCADE"
        )
        await connection.close()


class _SinglePoolLike:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _Acquire:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


# --- stubs -----------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int = 10
    output_tokens: int = 20


@dataclass
class _Block:
    text: str


@dataclass
class _Response:
    content: list[_Block]
    usage: _Usage = field(default_factory=_Usage)


class _Messages:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs: Any) -> _Response:
        self.calls += 1
        return _Response(content=[_Block(text=self._responses.pop(0))])


class _LLM:
    def __init__(self, responses: list[str]) -> None:
        self.messages = _Messages(responses)


@dataclass
class _Transport:
    sent: list[str] = field(default_factory=list)

    async def authenticated_address(self) -> str:
        return "kimani@getkimani.com"

    async def send_raw(self, raw: str) -> tuple[str, str | None]:
        self.sent.append(raw)
        return f"gmail-{uuid.uuid4().hex[:10]}", "thread-1"


_PROFILE = {
    "summary": "A 30-person agency hiring for operations.",
    "likely_challenges": [],
    "personalization_anchors": [
        {
            "anchor_id": "anchor_1",
            "fact": "Posted an operations coordinator role this month",
            "source": "job board",
            "confidence": 0.9,
        }
    ],
    "disqualifying_signals": [],
    "recommended_angle": "Ask about intake load behind the hire.",
    "insufficient_context": False,
}
_BRIEF = {
    "angle": "The ops coordinator hire suggests intake volume outgrew the process.",
    "supporting_anchors": ["anchor_1"],
    "proof_to_reference": [],
    "avoid": ["Claiming to know their numbers"],
    "confidence": 0.7,
}
_DRAFT = {
    "subject": "the ops coordinator role",
    "body": (
        "Noticed you are hiring an operations coordinator. When that role opens, it is "
        "usually because intake and follow-up grew faster than the process behind them. "
        "Is the hire meant to absorb volume, or to fix how the work moves?"
    ),
    "cta": {"kind": "question", "text": "Is the hire meant to absorb volume?"},
    "facts_asserted": [{"claim": "hiring an operations coordinator", "anchor_id": "anchor_1"}],
    "tone_check": {"matches_voice_rules": True, "notes": "Plain, specific."},
}


def _cfg() -> DeliverabilityConfig:
    return replace(
        load_config().deliverability,
        physical_address="123 Main St, Springfield, IL 62701, USA",
        jitter_seconds=0,
    )


def _in_window_now() -> datetime:
    now = datetime.now(UTC)
    candidate = (now - timedelta(days=(now.weekday() - 2) % 7)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    return candidate - timedelta(days=7) if candidate > now else candidate


async def _seed_lead(
    conn: asyncpg.Connection,
    *,
    profile: dict[str, Any] | None,
    email_status: EmailStatus = EmailStatus.VALID,
) -> tuple[uuid.UUID, str]:
    tag = uuid.uuid4().hex[:8]
    company = await repo.upsert_company(
        conn, name="Acme", domain=f"acme-{tag}.example", country="US"
    )
    contact = await repo.upsert_contact(
        conn, email=f"pat@acme-{tag}.example", email_status=email_status, company_id=company.id
    )
    created = await repo.create_lead(
        conn,
        contact_id=contact.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.MANUAL_IMPORT,
    )
    assert created.lead is not None
    if profile is not None:
        await repo.update_lead_profile(conn, created.lead.id, profile)
    return created.lead.id, contact.email


async def _emit_qualified(conn: asyncpg.Connection, lead_id: uuid.UUID, band: str) -> Any:
    return await core_events.emit(
        conn,
        type=f"lead.qualified.{band}",
        payload={
            "lead_id": str(lead_id),
            "band": band,
            "total": 82.0,
            "campaign_id": None,
            "source": "manual_import",
        },
        correlation_id=uuid.uuid4(),
        actor="test",
        idempotency_key=f"lead:{lead_id}:qualified:{band}:{uuid.uuid4()}",
    )


async def _run_all_jobs(conn: asyncpg.Connection) -> None:
    while True:
        claimed = await repo.claim_jobs(conn, worker_id="w1", limit=10)
        if not claimed:
            return
        for job in claimed:
            await run_worker.process_one_job(conn, job, "w1")


# --- the full path ---------------------------------------------------------


async def test_full_path_qualified_sql_to_draft_to_approval_to_send_to_recorded(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    cfg = _cfg()
    now = _in_window_now()
    llm = _LLM([json.dumps(_BRIEF), json.dumps(_DRAFT)])
    transport = _Transport()
    monkeypatch.setitem(
        run_worker.HANDLERS,
        "sales.draft_outreach",
        functools.partial(sales.handle_draft_outreach, client=llm, config=cfg),
    )
    monkeypatch.setitem(
        run_worker.HANDLERS,
        "sales.resume_gated_action",
        functools.partial(sales.handle_send_approved, config=cfg),
    )
    monkeypatch.setitem(
        run_worker.HANDLERS,
        "sales.send_outreach",
        functools.partial(
            sales.handle_send_outreach,
            transport=transport,
            config=cfg,
            environ={"ENV": "production"},
            now=now,
        ),
    )

    # The notifier is not under test here; it must not reach Slack.
    async def _no_slack(conn: asyncpg.Connection, job: Any) -> None:
        return None

    monkeypatch.setitem(run_worker.HANDLERS, "slack.notify_approval_request", _no_slack)

    lead_id, to_address = await _seed_lead(conn, profile=_PROFILE)
    await _emit_qualified(conn, lead_id, "sql")

    # lead.qualified.sql -> sales.draft_outreach
    while await run_worker.dispatch_one_event(_SinglePoolLike(conn)):
        pass
    await _run_all_jobs(conn)

    message = await repo.get_outbound_message_for_lead_step(conn, lead_id=lead_id, sequence_step=0)
    assert message is not None and message.send_state == SendState.DRAFTED
    assert message.approval_id is not None
    assert cfg.opt_out_sentence in (message.body_text or "")
    assert cfg.physical_address in (message.body_text or "")
    approval = await repo.get_approval(conn, message.approval_id)
    assert approval is not None and approval.status == ApprovalStatus.PENDING
    assert approval.payload["body"] == message.body_text  # the human sees exactly what sends
    assert transport.sent == []  # nothing sendable until granted

    # A human approves.
    await approvals.resolve(
        conn, message.approval_id, decision=ApprovalStatus.GRANTED, decided_by="human:stan"
    )
    while await run_worker.dispatch_one_event(_SinglePoolLike(conn)):
        pass
    await _run_all_jobs(conn)

    assert len(transport.sent) == 1
    sent = await repo.get_message(conn, message.id)
    assert sent is not None and sent.send_state == SendState.SENT
    assert sent.provider_message_id is not None and sent.thread_id == "thread-1"
    assert sent.to_address == to_address.lower()
    events = [r["type"] for r in await conn.fetch("SELECT type FROM events ORDER BY occurred_at")]
    for expected in (
        "lead.qualified.sql",
        "approval.requested",
        "outreach.drafted",
        "approval.granted",
        "outreach.sent",
    ):
        assert expected in events
    lead = await repo.get_lead(conn, lead_id)
    assert lead is not None and lead.first_touched_at is not None
    failed = await conn.fetchval("SELECT count(*) FROM jobs WHERE status <> 'completed'")
    assert failed == 0


# --- draft preconditions ---------------------------------------------------


async def _run_draft(conn: asyncpg.Connection, lead_id: uuid.UUID, band: str, llm: _LLM) -> None:
    event = await _emit_qualified(conn, lead_id, band)
    job = await repo.enqueue_job(
        conn,
        type="sales.draft_outreach",
        payload={
            **event.payload,
            "correlation_id": str(event.correlation_id),
            "source_event_id": str(event.event_id),
        },
    )
    claimed = await repo.claim_jobs(conn, worker_id="w1", limit=10)
    assert [j.id for j in claimed] == [job.id]
    await sales.handle_draft_outreach(conn, claimed[0], client=llm, config=_cfg())


async def test_no_anchors_skips_drafting_and_records_why(conn: asyncpg.Connection):
    llm = _LLM([])  # any model call would pop from an empty list and fail
    profile = {**_PROFILE, "personalization_anchors": []}
    lead_id, _ = await _seed_lead(conn, profile=profile)

    await _run_draft(conn, lead_id, "sql", llm)

    assert llm.messages.calls == 0
    assert (
        await repo.get_outbound_message_for_lead_step(conn, lead_id=lead_id, sequence_step=0)
        is None
    )
    assert await conn.fetchval("SELECT count(*) FROM approvals") == 0
    rows = await conn.fetch("SELECT payload FROM events WHERE type = 'outreach.blocked'")
    payload = json.loads(rows[0]["payload"])
    assert payload["reason"] == "no_personalization_anchors"
    assert payload["disposition"] == "skipped" and payload["draft_id"] is None


async def test_unverified_contact_is_not_drafted_under_default_config(conn: asyncpg.Connection):
    llm = _LLM([])
    lead_id, _ = await _seed_lead(conn, profile=_PROFILE, email_status=EmailStatus.UNVERIFIED)

    await _run_draft(conn, lead_id, "sql", llm)

    assert llm.messages.calls == 0
    rows = await conn.fetch("SELECT payload FROM events WHERE type = 'outreach.blocked'")
    assert json.loads(rows[0]["payload"])["reason"] == "email_status_not_allowed"


async def test_mql_is_a_no_op_while_draft_bands_is_sql_only(conn: asyncpg.Connection):
    llm = _LLM([])
    lead_id, _ = await _seed_lead(conn, profile=_PROFILE)

    await _run_draft(conn, lead_id, "mql", llm)

    assert llm.messages.calls == 0
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0
    assert await conn.fetchval("SELECT count(*) FROM events WHERE type = 'outreach.blocked'") == 0


async def test_redelivered_qualified_event_does_not_draft_twice(conn: asyncpg.Connection):
    lead_id, _ = await _seed_lead(conn, profile=_PROFILE)
    await _run_draft(conn, lead_id, "sql", _LLM([json.dumps(_BRIEF), json.dumps(_DRAFT)]))
    second = _LLM([])
    await _run_draft(conn, lead_id, "sql", second)

    assert second.messages.calls == 0
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1
    assert await conn.fetchval("SELECT count(*) FROM approvals") == 1
