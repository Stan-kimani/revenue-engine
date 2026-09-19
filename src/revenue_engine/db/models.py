"""Typed row models. Pydantic, no ORM — mirroring migrations/0001_init.sql.

Only the entities named in build-spec §10 M0.2 (companies, contacts, leads,
events, jobs) are modelled here. Other tables exist in the database after
migration 0001 but get row types when the milestone that needs them arrives.

Closed-vocabulary columns are `text` + CHECK at the database layer (see
migrations/0001_init.sql), so these StrEnums are an application-layer
convenience only — mypy catches a typoed status string, they don't change
what's stored.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class EmailStatus(StrEnum):
    UNVERIFIED = "unverified"
    VALID = "valid"
    RISKY = "risky"
    INVALID = "invalid"
    BOUNCED = "bounced"
    SUPPRESSED = "suppressed"


class LeadSource(StrEnum):
    WEBFORM = "webform"
    MANUAL_IMPORT = "manual_import"
    DISCOVERY = "discovery"
    REFERRAL = "referral"
    INBOUND_REPLY = "inbound_reply"


class LeadStatus(StrEnum):
    NEW = "new"
    DEFERRED = "deferred"
    ENRICHING = "enriching"
    ENRICH_FAILED = "enrich_failed"
    SCORED = "scored"
    QUALIFIED = "qualified"
    ENGAGED = "engaged"
    MEETING_BOOKED = "meeting_booked"
    CONVERTED = "converted"
    DISQUALIFIED = "disqualified"
    UNSUBSCRIBED = "unsubscribed"
    DORMANT = "dormant"


class LeadBand(StrEnum):
    COLD = "cold"
    WARM = "warm"
    MQL = "mql"
    SQL = "sql"


class BudgetBand(StrEnum):
    UNKNOWN = "unknown"
    UNDER_5K = "under_5k"
    FIVE_TO_15K = "5k_15k"
    FIFTEEN_TO_40K = "15k_40k"
    FORTY_K_PLUS = "40k_plus"


class BudgetSource(StrEnum):
    SELF_REPORTED = "self_reported"
    INFERRED = "inferred"
    DISCOVERY_CALL = "discovery_call"


class PainCategory(StrEnum):
    MANUAL_DATA_ENTRY = "manual_data_entry"
    SLOW_FOLLOWUP = "slow_followup"
    REPORTING_VISIBILITY = "reporting_visibility"
    INTAKE = "intake"
    RECONCILIATION = "reconciliation"
    OTHER = "other"


class TeamSizeBand(StrEnum):
    SOLO = "solo"
    TWO_TO_10 = "2_10"
    ELEVEN_TO_50 = "11_50"
    FIFTY_PLUS = "50_plus"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class AgentRunStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class Tier(StrEnum):
    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"


class AutonomyLevel(StrEnum):
    """agent-contracts.md §0.4. A0/A1 never reach core/approvals.py in
    practice (A0 has no external side effects to gate; A1's side effects are
    autonomous within config caps); A2 is the level that actually blocks.
    A3 means the agent never attempts automated execution at all — a human
    acts directly, so this code path is never reached either. Kept as all
    four values (not just A2/A3) because `request_approval()` takes the
    caller's real level and decides whether to block from
    `thresholds.yaml`'s `approvals.autonomy_requires_approval` list
    (core/config.py), not from a hardcoded assumption baked into this enum."""

    A0 = "A0"
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"


class ActionType(StrEnum):
    """Matches migrations/0001_init.sql's `approvals.action_type` CHECK
    constraint and event-catalog.md §7.1's expiry-policy table exactly."""

    OUTREACH_DRAFT = "outreach_draft"
    SEQUENCE_STEP = "sequence_step"
    PROPOSAL_SEND = "proposal_send"
    PRICING_DISCOUNT = "pricing_discount"
    CAMPAIGN_LAUNCH = "campaign_launch"
    ICP_UPDATE = "icp_update"
    CRM_MERGE = "crm_merge"
    RECORD_DELETE = "record_delete"


class ApprovalStatus(StrEnum):
    """Matches migrations/0001_init.sql's `approvals.status` CHECK constraint
    — 'granted'/'denied', not 'approved'/'rejected' (event-catalog.md's
    `approval.granted`/`approval.denied` and orchestrator/router.py already
    agree on this vocabulary)."""

    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


