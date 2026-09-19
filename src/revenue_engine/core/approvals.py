"""The human-in-the-loop gate (CLAUDE.md §1 non-negotiable 8, build-spec §0.7,
agent-contracts.md §0.4). M1.3.

THE PROPERTY THAT MATTERS: `is_approved()` is the single check a send/
proposal/pricing/discount/delete path calls before executing. It reads
`approvals` and nothing else — no Slack, no cache, no in-memory flag. If
Slack is deleted, unreachable, or misconfigured, `request_approval()` still
commits a `pending` row (it never talks to Slack itself — notification is a
separate job, `integrations/slack.py::handle_notify_approval_request`,
triggered by the `approval.requested` event it emits) and `is_approved()`
still correctly returns False. A Slack outage can only ever fail the
notification job; it cannot make an unapproved action executable, and it
cannot make an approved one stop being approved.

Append-only is enforced twice: `resolve()`/`expire_stale()` use
`UPDATE ... WHERE status = 'pending'` (a second attempt is a clean no-op —
`ApprovalAlreadyDecidedError`, not a re-decision), and
migrations/0005's `approvals_forbid_redecision` trigger blocks ANY update to
an already-decided row at the database level — the backstop for a future
write path that forgets the guard.

Autonomy gating (agent-contracts.md §0.4) is config-driven
(`config/thresholds.yaml`'s `approvals.autonomy_requires_approval`, default
`[A2, A3]`), never a hardcoded `if autonomy_level == ...` in this file or any
call site: A0 has no external side effects to gate; A1's side effects are
autonomous within config caps. An action whose level does NOT require
approval still gets a real `Approval` row (auto-`granted`, `decided_by`
`"system:auto"`) — `request_approval()` always returns a real row a caller
can log or reference, and `is_approved()` never needs a special case for
"this action type doesn't need gating."
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from ..db import repositories as repo
from ..db.models import ActionType, Approval, ApprovalStatus, AutonomyLevel
from .config import ThresholdsConfig, get_config
from .errors import ApprovalAlreadyDecidedError, ApprovalNotFoundError
from .events import emit

ACTOR = "core.approvals"


# ============================================================================
# Pure helpers — no I/O, no `conn` (unit-testable without a database)
# ============================================================================


def requires_approval(autonomy_level: AutonomyLevel, thresholds: ThresholdsConfig) -> bool:
    """Whether `autonomy_level` creates a BLOCKING pending approval, per
    config (`thresholds.autonomy_requires_approval`) — never a hardcoded
    level comparison. See this module's docstring."""
    return autonomy_level in thresholds.autonomy_requires_approval


def compute_expires_at(
    action_type: ActionType, thresholds: ThresholdsConfig, *, now: datetime
) -> datetime | None:
    """event-catalog.md §7.1's per-action-type TTL. None means never expires
    (`record_delete`)."""
    policy = thresholds.expiry[action_type]
    if policy.ttl_hours is None:
        return None
    return now + timedelta(hours=policy.ttl_hours)


# ============================================================================
# The gate
# ============================================================================


