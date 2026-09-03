"""Integration tests for integrations/slack.py against a real Postgres
instance (TEST_DATABASE_URL, never DATABASE_URL — tests/_db_safety.py).

The Slack client is always a stub here — never a real API call (M1.3 plan:
"Stub the Slack client in tests. Do not hit the real API.").

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from revenue_engine.core import approvals
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import ActionType, ApprovalStatus, AutonomyLevel, Job, JobStatus
from revenue_engine.integrations import slack

pytestmark = pytest.mark.integration


@pytest.fixture
async def conn(database_url: str):
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute("TRUNCATE approvals, events RESTART IDENTITY CASCADE")
        await connection.close()


def _make_job(*, approval_id: UUID) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid.uuid4(),
        type="slack.notify_approval_request",
        payload={"approval_id": str(approval_id)},
        status=JobStatus.RUNNING,
        run_after=now,
        attempts=0,
        locked_by="test",
        locked_at=now,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


class _RecordingClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    async def chat_postMessage(
        self, *, channel: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> Any:
        self.posted.append({"channel": channel, "text": text, "blocks": blocks})
        return {"ok": True, "channel": channel, "ts": "1700000000.000100"}

    async def chat_update(
        self, *, channel: str, ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> Any:
        self.updated.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ok": True}


class _AlwaysRaisingClient:
    """Simulates Slack being entirely unavailable — deleted app, revoked
    token, network down, whatever. Every call raises."""

    async def chat_postMessage(self, **kwargs: Any) -> Any:
        raise ConnectionError("could not reach slack.com")

    async def chat_update(self, **kwargs: Any) -> Any:
        raise ConnectionError("could not reach slack.com")


def _interaction_payload(
    *, action_id: str, approval_id: UUID, user: str = "stan"
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": "U123", "username": user},
        "channel": {"id": "C_APPROVALS"},
        "message": {"ts": "1700000000.000100"},
        "actions": [{"action_id": action_id, "value": str(approval_id)}],
    }


# ---------------------------------------------------------------------------
# PROTECTED: Slack unavailable -> approvals stay pending, nothing executable.
# The property under test is that nothing becomes executable — NOT that the
# notification job failed (dead-lettering is expected and uninteresting;
# it's the approval row's state that must be proven unaffected).
# ---------------------------------------------------------------------------


@pytest.mark.protected
async def test_slack_entirely_unavailable_leaves_approval_pending_and_unexecutable(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SLACK_APPROVAL_CHANNEL", "C_APPROVALS")
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "a real drafted proposal"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    assert approval.status == ApprovalStatus.PENDING

    job = _make_job(approval_id=approval.id)
    raising_client = _AlwaysRaisingClient()

    with pytest.raises(ConnectionError):
        await slack.handle_notify_approval_request(conn, job, client=raising_client)

    # The property that matters: the approval itself, and the gate that
    # reads it, are completely unaffected by the notification failure.
    row = await repo.get_approval(conn, approval.id)
    assert row is not None
    assert row.status == ApprovalStatus.PENDING
    assert await approvals.is_approved(conn, approval.id) is False


@pytest.mark.protected
async def test_slack_misconfigured_channel_also_leaves_approval_pending_and_unexecutable(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    """'Down, unreachable, or misconfigured' (M1.3 plan deliverable 3) — a
    missing SLACK_APPROVAL_CHANNEL is a real, likely misconfiguration, not
    just a hypothetical client-raises scenario. Same property, different
    cause."""
    monkeypatch.delenv("SLACK_APPROVAL_CHANNEL", raising=False)
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "draft"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )

    with pytest.raises(RuntimeError, match="SLACK_APPROVAL_CHANNEL"):
        await slack.handle_notify_approval_request(
            conn, _make_job(approval_id=approval.id), client=_RecordingClient()
        )

    row = await repo.get_approval(conn, approval.id)
    assert row is not None and row.status == ApprovalStatus.PENDING
    assert await approvals.is_approved(conn, approval.id) is False


@pytest.mark.protected
async def test_slack_unavailable_during_interaction_does_not_block_a_working_resolve(
    conn: asyncpg.Connection,
):
    """A Slack failure while trying to UPDATE the message after a decision
    must not roll back or block the decision itself — core/approvals.py's
    resolve() already committed before integrations/slack.py touches Slack
    at all."""
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "draft"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    payload = _interaction_payload(action_id=slack._APPROVE_ACTION_ID, approval_id=approval.id)

    # chat_update raising must not be allowed to look like the decision
    # itself failed — handle_interaction_payload lets this propagate (a
    # legitimate job/handler failure to log), but the decision is already
    # durable regardless of what happens next.
    with pytest.raises(ConnectionError):
        await slack.handle_interaction_payload(conn, payload, client=_AlwaysRaisingClient())

    row = await repo.get_approval(conn, approval.id)
    assert row is not None
    assert row.status == ApprovalStatus.GRANTED
    assert await approvals.is_approved(conn, approval.id) is True


# ---------------------------------------------------------------------------
# Standard
# ---------------------------------------------------------------------------


async def test_notify_approval_request_posts_full_content_to_the_configured_channel(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SLACK_APPROVAL_CHANNEL", "C_APPROVALS")
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "the exact drafted content"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    client = _RecordingClient()

    await slack.handle_notify_approval_request(
        conn, _make_job(approval_id=approval.id), client=client
    )

    assert len(client.posted) == 1
    assert client.posted[0]["channel"] == "C_APPROVALS"
    import json as _json

    assert "the exact drafted content" in _json.dumps(client.posted[0]["blocks"])


async def test_notify_skips_posting_for_an_already_decided_approval(conn: asyncpg.Connection):
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
    client = _RecordingClient()

    await slack.handle_notify_approval_request(
        conn, _make_job(approval_id=approval.id), client=client
    )

    assert client.posted == []  # nothing to decide anymore — would be misleading to post


async def test_full_path_request_to_slack_post_to_button_click_to_granted(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
):
    """request_approval() -> Slack post (stubbed) -> button callback ->
    resolve() -> approval.granted, exercised end to end through the real
    module functions (not simulated)."""
    monkeypatch.setenv("SLACK_APPROVAL_CHANNEL", "C_APPROVALS")
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "the real drafted proposal text"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )

    post_client = _RecordingClient()
    await slack.handle_notify_approval_request(
        conn, _make_job(approval_id=approval.id), client=post_client
    )
    assert len(post_client.posted) == 1  # the human sees the request

    interaction = _interaction_payload(
        action_id=slack._APPROVE_ACTION_ID, approval_id=approval.id, user="stan"
    )
    update_client = _RecordingClient()
    await slack.handle_interaction_payload(conn, interaction, client=update_client)

    # The message was updated to show the decision.
    assert len(update_client.updated) == 1
    assert "human:stan" in update_client.updated[0]["text"] or "human:stan" in str(
        update_client.updated[0]["blocks"]
    )

    # The gate itself reflects the grant.
    assert await approvals.is_approved(conn, approval.id) is True

    events = await conn.fetch("SELECT type, payload FROM events WHERE type = 'approval.granted'")
    import json as _json

    matching = [e for e in events if _json.loads(e["payload"])["approval_id"] == str(approval.id)]
    assert len(matching) == 1
    assert _json.loads(matching[0]["payload"])["decided_by"] == "human:stan"


async def test_reject_button_denies_and_updates_message(conn: asyncpg.Connection):
    approval = await approvals.request_approval(
        conn,
        action_type=ActionType.PROPOSAL_SEND,
        payload={"body": "draft"},
        autonomy_level=AutonomyLevel.A2,
        correlation_id=uuid.uuid4(),
    )
    interaction = _interaction_payload(
        action_id=slack._REJECT_ACTION_ID, approval_id=approval.id, user="jordan"
    )
    client = _RecordingClient()

    await slack.handle_interaction_payload(conn, interaction, client=client)

    row = await repo.get_approval(conn, approval.id)
    assert row is not None and row.status == ApprovalStatus.DENIED
    assert row.decided_by == "human:jordan"
    assert await approvals.is_approved(conn, approval.id) is False


async def test_double_click_on_a_decided_approval_updates_message_without_raising(
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

    interaction = _interaction_payload(
        action_id=slack._REJECT_ACTION_ID, approval_id=approval.id, user="late"
    )
    client = _RecordingClient()

    await slack.handle_interaction_payload(conn, interaction, client=client)  # must not raise

    assert len(client.updated) == 1
    assert "already decided" in client.updated[0]["text"]
    row = await repo.get_approval(conn, approval.id)
    assert row is not None and row.status == ApprovalStatus.GRANTED  # unchanged
