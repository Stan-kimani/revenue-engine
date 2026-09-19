"""The send gate (M1.4a, docs/deliverability.md §4-§7, CLAUDE.md §1.8 and §6).

THE PROPERTY THAT MATTERS: this guards the only code in the system that does
something irreversible to a real person. Every gate below reads live database
state immediately before the Gmail call, and every gate fails closed: if a check
cannot be evaluated (config absent, table missing, database unreachable) the
answer is NO SEND — never "unknown, proceed".

The gates, in evaluation order:
  from_domain  the message's from address is the configured from_address, on the
               configured sending domain (deliverability.md §1)
  dev_sandbox  outside production, DEV_SANDBOX_EMAIL must be set — the redirect
               itself happens inside integrations/gmail.py::send (CLAUDE.md §6)
  health       no open §6 pause; a fresh breach opens one and refuses
  message      the row is still 'drafted' and matches the lead/recipient asked for
  approval     core.approvals.is_approved() for the approval bound to THIS message,
               action_type outreach_draft, and the approved content equals the row
  content      §7: no HTML, <= max_links links, no Re:/Fwd: on a first touch,
               opt-out sentence and physical address present
  suppression  recipient address AND company domain against live suppressions;
               contacts.email_status in the allowed set, never suppressed/bounced/invalid
  cap          rolling 24h and 60-minute caps on the domain (follow-ups included),
               plus min_gap_seconds since the last send
  window       recipient-local business hours and weekdays

authorize_send() serialises evaluation per sending domain (advisory lock) and, in
the same transaction, reserves the message (drafted -> sending). A reservation
counts against the cap immediately. It returns a SendAuthorization — the only
thing integrations/gmail.py::send accepts — and the Gmail call happens after the
reservation commits. A suppression inserted in the milliseconds between that
commit and the API call cannot be seen: no database lock spans an external HTTP
call. That window is minimised (authorization_ttl_seconds), not closed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from ..db import repositories as repo
from ..db.models import (
    ActionType,
    Approval,
    ApprovalStatus,
    EmailStatus,
    Message,
    SendState,
    SuppressionReason,
)
from . import approvals
from . import queue as core_queue
from .config import DeliverabilityConfig, HealthConfig, get_config
from .errors import SendGateEvaluationError, SendNotAuthorizedError
from .events import emit

ACTOR = "core.sending"
SEND_JOB_TYPE = "sales.send_outreach"

# entity-model.md D6: these contact statuses are never sendable, whatever
# email_status_tiers says. A rule, not a tunable — a config edit that put
# 'bounced' in the send tier must still not send to it.
_NEVER_SENDABLE_STATUSES = frozenset(
    {EmailStatus.SUPPRESSED, EmailStatus.BOUNCED, EmailStatus.INVALID}
)

_HTML_RE = re.compile(
    r"<\s*/?\s*(html|head|body|div|span|p|br|a|img|table|tr|td|font|style|b|i|u)\b[^>]*>",
    re.IGNORECASE,
)
_LINK_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_REPLY_PREFIX_RE = re.compile(r"^\s*(re|fwd?|fw)\s*:", re.IGNORECASE)


class SendGate(StrEnum):
    FROM_DOMAIN = "from_domain"
    DEV_SANDBOX = "dev_sandbox"
    HEALTH = "health"
    MESSAGE_STATE = "message_state"
    APPROVAL = "approval"
    CONTENT = "content"
    SUPPRESSION = "suppression"
    CAP = "cap"
    WINDOW = "window"
    EVALUATION = "evaluation"


class Disposition(StrEnum):
    """What happens to the message after a refusal."""

    DEFERRED = "deferred"
    """Cap/window/gap: re-enqueued for `retry_after` (plus jitter). Not dropped."""
    HELD = "held"
    """Health pause, pending approval, sandbox/config not evaluable: the message
    stays 'drafted'; scripts/resume_sending.py re-enqueues it."""
    BLOCKED = "blocked"
    """Terminal: the message moves to send_state='blocked'."""


@dataclass(frozen=True)
class SendDecision:
    """Never a bare bool: a refusal always names the gate, the reason, and what
    happens next."""

    allowed: bool
    gate: SendGate | None = None
    reason: str | None = None
    """An outreach.blocked reason enum value."""
    detail: str = ""
    disposition: Disposition | None = None
    retry_after: datetime | None = None

    @classmethod
    def allow(cls) -> SendDecision:
        return cls(allowed=True)

    @classmethod
    def refuse(
        cls,
        gate: SendGate,
        reason: str,
        detail: str,
        disposition: Disposition,
        retry_after: datetime | None = None,
    ) -> SendDecision:
        return cls(
            allowed=False,
            gate=gate,
            reason=reason,
            detail=detail,
            disposition=disposition,
            retry_after=retry_after,
        )


_MINT = object()


class SendAuthorization:
    """The only argument integrations/gmail.py::send accepts. Minted solely by
    authorize_send() after every gate passed and the message was reserved in
    the same committed transaction. Single-use, and refused once older than
    deliverability.authorization_ttl_seconds."""

    def __init__(
        self,
        *,
        message: Message,
        authorized_at: datetime,
        ttl_seconds: int,
        _mint: object,
    ) -> None:
        if _mint is not _MINT:
            raise SendNotAuthorizedError(
                "SendAuthorization can only be created by core.sending.authorize_send()"
            )
        assert message.lead_id is not None and message.to_address and message.from_address
        self.message_id: UUID = message.id
        self.lead_id: UUID = message.lead_id
        self.to_address: str = message.to_address
        self.from_address: str = message.from_address
        self.subject: str = message.subject or ""
        self.body: str = message.body_text or ""
        self.sequence_step: int = message.sequence_step or 0
        self.campaign_id: UUID | None = message.campaign_id
        self.approval_id: UUID | None = message.approval_id
        self.authorized_at = authorized_at
        self._ttl = timedelta(seconds=ttl_seconds)
        self._used = False

    def consume(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if self._used:
            raise SendNotAuthorizedError(f"authorization for {self.message_id} already used")
        if now - self.authorized_at > self._ttl:
            raise SendNotAuthorizedError(
                f"authorization for {self.message_id} expired "
                f"({(now - self.authorized_at).total_seconds():.0f}s old)"
            )
        self._used = True


# ============================================================================
# Pure rules — no I/O (tests/unit/test_sending_gates.py)
# ============================================================================


def address_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower()


def is_role_based_address(address: str, cfg: DeliverabilityConfig) -> bool:
    """info@, sales@, support@ ... — recognisable without a verifier call."""
    local_part = address.rsplit("@", 1)[0].strip().lower()
    return local_part in cfg.role_based_local_parts


def compose_outbound_body(draft_body: str, cfg: DeliverabilityConfig) -> str:
    """Appends §7's opt-out sentence and physical address to the model's
    body at draft time, so the approver sees exactly what sends. An empty
    physical_address is left out — and the content gate then refuses the send."""
    parts = [draft_body.rstrip(), cfg.opt_out_sentence]
    if cfg.physical_address:
        parts.append(cfg.physical_address)
    return "\n\n".join(p for p in parts if p)


def check_content(
    *, subject: str, body: str, cfg: DeliverabilityConfig, first_touch: bool
) -> str | None:
    """§7 message-level requirements. Returns the failure detail, or None."""
    if not cfg.physical_address:
        return "physical_address is not configured (CAN-SPAM requires a postal address)"
    if cfg.physical_address not in body:
        return "body does not contain the configured physical address"
    if not cfg.opt_out_sentence or cfg.opt_out_sentence not in body:
        return "body does not contain the opt-out sentence"
    if _HTML_RE.search(body) or _HTML_RE.search(subject):
        return "HTML markup detected; plain text only"
    links = len(_LINK_RE.findall(body))
    if links > cfg.max_links:
        return f"{links} links exceeds max_links={cfg.max_links}"
    if first_touch and _REPLY_PREFIX_RE.match(subject):
        return "first-touch subject implies a prior conversation (Re:/Fwd:)"
    return None


def resolve_recipient_timezone(
    *,
    contact_attributes: Mapping[str, Any] | None,
    company_country: str | None,
    cfg: DeliverabilityConfig,
) -> tuple[tzinfo, str]:
    """Contact timezone attribute (when enrichment has a valid one) -> company
    country mapping -> default_recipient_timezone. Returns (zone, source)."""
    envelope = (contact_attributes or {}).get("timezone")
    value = envelope.get("value") if isinstance(envelope, dict) else None
    if isinstance(value, str) and value:
        try:
            return ZoneInfo(value), "contact_timezone"
        except (ZoneInfoNotFoundError, ValueError):
            pass
    if company_country:
        mapped = cfg.country_timezones.get(company_country.strip().upper())
        if mapped:
            return ZoneInfo(mapped), f"company_country:{company_country.strip().upper()}"
    return ZoneInfo(cfg.default_recipient_timezone), "default_recipient_timezone"


def next_window_open(now: datetime, zone: tzinfo, cfg: DeliverabilityConfig) -> datetime | None:
    """None if `now` is inside the send window in `zone`; otherwise the UTC
    instant the window next opens."""
    local = now.astimezone(zone)
    if (
        local.weekday() in cfg.send_days
        and cfg.send_window_start <= local.time() < cfg.send_window_end
    ):
        return None
    for offset in range(8):
        day: date = local.date() + timedelta(days=offset)
        if day.weekday() not in cfg.send_days:
            continue
        opens = datetime.combine(day, cfg.send_window_start, tzinfo=zone)
        if opens > local:
            return opens.astimezone(UTC)
    raise ValueError("send_days is empty")  # unreachable: config validation forbids it


@dataclass(frozen=True)
class HealthEvaluation:
    pause_reasons: tuple[str, ...]
    warnings: tuple[tuple[str, float, float], ...]
    """(metric, value, warn_threshold)."""
    metrics: dict[str, Any] = field(default_factory=dict)


def evaluate_health(
    *,
    sends: int,
    hard_bounces: float,
    spam_complaints: float,
    unsubscribes: float,
    health: HealthConfig,
) -> HealthEvaluation:
    """§6 with the M1.4a sample floor: below `sample_floor_sends`, rates do
    not evaluate and absolute counts pause instead; at or above it, the rates
    apply as written. Zero sends is a floor case, never a division by zero."""
    counts: dict[str, float] = {
        "hard_bounce": hard_bounces,
        "spam_complaint": spam_complaints,
        "unsubscribe": unsubscribes,
    }
    metrics: dict[str, Any] = {
        "window_days": health.window_days,
        "sends": sends,
        "sample_floor_sends": health.sample_floor_sends,
        **counts,
    }
    pause: list[str] = []
    warnings: list[tuple[str, float, float]] = []

    if sends < health.sample_floor_sends:
        metrics["mode"] = "below_sample_floor"
        for reason, limit in sorted(health.below_floor_pause_counts.items()):
            if counts[reason] >= limit:
                pause.append(
                    f"{reason} count {counts[reason]:g} >= {limit} "
                    f"(below the {health.sample_floor_sends}-send sample floor)"
                )
        return HealthEvaluation(tuple(pause), tuple(warnings), metrics)

    metrics["mode"] = "rates"
    rate_sources: dict[str, float] = {
        "bounce": hard_bounces,
        "spam_complaint": spam_complaints,
        "unsubscribe": unsubscribes,
    }
    rates = {metric: count / sends for metric, count in rate_sources.items()}
    metrics["rates"] = rates
    for metric in sorted(rates):
        value = rates[metric]
        threshold = health.rates[metric]
        if value >= threshold.pause:
            pause.append(f"{metric} rate {value:.4f} >= pause threshold {threshold.pause}")
        elif value >= threshold.warn:
            warnings.append((metric, value, threshold.warn))
    return HealthEvaluation(tuple(pause), tuple(warnings), metrics)


# ============================================================================
# The gate
# ============================================================================


async def can_send(
    conn: asyncpg.Connection,
    *,
    message_id: UUID,
    lead_id: UUID,
    to_address: str,
    correlation_id: UUID,
    now: datetime | None = None,
    config: DeliverabilityConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> SendDecision:
    """Evaluate every gate against live state. Never raises for a gate that
    cannot be evaluated — that is itself a refusal (gate=evaluation). May write
    a health pause (and its event) when a breach is detected; those writes live
    in a savepoint so a later failure cannot leave half of them behind."""
    now = now or datetime.now(UTC)
    env: Mapping[str, str] = environ if environ is not None else os.environ
    try:
        cfg = config if config is not None else get_config().deliverability
    except Exception as exc:  # noqa: BLE001 - config absent is a refusal, not a crash
        return SendDecision.refuse(
            SendGate.EVALUATION,
            "gate_evaluation_error",
            f"deliverability config unavailable: {exc!r}",
            Disposition.HELD,
        )
    try:
        async with conn.transaction():
            return await _evaluate(
                conn,
                message_id=message_id,
                lead_id=lead_id,
                to_address=to_address,
                correlation_id=correlation_id,
                now=now,
                cfg=cfg,
                env=env,
            )
    except Exception as exc:  # noqa: BLE001 - an unevaluable gate is NO SEND
        return SendDecision.refuse(
            SendGate.EVALUATION,
            "gate_evaluation_error",
            f"gate evaluation failed: {exc!r}",
            Disposition.HELD,
        )


async def _evaluate(
    conn: asyncpg.Connection,
    *,
    message_id: UUID,
    lead_id: UUID,
    to_address: str,
    correlation_id: UUID,
    now: datetime,
    cfg: DeliverabilityConfig,
    env: Mapping[str, str],
) -> SendDecision:
    message = await repo.get_message(conn, message_id)
    if message is None:
        return SendDecision.refuse(
            SendGate.MESSAGE_STATE,
            "message_not_sendable",
            f"message {message_id} does not exist",
            Disposition.BLOCKED,
        )

    # --- from_domain -------------------------------------------------------
    if address_domain(cfg.from_address) != cfg.sending_domain:
        return SendDecision.refuse(
            SendGate.FROM_DOMAIN,
            "wrong_from_domain",
            f"configured from_address {cfg.from_address!r} is not on sending_domain "
            f"{cfg.sending_domain!r}",
            Disposition.BLOCKED,
        )
    if (message.from_address or "").lower() != cfg.from_address:
        return SendDecision.refuse(
            SendGate.FROM_DOMAIN,
            "wrong_from_domain",
            f"message from_address {message.from_address!r} is not the configured "
            f"from_address {cfg.from_address!r}",
            Disposition.BLOCKED,
        )

    # --- dev_sandbox ---------------------------------------------------------
    if env.get("ENV") != "production" and not env.get("DEV_SANDBOX_EMAIL"):
        return SendDecision.refuse(
            SendGate.DEV_SANDBOX,
            "dev_sandbox",
            "ENV is not 'production' and DEV_SANDBOX_EMAIL is unset: there is nowhere safe "
            "to deliver",
            Disposition.HELD,
        )

    # --- health --------------------------------------------------------------
    open_pause = await repo.get_open_sending_pause(conn, sending_domain=cfg.sending_domain)
    if open_pause is not None:
        return SendDecision.refuse(
            SendGate.HEALTH,
            "sending_paused",
            f"sending paused since {open_pause.paused_at.isoformat()}: {open_pause.reason}",
            Disposition.HELD,
        )
    health = await _measure_health(conn, now=now, cfg=cfg)
    if health.pause_reasons:
        reason_text = "; ".join(health.pause_reasons)
        pause, created = await repo.open_sending_pause(
            conn, sending_domain=cfg.sending_domain, reason=reason_text, metrics=health.metrics
        )
        if created:
            await emit(
                conn,
                type="sending.paused",
                payload={
                    "sending_domain": cfg.sending_domain,
                    "pause_id": str(pause.id),
                    "reason": reason_text,
                    "metrics": health.metrics,
                },
                correlation_id=correlation_id,
                actor=ACTOR,
                idempotency_key=f"sending_pause:{pause.id}:paused",
            )
        return SendDecision.refuse(SendGate.HEALTH, "sending_paused", reason_text, Disposition.HELD)
    for metric, value, threshold in health.warnings:
        await emit(
            conn,
            type="sending.health_warning",
            payload={
                "sending_domain": cfg.sending_domain,
                "metric": metric,
                "value": value,
                "warn_threshold": threshold,
                "sends_in_window": health.metrics["sends"],
                "window_days": cfg.health.window_days,
            },
            correlation_id=correlation_id,
            actor=ACTOR,
            idempotency_key=(
                f"sending:{cfg.sending_domain}:health_warning:{metric}:{now.date().isoformat()}"
            ),
        )

    # --- message state -------------------------------------------------------
    if message.send_state != SendState.DRAFTED:
        detail = f"message send_state is {message.send_state}, not drafted"
        if (
            message.send_state == SendState.SENDING
            and message.send_started_at is not None
            and now - message.send_started_at > timedelta(seconds=cfg.authorization_ttl_seconds)
        ):
            # A reservation that outlived its authorization: the worker died or
            # timed out around the Gmail call. Whether it was delivered cannot be
            # known — never resend automatically.
            await repo.mark_message_send_outcome(
                conn,
                message.id,
                state=SendState.SEND_UNKNOWN,
                reason="reservation outlived its authorization; delivery outcome unknown",
            )
            detail = "stale reservation marked send_unknown; a human must reconcile"
        return SendDecision.refuse(
            SendGate.MESSAGE_STATE, "message_not_sendable", detail, Disposition.HELD
        )
    if message.lead_id != lead_id or (message.to_address or "").lower() != to_address.lower():
        return SendDecision.refuse(
            SendGate.MESSAGE_STATE,
            "message_not_sendable",
            "requested lead/recipient does not match the drafted message",
            Disposition.BLOCKED,
        )

    # --- approval ------------------------------------------------------------
    if message.approval_id is None:
        return SendDecision.refuse(
            SendGate.APPROVAL,
            "missing_approval",
            "message has no approval",
            Disposition.BLOCKED,
        )
    if not await approvals.is_approved(conn, message.approval_id):
        approval = await repo.get_approval(conn, message.approval_id)
        status = approval.status if approval else None
        return SendDecision.refuse(
            SendGate.APPROVAL,
            "missing_approval",
            f"approval {message.approval_id} is {status.value if status else 'missing'}, "
            "not granted",
            Disposition.HELD if status == ApprovalStatus.PENDING else Disposition.BLOCKED,
        )
    approval = await repo.get_approval(conn, message.approval_id)
    mismatch = _approval_mismatch(approval, message)
    if mismatch:
        return SendDecision.refuse(
            SendGate.APPROVAL, "approval_mismatch", mismatch, Disposition.BLOCKED
        )

    # --- content -------------------------------------------------------------
    content_problem = check_content(
        subject=message.subject or "",
        body=message.body_text or "",
        cfg=cfg,
        first_touch=(message.sequence_step or 0) == 0,
    )
    if content_problem:
        return SendDecision.refuse(
            SendGate.CONTENT,
            "content_requirements_not_met",
            content_problem,
            Disposition.BLOCKED,
        )

    # --- suppression ---------------------------------------------------------
    recipient = to_address.lower()
    domain = address_domain(recipient)
    suppressions = await repo.find_active_suppressions(
        conn, address=recipient, domain=domain, now=now
    )
    address_rows = [s for s in suppressions if s.address is not None]
    domain_rows = [s for s in suppressions if s.address is None]
    if address_rows:
        hit = address_rows[0]
        return SendDecision.refuse(
            SendGate.SUPPRESSION,
            "suppressed_contact",
            f"{recipient} suppressed ({hit.reason.value}, {hit.source})",
            Disposition.BLOCKED,
        )
    if domain_rows:
        hit = domain_rows[0]
        return SendDecision.refuse(
            SendGate.SUPPRESSION,
            "suppressed_domain",
            f"domain {domain} suppressed ({hit.reason.value}, {hit.source})",
            Disposition.BLOCKED,
        )
    contact = await repo.get_contact(conn, message.contact_id) if message.contact_id else None
    if contact is None:
        return SendDecision.refuse(
            SendGate.SUPPRESSION,
            "email_status_not_allowed",
            "message has no contact; email status cannot be checked",
            Disposition.BLOCKED,
        )
    if contact.email.lower() != recipient:
        return SendDecision.refuse(
            SendGate.APPROVAL,
            "approval_mismatch",
            "contact's current email differs from the approved recipient",
            Disposition.BLOCKED,
        )
    if contact.email_status == EmailStatus.SUPPRESSED:
        return SendDecision.refuse(
            SendGate.SUPPRESSION,
            "suppressed_contact",
            "contacts.email_status is suppressed",
            Disposition.BLOCKED,
        )
    tier = cfg.email_status_tiers.tier_of(contact.email_status)
    if contact.email_status in _NEVER_SENDABLE_STATUSES or tier == "never":
        return SendDecision.refuse(
            SendGate.SUPPRESSION,
            "email_status_not_allowed",
            f"contacts.email_status is {contact.email_status.value} (tier: {tier}); "
            f"sendable tiers: send={sorted(x.value for x in cfg.email_status_tiers.send)}, "
            f"restricted={sorted(x.value for x in cfg.email_status_tiers.restricted)}",
            Disposition.BLOCKED,
        )
    if is_role_based_address(recipient, cfg):
        # Detected in code as well as by the verifier: a role address is
        # recognisable without spending a credit, and it fails on two grounds —
        # bounce risk, and a shared inbox where cold email is deleted unread.
        return SendDecision.refuse(
            SendGate.SUPPRESSION,
            "email_status_not_allowed",
            f"{recipient} is a role-based address (local part in "
            "deliverability.role_based_local_parts)",
            Disposition.BLOCKED,
        )

    # --- cap -----------------------------------------------------------------
    daily = await repo.count_sends_since(
        conn, sending_domain=cfg.sending_domain, since=now - timedelta(hours=24)
    )
    if daily >= cfg.daily_cap:
        return SendDecision.refuse(
            SendGate.CAP,
            "daily_cap_reached",
            f"{daily} sends in the last 24h >= daily_cap {cfg.daily_cap}",
            Disposition.DEFERRED,
            retry_after=now + timedelta(hours=1),
        )
    if tier == "restricted":
        # Catch-all is permitted under stricter accounting, not treated as
        # equivalent to a verified mailbox (docs/deliverability.md §5).
        restricted_sends = await repo.count_sends_since(
            conn,
            sending_domain=cfg.sending_domain,
            since=now - timedelta(hours=24),
            recipient_email_status=contact.email_status,
        )
        if restricted_sends >= cfg.catch_all_daily_cap:
            return SendDecision.refuse(
                SendGate.CAP,
                "catch_all_cap_reached",
                f"{restricted_sends} sends to {contact.email_status.value} recipients in the "
                f"last 24h >= catch_all sub-cap {cfg.catch_all_daily_cap} "
                f"({cfg.catch_all_share:g} of daily_cap {cfg.daily_cap})",
                Disposition.DEFERRED,
                retry_after=now + timedelta(hours=1),
            )
    hourly = await repo.count_sends_since(
        conn, sending_domain=cfg.sending_domain, since=now - timedelta(hours=1)
    )
    if hourly >= cfg.hourly_cap:
        return SendDecision.refuse(
            SendGate.CAP,
            "hourly_cap_reached",
            f"{hourly} sends in the last hour >= hourly_cap {cfg.hourly_cap}",
            Disposition.DEFERRED,
            retry_after=now + timedelta(minutes=15),
        )
    last = await repo.get_last_send_started_at(conn, sending_domain=cfg.sending_domain)
    if last is not None and now - last < timedelta(seconds=cfg.min_gap_seconds):
        return SendDecision.refuse(
            SendGate.CAP,
            "min_gap_not_elapsed",
            f"last send {int((now - last).total_seconds())}s ago < min_gap_seconds "
            f"{cfg.min_gap_seconds}",
            Disposition.DEFERRED,
            retry_after=last + timedelta(seconds=cfg.min_gap_seconds),
        )

    # --- window --------------------------------------------------------------
    company = await repo.get_company(conn, contact.company_id) if contact.company_id else None
    zone, zone_source = resolve_recipient_timezone(
        contact_attributes=contact.attributes,
        company_country=company.country if company else None,
        cfg=cfg,
    )
    opens = next_window_open(now, zone, cfg)
    if opens is not None:
        return SendDecision.refuse(
            SendGate.WINDOW,
            "outside_send_window",
            f"outside {cfg.send_window_start}-{cfg.send_window_end} in {zone} ({zone_source})",
            Disposition.DEFERRED,
            retry_after=opens,
        )

    return SendDecision.allow()


def _approval_mismatch(approval: Approval | None, message: Message) -> str | None:
    if approval is None:
        return "approval row disappeared"
    if approval.action_type != ActionType.OUTREACH_DRAFT:
        return f"approval action_type is {approval.action_type.value}, not outreach_draft"
    payload = approval.payload
    expected = {
        "message_id": str(message.id),
        "to_address": (message.to_address or "").lower(),
        "from_address": (message.from_address or "").lower(),
        "subject": message.subject or "",
        "body": message.body_text or "",
    }
    for key, value in expected.items():
        approved = payload.get(key)
        if isinstance(approved, str) and key in ("to_address", "from_address"):
            approved = approved.lower()
        if approved != value:
            return f"approved {key} does not match the message being sent"
    return None


async def _measure_health(
    conn: asyncpg.Connection, *, now: datetime, cfg: DeliverabilityConfig
) -> HealthEvaluation:
    since = now - timedelta(days=cfg.health.window_days)
    return evaluate_health(
        sends=await repo.count_sends_since(conn, sending_domain=cfg.sending_domain, since=since),
        # Weighted by the recipient's status at send time: a catch-all bounce
        # counts double (docs/deliverability.md §6).
        hard_bounces=await repo.count_weighted_suppressions_since(
            conn,
            reason=SuppressionReason.HARD_BOUNCE,
            since=since,
            weight_by_status=cfg.bounce_weight_by_status,
        ),
        spam_complaints=await repo.count_weighted_suppressions_since(
            conn,
            reason=SuppressionReason.SPAM_COMPLAINT,
            since=since,
            weight_by_status=cfg.bounce_weight_by_status,
        ),
        unsubscribes=await repo.count_suppressions_since(
            conn, reason=SuppressionReason.UNSUBSCRIBE, since=since
        ),
        health=cfg.health,
    )


async def authorize_send(
    conn: asyncpg.Connection,
    *,
    message_id: UUID,
    lead_id: UUID,
    to_address: str,
    correlation_id: UUID,
    now: datetime | None = None,
    config: DeliverabilityConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[SendDecision, SendAuthorization | None]:
    """Lock the sending domain, evaluate every gate, and reserve the message —
    all in one committed transaction. Every refusal is recorded
    (outreach.blocked; terminal refusals also move the message to 'blocked').
    Raises SendGateEvaluationError if a refusal cannot be recorded; the send
    never happens either way."""
    now = now or datetime.now(UTC)
    try:
        async with conn.transaction():
            cfg = config if config is not None else get_config().deliverability
            await repo.lock_sending_domain(conn, cfg.sending_domain)
            decision = await can_send(
                conn,
                message_id=message_id,
                lead_id=lead_id,
                to_address=to_address,
                correlation_id=correlation_id,
                now=now,
                config=cfg,
                environ=environ,
            )
            if decision.allowed:
                # Snapshot the recipient's status onto the reservation: a
                # later bounce is attributed to the tier we sent under, not to
                # whatever the contact's status has become by then.
                recipient = await repo.get_contact_by_email(conn, to_address.lower())
                reserved = await repo.reserve_message_for_send(
                    conn,
                    message_id,
                    now=now,
                    recipient_email_status=(
                        recipient.email_status if recipient else EmailStatus.UNVERIFIED
                    ),
                )
                if reserved is not None:
                    return decision, SendAuthorization(
                        message=reserved,
                        authorized_at=now,
                        ttl_seconds=cfg.authorization_ttl_seconds,
                        _mint=_MINT,
                    )
                decision = SendDecision.refuse(
                    SendGate.MESSAGE_STATE,
                    "message_not_sendable",
                    "message left 'drafted' before it could be reserved",
                    Disposition.HELD,
                )
            await _record_refusal(
                conn,
                message_id=message_id,
                lead_id=lead_id,
                decision=decision,
                correlation_id=correlation_id,
                now=now,
            )
            return decision, None
    except Exception as exc:
        raise SendGateEvaluationError(
            f"send gate for message {message_id} could not be evaluated or recorded: {exc!r}"
        ) from exc


async def _record_refusal(
    conn: asyncpg.Connection,
    *,
    message_id: UUID,
    lead_id: UUID,
    decision: SendDecision,
    correlation_id: UUID,
    now: datetime,
) -> None:
    assert decision.reason and decision.gate and decision.disposition
    if decision.disposition == Disposition.BLOCKED:
        await repo.block_message(conn, message_id, reason=f"{decision.reason}: {decision.detail}")
    message_exists = await repo.get_message(conn, message_id) is not None
    await emit(
        conn,
        type="outreach.blocked",
        payload={
            "lead_id": str(lead_id),
            "draft_id": str(message_id) if message_exists else None,
            "reason": decision.reason,
            "gate": decision.gate.value,
            "detail": decision.detail[:2000],
            "disposition": decision.disposition.value,
            "deferred_until": decision.retry_after.isoformat() if decision.retry_after else None,
        },
        correlation_id=correlation_id,
        actor=ACTOR,
        idempotency_key=f"message:{message_id}:blocked:{decision.reason}:{now.isoformat()}",
    )


async def record_send_outcome(
    conn: asyncpg.Connection,
    authorization: SendAuthorization,
    *,
    state: SendState,
    detail: str,
    correlation_id: UUID,
) -> None:
    """A reservation whose Gmail call did not produce a message id:
    send_failed (definitively not delivered) or send_unknown (cannot tell —
    never retried automatically)."""
    reason = "send_failed" if state == SendState.SEND_FAILED else "send_outcome_unknown"
    async with conn.transaction():
        await repo.mark_message_send_outcome(
            conn, authorization.message_id, state=state, reason=detail[:2000]
        )
        await emit(
            conn,
            type="outreach.blocked",
            payload={
                "lead_id": str(authorization.lead_id),
                "draft_id": str(authorization.message_id),
                "reason": reason,
                "gate": "transport",
                "detail": detail[:2000],
                "disposition": "failed" if state == SendState.SEND_FAILED else "unknown",
                "deferred_until": None,
            },
            correlation_id=correlation_id,
            actor=ACTOR,
            idempotency_key=f"message:{authorization.message_id}:{reason}",
        )


async def record_sent(
    conn: asyncpg.Connection,
    authorization: SendAuthorization,
    *,
    provider_message_id: str,
    thread_id: str | None,
    sent_at: datetime,
) -> Message:
    async with conn.transaction():
        sent = await repo.mark_message_sent(
            conn,
            authorization.message_id,
            provider_message_id=provider_message_id,
            thread_id=thread_id,
            sent_at=sent_at,
        )
        if sent is None:
            raise SendGateEvaluationError(
                f"message {authorization.message_id} was delivered but is no longer 'sending'"
            )
        await repo.mark_lead_touched(conn, authorization.lead_id, at=sent_at)
    return sent


# ============================================================================
# Resume (scripts/resume_sending.py) — §6: a manual action with a recorded reason
# ============================================================================


@dataclass(frozen=True)
class ResumeResult:
    pause_id: UUID | None
    requeued_message_ids: tuple[UUID, ...]


async def resume_sending(
    conn: asyncpg.Connection,
    *,
    resumed_by: str,
    reason: str,
    config: DeliverabilityConfig | None = None,
) -> ResumeResult:
    if not resumed_by.strip() or not reason.strip():
        raise ValueError("resuming sending requires who and why")
    cfg = config if config is not None else get_config().deliverability
    async with conn.transaction():
        pause = await repo.resume_sending_pause(
            conn, sending_domain=cfg.sending_domain, resumed_by=resumed_by, resume_reason=reason
        )
        held = await repo.list_held_message_ids(conn, send_job_type=SEND_JOB_TYPE)
        correlation_id = pause.id if pause else UUID(int=0)
        for message_id in held:
            message = await repo.get_message(conn, message_id)
            assert message is not None and message.lead_id is not None
            await core_queue.enqueue(
                conn,
                job_type=SEND_JOB_TYPE,
                payload={
                    "message_id": str(message.id),
                    "lead_id": str(message.lead_id),
                    "to_address": message.to_address,
                    "correlation_id": str(correlation_id),
                },
            )
        await emit(
            conn,
            type="sending.resumed",
            payload={
                "sending_domain": cfg.sending_domain,
                "pause_id": str(pause.id) if pause else None,
                "resumed_by": resumed_by,
                "reason": reason,
                "requeued_count": len(held),
            },
            correlation_id=correlation_id,
            actor=ACTOR,
            idempotency_key=(
                f"sending:{cfg.sending_domain}:resumed:{pause.id if pause else 'requeue'}:"
                f"{datetime.now(UTC).isoformat()}"
            ),
        )
    return ResumeResult(pause_id=pause.id if pause else None, requeued_message_ids=tuple(held))