class Company(BaseModel):
    id: UUID
    name: str
    domain: str | None
    linkedin_url: str | None
    country: str | None
    employee_band: str | None
    attributes: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class Contact(BaseModel):
    id: UUID
    email: str
    email_status: EmailStatus
    full_name: str | None
    first_name: str | None
    last_name: str | None
    title: str | None
    linkedin_url: str | None
    company_id: UUID | None
    attributes: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class Lead(BaseModel):
    id: UUID
    contact_id: UUID
    company_id: UUID | None
    campaign_id: UUID | None
    industry_pack: str
    source: LeadSource
    status: LeadStatus
    band: LeadBand | None
    current_score: Decimal | None
    deal_id: UUID | None
    budget_band: BudgetBand | None
    budget_source: BudgetSource | None
    problem_statement: str | None
    pain_category: PainCategory | None
    team_size_band: TeamSizeBand | None
    profile: dict[str, Any] | None
    first_touched_at: datetime | None
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class LeadCreationResult(BaseModel):
    """Return type of repositories.create_lead(). Total over three outcomes —
    no path raises a raw database exception:

    1. Created (`deferred=False, failed=False`): `lead` is the newly created
       active lead.
    2. Deferred (`deferred=True, failed=False`): the single-thread rule
       (one_active_lead_per_company) blocked the create — a normal business
       outcome (event-catalog.md §3), not an error. `lead` is the persisted
       `status='deferred'` placeholder row event-catalog.md §3 documents. The
       caller (a later milestone — core/events.py doesn't exist yet) emits
       `lead.deferred`.
    3. Failed (`failed=True`): the deferred-placeholder insert itself could
       not be completed (any database error, not just a unique violation).
       `lead` is None; `error` carries the failure for the caller to log or
       decide what to do with. `blocked_by_lead_id` is still populated if it
       was found before the failure. Mechanically, this should be
       unreachable today — a `status='deferred'` row is excluded from both
       partial indexes' predicates, so it cannot violate either — but the
       contract is enforced unconditionally rather than assumed, since it
       depends on both indexes' exclusion lists staying identical forever
       (docs/decisions.md has the full reasoning and a test that forces this
       branch via fault injection, since it can't be reached with real data
       under the current schema).

    The contact-level constraint (one_active_lead_per_contact) is a different
    case and is NOT represented here — create_lead() raises
    DuplicateActiveLeadError for that instead, since there is no documented
    business-outcome event for it.
    """

    lead: Lead | None
    deferred: bool
    failed: bool = False
    blocked_by_lead_id: UUID | None = None
    error: str | None = None


class LeadScore(BaseModel):
    """One append-only row of `lead_scores` (entity-model.md §3.5). Never
    updated, never deleted — a re-score is a new row, never a mutation of an
    old one."""

    id: UUID
    lead_id: UUID
    total: Decimal
    band: LeadBand
    components: dict[str, Any]
    deterministic_part: Decimal | None
    llm_part: Decimal | None
    prompt_version: int | None
    model: str | None
    run_id: UUID | None
    scored_at: datetime


class Event(BaseModel):
    event_id: UUID
    type: str
    version: int
    occurred_at: datetime
    actor: str
    correlation_id: UUID
    causation_id: UUID | None
    idempotency_key: str
    payload: dict[str, Any]
    processed_at: datetime | None


class Job(BaseModel):
    id: UUID
    type: str
    payload: dict[str, Any]
    status: JobStatus
    run_after: datetime
    attempts: int
    locked_by: str | None
    locked_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class AgentRun(BaseModel):
    """One core/llm.py::complete_json() call (M0.4). Written on both success
    and failure — see docs/decisions.md for why this table's columns extend
    beyond build-spec §5.1's original locked shape."""

    id: UUID
    agent: str
    trigger_event: UUID | None
    trace_id: str | None
    prompt_id: str
    prompt_version: int
    tier: Tier
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost: Decimal | None
    latency_ms: int | None
    status: AgentRunStatus
    error: str | None
    retry_count: int
    created_at: datetime


class Approval(BaseModel):
    """One row of `approvals` (migrations/0001_init.sql + 0005, M1.3). The
    gate CLAUDE.md §1 non-negotiable 8 requires: nothing downstream of an
    A2/A3 action executes without a committed row here reaching
    `status=granted`. `core/approvals.py::resolve()` and `expire_stale()` are
    the ONLY code paths permitted to change `status` away from `pending` —
    enforced doubly, in application code (`UPDATE ... WHERE status='pending'`)
    and in the database itself (migrations/0005's `approvals_forbid_redecision`
    trigger)."""

    id: UUID
    action_type: ActionType
    payload: dict[str, Any]
    requested_by_agent: str | None
    status: ApprovalStatus
    decided_by: str | None
    token: str | None
    expires_at: datetime | None
    decided_at: datetime | None
    decision_reason: str | None
    correlation_id: UUID | None
    causation_id: UUID | None
    dedupe_key: str | None
    created_at: datetime


class SendState(StrEnum):
    """migrations/0006's `messages.send_state`. The allowed transitions are
    enforced by a database trigger, not by this enum (see the migration)."""

    DRAFTED = "drafted"
    SENDING = "sending"
    SENT = "sent"
    SEND_FAILED = "send_failed"
    SEND_UNKNOWN = "send_unknown"
    BLOCKED = "blocked"


class SuppressionReason(StrEnum):
    UNSUBSCRIBE = "unsubscribe"
    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"
    SPAM_COMPLAINT = "spam_complaint"
    HOSTILE_REPLY = "hostile_reply"
    MANUAL = "manual"


class Message(BaseModel):
    """One row of `messages` (migrations/0001 + 0006)."""

    id: UUID
    lead_id: UUID | None
    contact_id: UUID | None
    campaign_id: UUID | None
    direction: str
    channel: str
    provider_message_id: str | None
    thread_id: str | None
    subject: str | None
    body_text: str | None
    sequence_step: int | None
    prompt_version: int | None
    approval_id: UUID | None
    from_address: str | None
    to_address: str | None
    send_state: SendState | None
    send_started_at: datetime | None
    send_block_reason: str | None
    sent_at: datetime | None
    created_at: datetime
    updated_at: datetime


class Suppression(BaseModel):
    """`address is None` means the whole `domain` is suppressed."""

    id: UUID
    address: str | None
    domain: str | None
    reason: SuppressionReason
    source: str
    expires_at: datetime | None
    created_at: datetime


class SendingPause(BaseModel):
    id: UUID
    sending_domain: str
    reason: str
    metrics: dict[str, Any]
    paused_at: datetime
    resumed_at: datetime | None
    resumed_by: str | None
    resume_reason: str | None
