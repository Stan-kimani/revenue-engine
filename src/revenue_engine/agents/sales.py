"""Sales agent (agent-contracts.md §3) — M1.4a: outreach drafting and the send path
ONLY. Reply polling, reply classification, the sequence state machine and
calendar booking are M1.4b.

Three job handlers, registered in scripts/run_worker.py's HANDLERS:
  sales.draft_outreach        <- lead.qualified.* (acts only on pack outreach.draft_bands)
  sales.resume_gated_action   <- approval.granted (acts only on action_type outreach_draft)
  sales.send_outreach         <- enqueued by the handler above, and by deferrals

Drafting never produces anything sendable on its own. A draft becomes a
messages row (send_state='drafted') plus an approval request bound to that exact
row, committed together. Sending happens only from an approval.granted, and
every send goes through core/sending.py::authorize_send — which re-checks the
approval, suppression, caps, window and health against live state — before
integrations/gmail.py is called.

This module never imports another agent (CLAUDE.md §1.5).
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any
from uuid import UUID

import asyncpg

from ..core import approvals, sending
from ..core import queue as core_queue
from ..core.config import DeliverabilityConfig, IndustryPack, get_config, load_config
from ..core.errors import (
    GmailSenderMismatchError,
    GmailSendRejectedError,
    RevenueEngineError,
    SendNotAuthorizedError,
)
from ..core.events import emit
from ..core.llm import AnthropicClientProtocol, complete_json
from ..core.observability import TraceContext
from ..db import repositories as repo
from ..db.models import (
    ActionType,
    AgentRunStatus,
    AutonomyLevel,
    EmailStatus,
    Job,
    SendState,
)
from ..integrations import gmail

ACTOR = "agent:sales"
FIRST_TOUCH_STEP = 0


@cache
def _pinned_pack(pack_name: str) -> IndustryPack:
    """The lead's pinned pack (entity-model.md §3.4), never get_config()'s
    default. Same helper as agents/qualification.py; duplicated because agents
    never import each other."""
    return load_config(industry_pack=pack_name).pack


def _jitter(cfg: DeliverabilityConfig, rng: random.Random | None) -> timedelta:
    source = rng if rng is not None else random
    return timedelta(seconds=source.uniform(0, cfg.jitter_seconds))


# ============================================================================
# Draft
# ============================================================================


async def handle_draft_outreach(
    conn: asyncpg.Connection,
    job: Job,
    *,
    client: AnthropicClientProtocol | None = None,
    config: DeliverabilityConfig | None = None,
) -> None:
    lead_id = UUID(job.payload["lead_id"])
    band = str(job.payload["band"])
    correlation_id = UUID(job.payload["correlation_id"])
    causation_id = UUID(job.payload["source_event_id"])
    cfg = config if config is not None else get_config().deliverability

    lead = await repo.get_lead(conn, lead_id)
    if lead is None:
        raise RevenueEngineError(f"sales.draft_outreach: lead not found: {lead_id}")
    pack = _pinned_pack(lead.industry_pack)
    if band not in pack.outreach_draft_bands:
        return  # e.g. mql while the pack's outreach.draft_bands is [sql]

    existing = await repo.get_outbound_message_for_lead_step(
        conn, lead_id=lead_id, sequence_step=FIRST_TOUCH_STEP
    )
    if existing is not None:
        return  # redelivered event: already drafted (or sent)

    contact = await repo.get_contact(conn, lead.contact_id)
    if contact is None:
        raise RevenueEngineError(f"sales.draft_outreach: contact not found: {lead.contact_id}")

    profile: dict[str, Any] = lead.profile or {}
    to_address = contact.email.lower()
    skip = await _draft_precondition_failure(
        conn, profile=profile, to_address=to_address, email_status=contact.email_status, cfg=cfg
    )
    if skip is not None:
        reason, detail = skip
        await emit(
            conn,
            type="outreach.blocked",
            payload={
                "lead_id": str(lead_id),
                "draft_id": None,
                "reason": reason,
                "gate": "draft_precondition",
                "detail": detail,
                "disposition": "skipped",
                "deferred_until": None,
            },
            correlation_id=correlation_id,
            actor=ACTOR,
            idempotency_key=f"lead:{lead_id}:draft_skipped:{reason}",
            causation_id=causation_id,
        )
        return

    trace = TraceContext(trace_id=str(job.id), correlation_id=correlation_id)
    brief = await complete_json(
        "sales/research_account.md",
        {
            "prospect_profile": profile,
            # Semantic memory is not built yet: nothing retrieved is the honest input.
            "similar_wins": [],
            "campaign_angle": None,
            "voice_rules": pack.voice.as_prompt_text(),
        },
        "outputs/account_brief.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor=ACTOR,
        causation_id=causation_id,
        client=client,
    )
    draft = await complete_json(
        "sales/draft_initial_outreach.md",
        {
            "account_brief": brief,
            "prospect_profile": profile,
            "sender_persona": pack.voice.sender_persona,
            "voice_rules": pack.voice.as_prompt_text(),
            "constraints": dict(pack.commercial_boundaries),
        },
        "outputs/outreach_draft.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor=ACTOR,
        causation_id=causation_id,
        client=client,
    )
    run = await repo.get_latest_agent_run(
        conn,
        agent=ACTOR,
        prompt_id="sales/draft_initial_outreach",
        trigger_event=causation_id,
        status=AgentRunStatus.SUCCESS,
    )
    if run is None:
        raise RevenueEngineError(
            "sales.draft_outreach: complete_json() succeeded but its agent_runs row could "
            "not be read back"
        )

    subject = str(draft["subject"])
    body = sending.compose_outbound_body(str(draft["body"]), cfg)

    async with conn.transaction():
        message = await repo.insert_outbound_draft(
            conn,
            lead_id=lead_id,
            contact_id=contact.id,
            campaign_id=lead.campaign_id,
            subject=subject,
            body_text=body,
            sequence_step=FIRST_TOUCH_STEP,
            prompt_version=run.prompt_version,
            from_address=cfg.from_address,
            to_address=to_address,
        )
        approval = await approvals.request_approval(
            conn,
            action_type=ActionType.OUTREACH_DRAFT,
            payload={
                "lead_id": str(lead_id),
                "message_id": str(message.id),
                "to_address": to_address,
                "from_address": cfg.from_address,
                "subject": subject,
                "body": body,
                "facts_asserted": draft["facts_asserted"],
                "angle": brief["angle"],
                "brief_confidence": brief["confidence"],
            },
            # First-touch outreach is always approval-gated in M1.4a
            # (build-spec §4.3; no campaign-level autonomy config exists yet).
            autonomy_level=AutonomyLevel.A2,
            correlation_id=correlation_id,
            causation_id=causation_id,
            requested_by_agent=ACTOR,
            dedupe_key=f"outreach_draft:{message.id}",
        )
        await repo.set_message_approval(conn, message.id, approval.id)
        await emit(
            conn,
            type="outreach.drafted",
            payload={
                "lead_id": str(lead_id),
                "draft_id": str(message.id),
                "sequence_step": FIRST_TOUCH_STEP,
                "requires_approval": True,
                "approval_id": str(approval.id),
                "prompt_version": run.prompt_version,
                "run_id": str(run.id),
            },
            correlation_id=correlation_id,
            actor=ACTOR,
            idempotency_key=f"message:{message.id}:drafted",
            causation_id=causation_id,
        )


async def _draft_precondition_failure(
    conn: asyncpg.Connection,
    *,
    profile: dict[str, Any],
    to_address: str,
    email_status: EmailStatus,
    cfg: DeliverabilityConfig,
) -> tuple[str, str] | None:
    """Cheap checks that make a draft pointless, run before any LLM call. Every
    one of them is re-checked at send time against live state; this only avoids
    spending a model call and a human's approval on something that cannot send."""
    anchors = profile.get("personalization_anchors") or []
    if not anchors:
        return (
            "no_personalization_anchors",
            "prospect profile has no personalization anchors; a generic email with no "
            "specific reason to contact them is not drafted (phase1-llm-boundary.md §3)",
        )
    if email_status not in cfg.allowed_email_statuses:
        return (
            "email_status_not_allowed",
            f"contacts.email_status is {email_status.value}; allowed: "
            f"{sorted(s.value for s in cfg.allowed_email_statuses)}",
        )
    suppressions = await repo.find_active_suppressions(
        conn,
        address=to_address,
        domain=sending.address_domain(to_address),
        now=datetime.now(UTC),
    )
    if any(s.address is not None for s in suppressions):
        return ("suppressed_contact", f"{to_address} is suppressed")
    if suppressions:
        return ("suppressed_domain", f"{sending.address_domain(to_address)} is suppressed")
    return None


