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
