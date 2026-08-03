"""Typed row models. No ORM — plain dataclasses mirroring migrations/0001_init.sql.

Only the entities named in build-spec §10 M0.2 (companies, contacts, leads,
events, jobs) are modelled here. Other tables exist in the database after
migration 0001 but get row types when the milestone that needs them arrives.

Closed-vocabulary columns are `text` + CHECK at the database layer (see
migrations/0001_init.sql), so these StrEnums are an application-layer
convenience only — mypy catches a typoed status string, they don't change
what's stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID


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


@dataclass
class Company:
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


@dataclass
class Contact:
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


@dataclass
class Lead:
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


@dataclass
class Event:
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


@dataclass
class Job:
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