async def request_approval(
    conn: asyncpg.Connection,
    *,
    action_type: ActionType,
    payload: dict[str, object],
    autonomy_level: AutonomyLevel,
    correlation_id: UUID,
    causation_id: UUID | None = None,
    requested_by_agent: str | None = None,
    dedupe_key: str | None = None,
    thresholds: ThresholdsConfig | None = None,
) -> Approval:
    """Always returns a real, persisted `Approval` row — never talks to
    Slack (see module docstring). `payload` should carry the FULL content a
    human must see to decide (e.g. the drafted email body), not a summary
    (`integrations/slack.py` renders exactly this, verbatim) — and, by
    convention, `payload["lead_id"]` when the action is lead-scoped, which
    this function reads back out for `approval.requested`'s optional
    `lead_id` field (event-catalog.md §3) rather than taking a second,
    separate parameter for it.

    If `autonomy_level` does not require approval (config-driven, see
    `requires_approval()`), the row is inserted already `granted`
    (`decided_by="system:auto"`) and `approval.granted` is emitted directly —
    `request_approval()` is safe to call unconditionally from any call site
    regardless of level; the config alone decides whether it actually gates.
    """
    resolved_thresholds = thresholds if thresholds is not None else get_config().thresholds
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)

    if not requires_approval(autonomy_level, resolved_thresholds):
        approval = await repo.insert_approval(
            conn,
            action_type=action_type,
            payload=payload,
            requested_by_agent=requested_by_agent,
            token=token,
            expires_at=None,
            correlation_id=correlation_id,
            causation_id=causation_id,
            dedupe_key=dedupe_key,
        )
        granted = await repo.resolve_approval(
            conn,
            approval.id,
            status=ApprovalStatus.GRANTED,
            decided_by="system:auto",
            decision_reason=f"autonomy level {autonomy_level.value} does not require approval",
        )
        assert granted is not None  # just inserted as pending; nothing else could have raced it
        await emit(
            conn,
            type="approval.granted",
            payload={
                "approval_id": str(granted.id),
                "decided_by": granted.decided_by,
                "token": token,
            },
            correlation_id=correlation_id,
            actor=ACTOR,
            idempotency_key=f"approval:{granted.id}:granted",
            causation_id=causation_id,
        )
        return granted

    expires_at = compute_expires_at(action_type, resolved_thresholds, now=now)
    approval = await repo.insert_approval(
        conn,
        action_type=action_type,
        payload=payload,
        requested_by_agent=requested_by_agent,
        token=token,
        expires_at=expires_at,
        correlation_id=correlation_id,
        causation_id=causation_id,
        dedupe_key=dedupe_key,
    )
    # On the dedupe path (insert_approval returned an existing pending row),
    # this emit is a no-op: the idempotency_key is per approval_id.
    lead_id = payload.get("lead_id")
    await emit(
        conn,
        type="approval.requested",
        payload={
            "approval_id": str(approval.id),
            "action_type": action_type.value,
            "lead_id": str(lead_id) if lead_id else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
        correlation_id=correlation_id,
        actor=ACTOR,
        idempotency_key=f"approval:{approval.id}:requested",
        causation_id=causation_id,
    )
    return approval


async def is_approved(conn: asyncpg.Connection, approval_id: UUID) -> bool:
    """THE single check a send/proposal/pricing/discount/delete path calls.
    Reads `approvals` and nothing else. An approval whose status ever left
    'pending' as anything other than 'granted' can never become approved
    later — migrations/0005's trigger makes that structurally impossible,
    not merely a convention this function happens to check for."""
    approval = await repo.get_approval(conn, approval_id)
    return approval is not None and approval.status == ApprovalStatus.GRANTED


async def resolve(
    conn: asyncpg.Connection,
    approval_id: UUID,
    *,
    decision: ApprovalStatus,
    decided_by: str,
    reason: str | None = None,
) -> Approval:
    """`decision` must be GRANTED or DENIED (a human decision) — PENDING/
    EXPIRED are not valid decisions and raise ValueError; expiry has its own
    function (`expire_stale`) since it's system-driven, not human-driven.

    Raises `ApprovalNotFoundError` if `approval_id` matches no row, or
    `ApprovalAlreadyDecidedError` if it exists but already left 'pending' —
    a resolved approval is never re-decided (checked here via
    `repo.resolve_approval`'s `WHERE status='pending'` guard; the database
    trigger is the backstop for any OTHER write path)."""
    if decision not in (ApprovalStatus.GRANTED, ApprovalStatus.DENIED):
        raise ValueError(f"resolve() decision must be GRANTED or DENIED, got {decision}")

    resolved = await repo.resolve_approval(
        conn, approval_id, status=decision, decided_by=decided_by, decision_reason=reason
    )
    if resolved is None:
        existing = await repo.get_approval(conn, approval_id)
        if existing is None:
            raise ApprovalNotFoundError(approval_id)
        raise ApprovalAlreadyDecidedError(approval_id, existing.status.value)

    event_type = "approval.granted" if decision == ApprovalStatus.GRANTED else "approval.denied"
    event_payload: dict[str, object] = {"approval_id": str(resolved.id), "decided_by": decided_by}
    if decision == ApprovalStatus.GRANTED:
        event_payload["token"] = resolved.token
    else:
        event_payload["reason"] = reason

    await emit(
        conn,
        type=event_type,
        payload=event_payload,
        correlation_id=resolved.correlation_id or resolved.id,
        actor=ACTOR,
        idempotency_key=f"approval:{resolved.id}:{decision.value}",
        causation_id=resolved.causation_id,
    )
    return resolved


# ============================================================================
# Expiry (event-catalog.md §7.1) — scheduled, see scripts/run_worker.py's
# run_expire_stale_loop.
# ============================================================================


@dataclass(frozen=True)
class ExpireStaleResult:
    cancelled: tuple[Approval, ...] = field(default_factory=tuple)
    """`on_expiry: cancel` — terminal, status moved to 'expired'."""
    escalated: tuple[Approval, ...] = field(default_factory=tuple)
    """`on_expiry: escalate` — never cancelled, status is UNCHANGED
    ('pending'); `approval.expired` fired once (action_taken='escalated') as
    a notification signal only. Still fully approvable/rejectable."""


async def expire_stale(
    conn: asyncpg.Connection, *, thresholds: ThresholdsConfig | None = None
) -> ExpireStaleResult:
    """Sweeps every `action_type` with a finite TTL for pending approvals
    past it. `record_delete` (ttl_hours=None) is never swept — never
    autonomous, at any confidence, under any config.

    Only genuinely stale rows are touched: the cutoff query
    (`repo.get_pending_approvals_older_than`) is scoped per action_type to
    that type's own configured TTL: an `outreach_draft` approval 10 hours old
    is untouched (72h TTL), one 80 hours old is cancelled, and a
    `proposal_send` approval 30 hours old is escalated but stays pending (24h
    TTL, on_expiry=escalate).
    """
    resolved_thresholds = thresholds if thresholds is not None else get_config().thresholds
    now = datetime.now(UTC)

    cancelled: list[Approval] = []
    escalated: list[Approval] = []

    for action_type, policy in resolved_thresholds.expiry.items():
        if policy.ttl_hours is None:
            continue
        cutoff = now - timedelta(hours=policy.ttl_hours)
        stale = await repo.get_pending_approvals_older_than(
            conn, action_type=action_type, cutoff=cutoff
        )
        for approval in stale:
            if policy.on_expiry == "cancel":
                updated = await repo.expire_cancel_approval(conn, approval.id, reason="ttl_expired")
                if updated is None:
                    continue  # lost a race against a concurrent resolve() — not stale anymore
                cancelled.append(updated)
                await emit(
                    conn,
                    type="approval.expired",
                    payload={
                        "approval_id": str(updated.id),
                        "action_type": action_type.value,
                        "action_taken": "cancelled",
                    },
                    correlation_id=updated.correlation_id or updated.id,
                    actor=ACTOR,
                    idempotency_key=f"approval:{updated.id}:expired:cancelled",
                    causation_id=updated.causation_id,
                )
            else:  # "escalate" — never cancels; status stays pending.
                escalated.append(approval)
                # No attempt counter in the idempotency_key: this fires
                # exactly ONCE per approval, ever, no matter how many
                # scheduler ticks find it still pending past its TTL — the
                # daily digest (orchestrator/schedules.py, not built) is the
                # recurring safety net, not a repeated escalation event
                # (event-catalog.md §7.1: "A single missed Slack ping is
                # invisible; a daily... is not").
                await emit(
                    conn,
                    type="approval.expired",
                    payload={
                        "approval_id": str(approval.id),
                        "action_type": action_type.value,
                        "action_taken": "escalated",
                    },
                    correlation_id=approval.correlation_id or approval.id,
                    actor=ACTOR,
                    idempotency_key=f"approval:{approval.id}:expired:escalated",
                    causation_id=approval.causation_id,
                )

    return ExpireStaleResult(cancelled=tuple(cancelled), escalated=tuple(escalated))
