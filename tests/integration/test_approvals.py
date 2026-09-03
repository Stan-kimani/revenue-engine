"""Integration tests for core/approvals.py against a real Postgres instance
(TEST_DATABASE_URL, never DATABASE_URL — tests/_db_safety.py).

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from revenue_engine.core import approvals
from revenue_engine.core.errors import ApprovalAlreadyDecidedError, ApprovalNotFoundError
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import ActionType, ApprovalStatus, AutonomyLevel

pytestmark = pytest.mark.integration


@pytest.fixture
async def conn(database_url: str):
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute("TRUNCATE approvals, events RESTART IDENTITY CASCADE")
        await connection.close()


# ---------------------------------------------------------------------------
# PROTECTED: nothing executes without a committed, granted approval row.
# ---------------------------------------------------------------------------


@pytest.mark.protected
async def test_action_requiring_approval_cannot_execute_without_a_committed_approved_row(
    conn: asyncpg.Connection,
):
    """Simulates the send-path check directly (M1.4's real send path doesn't
    exist yet, but the gate it will call already does): construct an
    approval_id in every state OTHER than granted and prove is_approved()
    refuses each one, bypassing request_approval()/resolve() entirely for
    the 'nonexistent id' case to rule out any reliance on having gone
    through the normal creation path first."""
    # Never existed at all.
    assert await approvals.is_approved(conn, uuid.uuid4()) is False

    # Exists, still pending (a real request that hasn't been decided).
    pending = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "draft"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    assert await approvals.is_approved(conn, pending.id) is False

    # Exists, explicitly denied.
    denied = await approvals.resolve(
        conn, pending.id, decision=ApprovalStatus.DENIED, decided_by="human:stan"
    )
    assert denied.status == ApprovalStatus.DENIED
    assert await approvals.is_approved(conn, pending.id) is False

    # Exists, expired (system-driven, never decided by a human).
    expired_source = await approvals.request_approval(
        conn,
        action_type=ActionType.OUTREACH_DRAFT,
        payload={"body": "draft2"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    updated = await repo.expire_cancel_approval(conn, expired_source.id, reason="test")
    assert updated is not None and updated.status == ApprovalStatus.EXPIRED
    assert await approvals.is_approved(conn, expired_source.id) is False

    # Only a real grant makes is_approved() true.
    grant_source = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "draft3"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    await approvals.resolve(
        conn, grant_source.id, decision=ApprovalStatus.GRANTED, decided_by="human:stan"
    )
    assert await approvals.is_approved(conn, grant_source.id) is True


@pytest.mark.protected
async def test_resolved_approval_cannot_be_redecided_rejected_by_the_database(
    conn: asyncpg.Connection,
):
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "draft"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    await approvals.resolve(
        conn, approval.id, decision=ApprovalStatus.GRANTED, decided_by="human:stan"
    )

    # Through the normal application path: a clean, typed refusal.
    with pytest.raises(ApprovalAlreadyDecidedError):
        await approvals.resolve(
            conn, approval.id, decision=ApprovalStatus.DENIED, decided_by="human:evil"
        )

    # Bypassing application code entirely — a raw UPDATE straight against the
    # table — is what "rejected by the database" means literally. This must
    # fail even though it never goes near core/approvals.py or
    # db/repositories.py's own WHERE-status='pending' guard.
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="already decided"):
        await conn.execute("UPDATE approvals SET decided_by = 'hacker' WHERE id = $1", approval.id)

    row = await repo.get_approval(conn, approval.id)
    assert row is not None
    assert row.status == ApprovalStatus.GRANTED
    assert row.decided_by == "human:stan"  # untouched by either redecision attempt


@pytest.mark.protected
async def test_expired_approval_is_not_executable_even_if_approved_afterwards(
    conn: asyncpg.Connection,
):
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.OUTREACH_DRAFT,
        payload={"body": "draft"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    expired = await repo.expire_cancel_approval(conn, approval.id, reason="ttl_expired")
    assert expired is not None and expired.status == ApprovalStatus.EXPIRED

    with pytest.raises(ApprovalAlreadyDecidedError):
        await approvals.resolve(
            conn, approval.id, decision=ApprovalStatus.GRANTED, decided_by="human:stan"
        )

    assert await approvals.is_approved(conn, approval.id) is False
    row = await repo.get_approval(conn, approval.id)
    assert row is not None and row.status == ApprovalStatus.EXPIRED


# ---------------------------------------------------------------------------
# Standard
# ---------------------------------------------------------------------------


async def test_two_pending_requests_for_the_same_logical_action_are_prevented(
    conn: asyncpg.Connection,
):
    first = await approvals.request_approval(
        conn,
        action_type=ActionType.OUTREACH_DRAFT,
        payload={"body": "draft v1"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
        dedupe_key="outreach_draft:lead-123:step-0",
    )
    second = await approvals.request_approval(
        conn,
        action_type=ActionType.OUTREACH_DRAFT,
        payload={"body": "draft v2 (retried job, different content)"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
        dedupe_key="outreach_draft:lead-123:step-0",
    )

    assert first.id == second.id
    rows = await conn.fetch(
        "SELECT count(*) AS n FROM approvals WHERE dedupe_key = $1",
        "outreach_draft:lead-123:step-0",
    )
    assert rows[0]["n"] == 1


async def test_approval_granted_emitted_exactly_once_per_approval(conn: asyncpg.Connection):
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "draft"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    await approvals.resolve(
        conn, approval.id, decision=ApprovalStatus.GRANTED, decided_by="human:stan"
    )
    with pytest.raises(ApprovalAlreadyDecidedError):
        await approvals.resolve(
            conn, approval.id, decision=ApprovalStatus.GRANTED, decided_by="human:stan"
        )

    rows = await conn.fetch("SELECT payload FROM events WHERE type = 'approval.granted'")
    matching = [r for r in rows if json.loads(r["payload"])["approval_id"] == str(approval.id)]
    assert len(matching) == 1


async def test_expire_stale_moves_only_genuinely_stale_rows_and_emits_approval_expired(
    conn: asyncpg.Connection,
):
    now = datetime.now(UTC)
    stale_id = uuid.uuid4()
    fresh_id = uuid.uuid4()
    # outreach_draft: on_expiry=cancel, ttl_hours=72 (config/thresholds.yaml).
    await conn.execute(
        "INSERT INTO approvals (id, action_type, payload, status, token, created_at) "
        "VALUES ($1, 'outreach_draft', '{}'::jsonb, 'pending', 'tok-stale', $2)",
        stale_id,
        now - timedelta(hours=80),
    )
    await conn.execute(
        "INSERT INTO approvals (id, action_type, payload, status, token, created_at) "
        "VALUES ($1, 'outreach_draft', '{}'::jsonb, 'pending', 'tok-fresh', $2)",
        fresh_id,
        now - timedelta(hours=1),
    )

    result = await approvals.expire_stale(conn)

    cancelled_ids = {a.id for a in result.cancelled}
    assert stale_id in cancelled_ids
    assert fresh_id not in cancelled_ids

    stale_row = await repo.get_approval(conn, stale_id)
    fresh_row = await repo.get_approval(conn, fresh_id)
    assert stale_row is not None and stale_row.status == ApprovalStatus.EXPIRED
    assert fresh_row is not None and fresh_row.status == ApprovalStatus.PENDING

    events = await conn.fetch("SELECT payload FROM events WHERE type = 'approval.expired'")
    matching = [r for r in events if json.loads(r["payload"])["approval_id"] == str(stale_id)]
    assert len(matching) == 1
    assert json.loads(matching[0]["payload"])["action_taken"] == "cancelled"
    fresh_events = [r for r in events if json.loads(r["payload"])["approval_id"] == str(fresh_id)]
    assert fresh_events == []


async def test_expire_stale_escalate_branch_never_cancels_and_fires_once(conn: asyncpg.Connection):
    # proposal_send: on_expiry=escalate, ttl_hours=24 (config/thresholds.yaml).
    approval_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO approvals (id, action_type, payload, status, token, created_at) "
        "VALUES ($1, 'proposal_send', '{}'::jsonb, 'pending', 'tok', $2)",
        approval_id,
        datetime.now(UTC) - timedelta(hours=30),
    )

    first = await approvals.expire_stale(conn)
    second = await approvals.expire_stale(conn)  # a later scheduler tick, still stale

    assert approval_id in {a.id for a in first.escalated}
    assert approval_id in {a.id for a in second.escalated}  # still reported, still pending

    row = await repo.get_approval(conn, approval_id)
    assert row is not None and row.status == ApprovalStatus.PENDING  # never cancelled

    events = await conn.fetch("SELECT payload FROM events WHERE type = 'approval.expired'")
    matching = [r for r in events if json.loads(r["payload"])["approval_id"] == str(approval_id)]
    assert len(matching) == 1  # fires once, ever — not once per scheduler tick
    assert json.loads(matching[0]["payload"])["action_taken"] == "escalated"


async def test_record_delete_never_swept_no_ttl(conn: asyncpg.Connection):
    approval_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO approvals (id, action_type, payload, status, token, created_at) "
        "VALUES ($1, 'record_delete', '{}'::jsonb, 'pending', 'tok', $2)",
        approval_id,
        datetime.now(UTC) - timedelta(days=365),
    )

    result = await approvals.expire_stale(conn)

    all_touched = {a.id for a in result.cancelled} | {a.id for a in result.escalated}
    assert approval_id not in all_touched
    row = await repo.get_approval(conn, approval_id)
    assert row is not None and row.status == ApprovalStatus.PENDING


async def test_auto_grant_for_autonomy_level_that_does_not_require_approval(
    conn: asyncpg.Connection,
):
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.OUTREACH_DRAFT,
        payload={"body": "a follow-up inside an approved sequence"},
        autonomy_level=AutonomyLevel.A1,
        correlation_id=uuid.uuid4(),
    )

    assert approval.status == ApprovalStatus.GRANTED
    assert approval.decided_by == "system:auto"
    assert await approvals.is_approved(conn, approval.id) is True

    events = await conn.fetch("SELECT type, payload FROM events WHERE type LIKE 'approval.%'")
    types = [e["type"] for e in events]
    assert types == ["approval.granted"]  # no approval.requested for an auto-granted action


async def test_resolve_unknown_approval_id_raises_not_found(conn: asyncpg.Connection):
    with pytest.raises(ApprovalNotFoundError):
        await approvals.resolve(
            conn, uuid.uuid4(), decision=ApprovalStatus.GRANTED, decided_by="human:stan"
        )
