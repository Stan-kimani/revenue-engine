"""Integration tests for the send path (core/sending.py, integrations/gmail.py,
agents/sales.py send handlers) against a real Postgres instance
(TEST_DATABASE_URL, never DATABASE_URL — tests/_db_safety.py).

The Gmail transport is ALWAYS a stub. The real account is in warmup at 5/day; a
test suite must never consume that.

Every PROTECTED test attempts to bypass the normal path — calling the send
handler or the gate directly, without going through approval.granted — and
asserts the send is refused and the transport was never called.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from revenue_engine.agents import sales
from revenue_engine.core import approvals, sending
from revenue_engine.core.config import DeliverabilityConfig, load_config
from revenue_engine.core.errors import GmailSendRejectedError
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import (
    ActionType,
    ApprovalStatus,
    AutonomyLevel,
    EmailStatus,
    Job,
    JobStatus,
    LeadSource,
    SendState,
    SuppressionReason,
)

pytestmark = pytest.mark.integration

PRODUCTION = {"ENV": "production"}


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


def _in_window_now() -> datetime:
    """The most recent Wednesday 15:00 UTC (11:00 in New York) — inside the send
    window, and never in the future relative to rows the database timestamps
    with now()."""
    now = datetime.now(UTC)
    days_back = (now.weekday() - 2) % 7
    candidate = (now - timedelta(days=days_back)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


NOW = _in_window_now()


def _cfg(**overrides: Any) -> DeliverabilityConfig:
    base = replace(
        load_config().deliverability,
        physical_address="123 Main St, Springfield, IL 62701, USA",
        jitter_seconds=0,
    )
    return replace(base, **overrides)


@dataclass
class StubTransport:
    address: str = "kimani@getkimani.com"
    fail_with: BaseException | None = None
    sent: list[str] = field(default_factory=list)

    async def authenticated_address(self) -> str:
        return self.address

    async def send_raw(self, raw: str) -> tuple[str, str | None]:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(raw)
        return f"gmail-{len(self.sent)}-{uuid.uuid4().hex[:8]}", f"thread-{len(self.sent)}"


@dataclass
class Seeded:
    lead_id: uuid.UUID
    contact_id: uuid.UUID
    message_id: uuid.UUID
    approval_id: uuid.UUID
    to_address: str
    domain: str


async def _seed(
    conn: asyncpg.Connection,
    *,
    cfg: DeliverabilityConfig,
    approve: bool = True,
    email_status: EmailStatus = EmailStatus.VALID,
    country: str | None = "US",
    sequence_step: int = 0,
) -> Seeded:
    """A drafted message bound to an approval, built the way
    agents/sales.py::handle_draft_outreach builds one (minus the LLM calls)."""
    tag = uuid.uuid4().hex[:8]
    domain = f"acme-{tag}.example"
    company = await repo.upsert_company(conn, name="Acme", domain=domain, country=country)
    contact = await repo.upsert_contact(
        conn, email=f"pat@{domain}", email_status=email_status, company_id=company.id
    )
    created = await repo.create_lead(
        conn,
        contact_id=contact.id,
        company_id=company.id,
        industry_pack="b2b-service-firms",
        source=LeadSource.MANUAL_IMPORT,
    )
    assert created.lead is not None
    body = sending.compose_outbound_body(
        "Saw the ops coordinator role you posted. Worth a chat?", cfg
    )
    message = await repo.insert_outbound_draft(
        conn,
        lead_id=created.lead.id,
        contact_id=contact.id,
        campaign_id=None,
        subject="ops coordinator hire",
        body_text=body,
        sequence_step=sequence_step,
        prompt_version=1,
        from_address=cfg.from_address,
        to_address=contact.email,
    )
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.OUTREACH_DRAFT,
        payload={
            "lead_id": str(created.lead.id),
            "message_id": str(message.id),
            "to_address": contact.email,
            "from_address": cfg.from_address,
            "subject": message.subject,
            "body": body,
        },
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
        dedupe_key=f"outreach_draft:{message.id}",
    )
    await repo.set_message_approval(conn, message.id, approval.id)
    if approve:
        await approvals.resolve(
            conn, approval.id, decision=ApprovalStatus.GRANTED, decided_by="human:test"
        )
    return Seeded(created.lead.id, contact.id, message.id, approval.id, contact.email, domain)


def _send_job(seeded: Seeded) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid.uuid4(),
        type=sending.SEND_JOB_TYPE,
        payload={
            "message_id": str(seeded.message_id),
            "lead_id": str(seeded.lead_id),
            "to_address": seeded.to_address,
            "correlation_id": str(uuid.uuid4()),
        },
        status=JobStatus.RUNNING,
        run_after=now,
        attempts=0,
        locked_by="test",
        locked_at=now,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


async def _attempt(
    conn: asyncpg.Connection,
    seeded: Seeded,
    *,
    cfg: DeliverabilityConfig,
    transport: StubTransport,
    now: datetime = NOW,
    environ: dict[str, str] | None = None,
) -> None:
    """Calls the send handler directly — the bypass every protected test uses."""
    await sales.handle_send_outreach(
        conn,
        _send_job(seeded),
        transport=transport,
        config=cfg,
        environ=environ if environ is not None else PRODUCTION,
        now=now,
    )


async def _blocked_events(conn: asyncpg.Connection, message_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT payload FROM events WHERE type = 'outreach.blocked'")
    payloads = [json.loads(r["payload"]) for r in rows]
    return [p for p in payloads if p["draft_id"] == str(message_id)]


async def _insert_sent(
    conn: asyncpg.Connection, cfg: DeliverabilityConfig, *, started_at: datetime, step: int = 0
) -> None:
    await conn.execute(
        """
        INSERT INTO messages (direction, channel, from_address, to_address, send_state,
                              send_started_at, sent_at, provider_message_id, sequence_step)
        VALUES ('outbound', 'email', $1, $2, 'sent', $3, $3, $4, $5)
        """,
        cfg.from_address,
        f"someone-{uuid.uuid4().hex[:6]}@elsewhere.example",
        started_at,
        f"prior-{uuid.uuid4().hex}",
        step,
    )


# ===========================================================================
# PROTECTED — each attempts to bypass the normal path and asserts refusal
# ===========================================================================


@pytest.mark.protected
async def test_sending_without_an_approved_approval_row_is_refused(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport()

    pending = await _seed(conn, cfg=cfg, approve=False)
    await _attempt(conn, pending, cfg=cfg, transport=transport)

    unbound = await _seed(conn, cfg=cfg, approve=False)
    await conn.execute("UPDATE messages SET approval_id = NULL WHERE id = $1", unbound.message_id)
    await _attempt(conn, unbound, cfg=cfg, transport=transport)

    denied = await _seed(conn, cfg=cfg, approve=False)
    await approvals.resolve(
        conn, denied.approval_id, decision=ApprovalStatus.DENIED, decided_by="human:test"
    )
    await _attempt(conn, denied, cfg=cfg, transport=transport)

    assert transport.sent == []
    for seeded in (pending, unbound, denied):
        events = await _blocked_events(conn, seeded.message_id)
        assert [e["gate"] for e in events] == ["approval"]
        assert events[0]["reason"] == "missing_approval"
        message = await repo.get_message(conn, seeded.message_id)
        assert message is not None and message.send_state != SendState.SENT


@pytest.mark.protected
async def test_approval_for_different_content_does_not_authorize_this_message(
    conn: asyncpg.Connection,
):
    cfg = _cfg()
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg)
    # The approved row stays granted; the message body is changed after approval.
    await conn.execute(
        "UPDATE messages SET body_text = body_text || ' P.S. new claim' WHERE id = $1",
        seeded.message_id,
    )

    await _attempt(conn, seeded, cfg=cfg, transport=transport)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert events[0]["reason"] == "approval_mismatch"


@pytest.mark.protected
async def test_sending_to_a_suppressed_address_is_refused(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg)
    await repo.insert_suppression(
        conn,
        address=seeded.to_address,
        domain=seeded.domain,
        reason=SuppressionReason.UNSUBSCRIBE,
        source="test",
    )

    await _attempt(conn, seeded, cfg=cfg, transport=transport)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert (events[0]["gate"], events[0]["reason"]) == ("suppression", "suppressed_contact")
    message = await repo.get_message(conn, seeded.message_id)
    assert message is not None and message.send_state == SendState.BLOCKED


@pytest.mark.protected
async def test_unsuppressed_address_at_a_suppressed_domain_is_refused(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg)
    await repo.insert_suppression(
        conn, address=None, domain=seeded.domain, reason=SuppressionReason.MANUAL, source="test"
    )
    # This exact address has no suppression row of its own.
    assert not await conn.fetchval(
        "SELECT count(*) FROM suppressions WHERE address = $1", seeded.to_address
    )

    await _attempt(conn, seeded, cfg=cfg, transport=transport)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert (events[0]["gate"], events[0]["reason"]) == ("suppression", "suppressed_domain")


@pytest.mark.protected
async def test_sending_when_the_daily_cap_is_already_met_is_refused(conn: asyncpg.Connection):
    cfg = _cfg(daily_cap=5)
    transport = StubTransport()
    for hours_ago in (23, 20, 15, 10, 5):
        await _insert_sent(conn, cfg, started_at=NOW - timedelta(hours=hours_ago))
    seeded = await _seed(conn, cfg=cfg)

    await _attempt(conn, seeded, cfg=cfg, transport=transport)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert (events[0]["gate"], events[0]["reason"]) == ("cap", "daily_cap_reached")
    assert events[0]["disposition"] == "deferred"
    # Deferred, not dropped: the message is still drafted and a later send job exists.
    message = await repo.get_message(conn, seeded.message_id)
    assert message is not None and message.send_state == SendState.DRAFTED
    requeued = await conn.fetch(
        "SELECT run_after FROM jobs WHERE type = $1 AND payload->>'message_id' = $2",
        sending.SEND_JOB_TYPE,
        str(seeded.message_id),
    )
    assert len(requeued) == 1 and requeued[0]["run_after"] > NOW


@pytest.mark.protected
async def test_sending_outside_the_send_window_is_refused(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg, country="US")
    saturday_noon_new_york = NOW + timedelta(days=3)  # Wednesday -> Saturday

    await _attempt(conn, seeded, cfg=cfg, transport=transport, now=saturday_noon_new_york)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert (events[0]["gate"], events[0]["reason"]) == ("window", "outside_send_window")
    assert events[0]["deferred_until"] is not None


@pytest.mark.protected
async def test_a_from_address_on_the_wrong_domain_is_refused(conn: asyncpg.Connection):
    good = _cfg()
    transport = StubTransport(address="kimani@kimanimburu.com")
    seeded = await _seed(conn, cfg=good)
    wrong = _cfg(from_address="kimani@kimanimburu.com")  # the brand domain

    await _attempt(conn, seeded, cfg=wrong, transport=transport)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert (events[0]["gate"], events[0]["reason"]) == ("from_domain", "wrong_from_domain")


@pytest.mark.protected
async def test_suppression_added_between_draft_and_send_is_caught_at_send_time(
    conn: asyncpg.Connection,
):
    cfg = _cfg()
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg)
    at_draft_time = await sending.can_send(
        conn,
        message_id=seeded.message_id,
        lead_id=seeded.lead_id,
        to_address=seeded.to_address,
        correlation_id=uuid.uuid4(),
        now=NOW,
        config=cfg,
        environ=PRODUCTION,
    )
    assert at_draft_time.allowed  # nothing suppressed yet

    await repo.insert_suppression(
        conn,
        address=seeded.to_address,
        domain=seeded.domain,
        reason=SuppressionReason.HOSTILE_REPLY,
        source="test",
    )
    await _attempt(conn, seeded, cfg=cfg, transport=transport)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert events[0]["reason"] == "suppressed_contact"


@pytest.mark.protected
async def test_health_threshold_breach_pauses_sending_entirely(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport()
    first = await _seed(conn, cfg=cfg)
    second = await _seed(conn, cfg=cfg)
    for n in range(2):  # 2 hard bounces below the sample floor pauses (config)
        await repo.insert_suppression(
            conn,
            address=f"bounced{n}@elsewhere.example",
            domain="elsewhere.example",
            reason=SuppressionReason.HARD_BOUNCE,
            source="test",
        )

    await _attempt(conn, first, cfg=cfg, transport=transport)
    await _attempt(conn, second, cfg=cfg, transport=transport)

    assert transport.sent == []
    for seeded in (first, second):
        events = await _blocked_events(conn, seeded.message_id)
        assert (events[0]["gate"], events[0]["reason"]) == ("health", "sending_paused")
        assert events[0]["disposition"] == "held"
    pause = await repo.get_open_sending_pause(conn, sending_domain=cfg.sending_domain)
    assert pause is not None
    paused_events = await conn.fetchval("SELECT count(*) FROM events WHERE type = 'sending.paused'")
    assert paused_events == 1  # one alert per pause, not one per refused send
    # Stops entirely, does not throttle: no send job was re-enqueued.
    assert await conn.fetchval("SELECT count(*) FROM jobs") == 0

    # Resuming requires a human and a reason, and re-enqueues the held drafts.
    with pytest.raises(ValueError):
        await sending.resume_sending(conn, resumed_by="human:test", reason="  ", config=cfg)
    result = await sending.resume_sending(
        conn, resumed_by="human:test", reason="bounced rows removed from list", config=cfg
    )
    assert result.pause_id == pause.id
    assert set(result.requeued_message_ids) == {first.message_id, second.message_id}


# ===========================================================================
# Standard
# ===========================================================================


async def test_a_refusal_records_which_gate_fired_and_why(conn: asyncpg.Connection):
    cfg = _cfg(allowed_email_statuses=frozenset({EmailStatus.VALID}))
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg, email_status=EmailStatus.UNVERIFIED)

    await _attempt(conn, seeded, cfg=cfg, transport=transport)

    events = await _blocked_events(conn, seeded.message_id)
    assert len(events) == 1
    event = events[0]
    assert event["gate"] == "suppression"
    assert event["reason"] == "email_status_not_allowed"
    assert "unverified" in event["detail"]
    message = await repo.get_message(conn, seeded.message_id)
    assert message is not None and message.send_block_reason is not None
    assert message.send_block_reason.startswith("email_status_not_allowed")


async def test_caps_count_follow_ups_not_just_first_touch(conn: asyncpg.Connection):
    cfg = _cfg(daily_cap=1)
    transport = StubTransport()
    await _insert_sent(conn, cfg, started_at=NOW - timedelta(hours=3), step=2)  # a follow-up
    seeded = await _seed(conn, cfg=cfg)

    await _attempt(conn, seeded, cfg=cfg, transport=transport)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert events[0]["reason"] == "daily_cap_reached"


async def test_hourly_cap_and_min_gap_defer(conn: asyncpg.Connection):
    cfg = _cfg(hourly_cap=1)
    transport = StubTransport()
    await _insert_sent(conn, cfg, started_at=NOW - timedelta(minutes=30))
    hourly = await _seed(conn, cfg=cfg)
    await _attempt(conn, hourly, cfg=cfg, transport=transport)
    assert (await _blocked_events(conn, hourly.message_id))[0]["reason"] == "hourly_cap_reached"

    gap_cfg = _cfg(hourly_cap=100, min_gap_seconds=3600)
    gap = await _seed(conn, cfg=gap_cfg)
    await _attempt(conn, gap, cfg=gap_cfg, transport=transport)
    assert (await _blocked_events(conn, gap.message_id))[0]["reason"] == "min_gap_not_elapsed"
    assert transport.sent == []


async def test_all_gates_pass_sends_once_and_records_the_provider_id(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg)

    await _attempt(conn, seeded, cfg=cfg, transport=transport)
    await _attempt(conn, seeded, cfg=cfg, transport=transport)  # redelivered job

    assert len(transport.sent) == 1
    message = await repo.get_message(conn, seeded.message_id)
    assert message is not None and message.send_state == SendState.SENT
    assert message.provider_message_id and message.thread_id and message.sent_at
    sent_events = await conn.fetch("SELECT payload FROM events WHERE type = 'outreach.sent'")
    assert len(sent_events) == 1
    assert json.loads(sent_events[0]["payload"])["dev_sandbox_redirect"] is False


async def test_dev_sandbox_redirects_and_unset_sandbox_refuses(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg)
    await _attempt(conn, seeded, cfg=cfg, transport=transport, environ={"ENV": "development"})
    events = await _blocked_events(conn, seeded.message_id)
    assert events[0]["reason"] == "dev_sandbox" and events[0]["disposition"] == "held"
    assert transport.sent == []

    await _attempt(
        conn,
        seeded,
        cfg=cfg,
        transport=transport,
        environ={"ENV": "development", "DEV_SANDBOX_EMAIL": "sandbox@kimanimburu.example"},
    )
    assert len(transport.sent) == 1
    import base64
    import email

    delivered = email.message_from_bytes(base64.urlsafe_b64decode(transport.sent[0]))
    assert delivered["To"] == "sandbox@kimanimburu.example"
    assert delivered["To"] != seeded.to_address


async def test_unknown_transport_outcome_is_never_resent(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport(fail_with=TimeoutError("read timeout"))
    seeded = await _seed(conn, cfg=cfg)

    await _attempt(conn, seeded, cfg=cfg, transport=transport)
    message = await repo.get_message(conn, seeded.message_id)
    assert message is not None and message.send_state == SendState.SEND_UNKNOWN

    transport.fail_with = None
    await _attempt(conn, seeded, cfg=cfg, transport=transport)
    assert transport.sent == []  # a human reconciles; the system does not guess


async def test_definitive_gmail_rejection_is_send_failed(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport(fail_with=GmailSendRejectedError(400, "invalid recipient"))
    seeded = await _seed(conn, cfg=cfg)
    await _attempt(conn, seeded, cfg=cfg, transport=transport)
    message = await repo.get_message(conn, seeded.message_id)
    assert message is not None and message.send_state == SendState.SEND_FAILED


async def test_authenticated_account_must_be_the_from_address(conn: asyncpg.Connection):
    cfg = _cfg()
    transport = StubTransport(address="kimanimburu098@gmail.com")
    seeded = await _seed(conn, cfg=cfg)
    await _attempt(conn, seeded, cfg=cfg, transport=transport)
    assert transport.sent == []
    message = await repo.get_message(conn, seeded.message_id)
    assert message is not None and message.send_state == SendState.SEND_FAILED


async def test_concurrent_sends_cannot_exceed_the_cap(conn: asyncpg.Connection, database_url: str):
    cfg = _cfg(daily_cap=1, hourly_cap=10, min_gap_seconds=0)
    transport = StubTransport()
    a = await _seed(conn, cfg=cfg)
    b = await _seed(conn, cfg=cfg)
    other = await asyncpg.connect(database_url)
    try:
        results = await asyncio.gather(
            sending.authorize_send(
                conn,
                message_id=a.message_id,
                lead_id=a.lead_id,
                to_address=a.to_address,
                correlation_id=uuid.uuid4(),
                now=NOW,
                config=cfg,
                environ=PRODUCTION,
            ),
            sending.authorize_send(
                other,
                message_id=b.message_id,
                lead_id=b.lead_id,
                to_address=b.to_address,
                correlation_id=uuid.uuid4(),
                now=NOW,
                config=cfg,
                environ=PRODUCTION,
            ),
        )
    finally:
        await other.close()
    authorized = [auth for _, auth in results if auth is not None]
    assert len(authorized) == 1
    assert transport.sent == []


async def test_a_gate_that_cannot_be_evaluated_means_no_send(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    cfg = _cfg()
    transport = StubTransport()
    seeded = await _seed(conn, cfg=cfg)

    async def broken(*args: Any, **kwargs: Any) -> int:
        raise asyncpg.exceptions.UndefinedTableError('relation "messages" does not exist')

    monkeypatch.setattr(repo, "count_sends_since", broken)
    await _attempt(conn, seeded, cfg=cfg, transport=transport)

    assert transport.sent == []
    events = await _blocked_events(conn, seeded.message_id)
    assert (events[0]["gate"], events[0]["reason"]) == ("evaluation", "gate_evaluation_error")
    message = await repo.get_message(conn, seeded.message_id)
    assert message is not None and message.send_state == SendState.DRAFTED


async def test_suppressions_cannot_be_deleted_or_edited(conn: asyncpg.Connection):
    row = await repo.insert_suppression(
        conn,
        address="x@y.example",
        domain="y.example",
        reason=SuppressionReason.UNSUBSCRIBE,
        source="test",
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="append-only"):
        await conn.execute("DELETE FROM suppressions WHERE id = $1", row.id)
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="append-only"):
        await conn.execute("UPDATE suppressions SET reason = 'manual' WHERE id = $1", row.id)


async def test_a_sent_message_can_never_return_to_drafted(conn: asyncpg.Connection):
    cfg = _cfg()
    seeded = await _seed(conn, cfg=cfg)
    await _attempt(conn, seeded, cfg=cfg, transport=StubTransport())
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="not allowed"):
        await conn.execute(
            "UPDATE messages SET send_state = 'drafted' WHERE id = $1", seeded.message_id
        )