# ============================================================================
# Send
# ============================================================================


async def handle_send_approved(
    conn: asyncpg.Connection,
    job: Job,
    *,
    config: DeliverabilityConfig | None = None,
    rng: random.Random | None = None,
) -> None:
    """approval.granted -> a delayed sales.send_outreach job (now + jitter).
    Never sends directly: the send job runs every gate at the moment it fires."""
    approval_id = UUID(job.payload["approval_id"])
    source_event_id = UUID(job.payload["source_event_id"])
    correlation_id = job.payload["correlation_id"]
    cfg = config if config is not None else get_config().deliverability

    approval = await repo.get_approval(conn, approval_id)
    if approval is None:
        raise RevenueEngineError(f"sales.resume_gated_action: approval not found: {approval_id}")
    if approval.action_type != ActionType.OUTREACH_DRAFT:
        return  # other gated actions get their own resume handlers in later milestones

    message_id = UUID(str(approval.payload["message_id"]))
    message = await repo.get_message(conn, message_id)
    if message is None or message.lead_id is None or message.to_address is None:
        raise RevenueEngineError(f"sales.resume_gated_action: message not found: {message_id}")

    existing = await repo.get_job_by_source_event(
        conn, job_type=sending.SEND_JOB_TYPE, source_event_id=source_event_id
    )
    if existing is not None:
        return
    await core_queue.enqueue(
        conn,
        job_type=sending.SEND_JOB_TYPE,
        payload={
            "message_id": str(message.id),
            "lead_id": str(message.lead_id),
            "to_address": message.to_address,
            "correlation_id": correlation_id,
            "source_event_id": str(source_event_id),
        },
        run_after=datetime.now(UTC) + _jitter(cfg, rng),
    )


