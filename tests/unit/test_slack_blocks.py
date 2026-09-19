"""Unit tests for integrations/slack.py's pure Block Kit rendering — no I/O,
no Slack SDK object involved (M1.3 plan deliverable 3: "shows the full
drafted content, not a summary — the human must see exactly what would
send")."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from revenue_engine.db.models import ActionType, Approval, ApprovalStatus
from revenue_engine.integrations.slack import (
    _APPROVE_ACTION_ID,
    _REJECT_ACTION_ID,
    build_approval_request_blocks,
    build_decided_blocks,
)


def _make_approval(**overrides: object) -> Approval:
    base: dict[str, object] = dict(
        id=uuid4(),
        action_type=ActionType.PROPOSAL_SEND,
        payload={"subject": "Following up", "body": "Here is the proposal: $12,000 for the build."},
        requested_by_agent="agent:sales",
        status=ApprovalStatus.PENDING,
        decided_by=None,
        token="tok-123",
        expires_at=datetime(2026, 1, 4, tzinfo=UTC),
        decided_at=None,
        decision_reason=None,
        correlation_id=uuid4(),
        causation_id=None,
        dedupe_key=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    base.update(overrides)
    return Approval(**base)  # type: ignore[arg-type]


def test_request_blocks_show_the_full_payload_verbatim():
    approval = _make_approval()

    _, blocks = build_approval_request_blocks(approval)

    rendered = json.dumps(blocks)
    assert "Here is the proposal: $12,000 for the build." in rendered
    assert "Following up" in rendered


def test_request_blocks_have_approve_and_reject_buttons_carrying_the_approval_id():
    approval = _make_approval()

    _, blocks = build_approval_request_blocks(approval)

    actions_block = next(b for b in blocks if b["type"] == "actions")
    action_ids = {el["action_id"]: el["value"] for el in actions_block["elements"]}
    assert action_ids == {
        _APPROVE_ACTION_ID: str(approval.id),
        _REJECT_ACTION_ID: str(approval.id),
    }


def test_request_blocks_never_summarise_or_truncate_normal_sized_content():
    """A defensive regression: build_approval_request_blocks must not ever
    shorten/ellipsize the payload for a normal-sized draft — only the
    explicit, clearly-labelled truncation path (extreme payloads) may."""
    long_body = "x" * 500  # well under the truncation threshold
    approval = _make_approval(payload={"body": long_body})

    _, blocks = build_approval_request_blocks(approval)

    rendered = json.dumps(blocks)
    assert long_body in rendered
    assert "truncated" not in rendered.lower()


def test_oversized_payload_is_flagged_not_silently_truncated():
    huge_body = "y" * 10_000
    approval = _make_approval(payload={"body": huge_body})

    _, blocks = build_approval_request_blocks(approval)

    rendered = json.dumps(blocks)
    assert "truncated" in rendered.lower()


def test_decided_blocks_show_who_and_when_for_a_grant():
    approval = _make_approval(
        status=ApprovalStatus.GRANTED,
        decided_by="human:stan",
        decided_at=datetime(2026, 1, 2, 15, 30, tzinfo=UTC),
    )

    text, blocks = build_decided_blocks(approval)

    assert "human:stan" in text
    rendered = json.dumps(blocks)
    assert "human:stan" in rendered
    assert "2026-01-02" in rendered
    assert "Approved" in rendered


def test_decided_blocks_show_denial_reason_when_given():
    approval = _make_approval(
        status=ApprovalStatus.DENIED,
        decided_by="human:stan",
        decided_at=datetime(2026, 1, 2, tzinfo=UTC),
        decision_reason="Price is too aggressive for this account.",
    )

    _, blocks = build_decided_blocks(approval)

    rendered = json.dumps(blocks)
    assert "Rejected" in rendered
    assert "Price is too aggressive for this account." in rendered


def test_sending_pause_alert_says_nothing_will_send_and_shows_why():
    from revenue_engine.integrations.slack import render_sending_alert

    text = render_sending_alert(
        "sending.paused",
        {
            "sending_domain": "getkimani.com",
            "pause_id": str(uuid4()),
            "reason": "hard_bounce count 2 >= 2 (below the 50-send sample floor)",
            "metrics": {"sends": 12, "hard_bounce": 2},
        },
    )
    assert "PAUSED" in text and "getkimani.com" in text
    assert "hard_bounce count 2 >= 2" in text
    assert "resume_sending" in text