async def handle_send_outreach(
    conn: asyncpg.Connection,
    job: Job,
    *,
    transport: gmail.GmailTransportProtocol | None = None,
    config: DeliverabilityConfig | None = None,
    environ: Mapping[str, str] | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> None:
    message_id = UUID(job.payload["message_id"])
    lead_id = UUID(job.payload["lead_id"])
    to_address = str(job.payload["to_address"])
    correlation_id = UUID(job.payload["correlation_id"])
    cfg = config if config is not None else get_config().deliverability
    now = now or datetime.now(UTC)

    decision, authorization = await sending.authorize_send(
        conn,
        message_id=message_id,
        lead_id=lead_id,
        to_address=to_address,
        correlation_id=correlation_id,
        now=now,
        config=cfg,
        environ=environ,
    )
    if authorization is None:
        if decision.disposition == sending.Disposition.DEFERRED and decision.retry_after:
            await core_queue.enqueue(
                conn,
                job_type=sending.SEND_JOB_TYPE,
                payload={
                    "message_id": str(message_id),
                    "lead_id": str(lead_id),
                    "to_address": to_address,
                    "correlation_id": str(correlation_id),
                },
                run_after=decision.retry_after + _jitter(cfg, rng),
            )
        return

    try:
        result = await gmail.send(
            authorization,
            transport=transport,
            environ=environ,
            now=now,
            timeout_seconds=cfg.gmail_timeout_seconds,
        )
    except (GmailSendRejectedError, GmailSenderMismatchError, SendNotAuthorizedError) as exc:
        await sending.record_send_outcome(
            conn,
            authorization,
            state=SendState.SEND_FAILED,
            detail=str(exc),
            correlation_id=correlation_id,
        )
        return
    except Exception as exc:  # noqa: BLE001 - any other failure: delivery cannot be known
        await sending.record_send_outcome(
            conn,
            authorization,
            state=SendState.SEND_UNKNOWN,
            detail=repr(exc),
            correlation_id=correlation_id,
        )
        return

    sent = await sending.record_sent(
        conn,
        authorization,
        provider_message_id=result.provider_message_id,
        thread_id=result.thread_id,
        sent_at=now,
    )
    await emit(
        conn,
        type="outreach.sent",
        payload={
            "lead_id": str(authorization.lead_id),
            "message_id": str(sent.id),
            "provider_message_id": result.provider_message_id,
            "thread_id": result.thread_id,
            "sequence_step": authorization.sequence_step,
            "campaign_id": str(authorization.campaign_id) if authorization.campaign_id else None,
            "approval_id": str(authorization.approval_id) if authorization.approval_id else None,
            "sent_at": now.isoformat(),
            "dev_sandbox_redirect": result.dev_sandbox_redirect,
        },
        correlation_id=correlation_id,
        actor=ACTOR,
        idempotency_key=f"message:{result.provider_message_id}:sent",
    )
    if result.dev_sandbox_redirect:
        # agent-contracts.md §3 gate 3: the real recipient was blocked; the mail
        # went to DEV_SANDBOX_EMAIL (CLAUDE.md §6).
        await emit(
            conn,
            type="outreach.blocked",
            payload={
                "lead_id": str(authorization.lead_id),
                "draft_id": str(sent.id),
                "reason": "dev_sandbox",
                "gate": "dev_sandbox",
                "detail": f"ENV is not production; delivered to {result.delivered_to} instead",
                "disposition": "blocked",
                "deferred_until": None,
            },
            correlation_id=correlation_id,
            actor=ACTOR,
            idempotency_key=f"message:{sent.id}:dev_sandbox_redirect",
        )
