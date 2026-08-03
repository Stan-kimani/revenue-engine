"""All SQL for this milestone lives here (CLAUDE.md §4) — companies, contacts,
leads, events, jobs, per build-spec §10 M0.2. Other tables exist in the
database after migrations/0001_init.sql but get repository functions when the
milestone that needs them arrives.

Functions take an `asyncpg.Connection` directly; pooling is a later concern
(core/queue.py, M0.3). jsonb columns are read back as `str` by asyncpg (no
codec is registered on a bare connection), so every jsonb read goes through
`json.loads` and every jsonb write is cast explicitly with `::jsonb`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import jsonschema

from ..core.errors import (
    DuplicateActiveLeadError,
    InvalidAttributeEnvelopeError,
    RevenueEngineError,
)
from .models import (
    BudgetBand,
    BudgetSource,
    Company,
    Contact,
    EmailStatus,
    Event,
    Job,
    JobStatus,
    Lead,
    LeadBand,
    LeadSource,
    LeadStatus,
    PainCategory,
    TeamSizeBand,
)

_ATTRIBUTE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "schemas" / "entities" / "attribute.json"
)
_ATTRIBUTE_VALIDATOR = jsonschema.Draft202012Validator(
    json.loads(_ATTRIBUTE_SCHEMA_PATH.read_text())
)


def _dump_json(value: Any) -> str:
    return json.dumps(value)


def _validate_attributes(attributes: dict[str, Any]) -> None:
    """Gate every write to an `attributes` JSONB column (entity-model.md §2)."""
    for field_name, envelope in attributes.items():
        errors = [e.message for e in _ATTRIBUTE_VALIDATOR.iter_errors(envelope)]
        if errors:
            raise InvalidAttributeEnvelopeError(field_name, errors)


# ============================================================================
# Companies
# ============================================================================


async def get_company(conn: asyncpg.Connection, company_id: UUID) -> Company | None:
    row = await conn.fetchrow("SELECT * FROM companies WHERE id = $1", company_id)
    return _row_to_company(row) if row else None


async def get_company_by_domain(conn: asyncpg.Connection, domain: str) -> Company | None:
    """Exact domain match — leadgen resolves companies this way before any
    LLM call (agent-contracts.md §1)."""
    row = await conn.fetchrow(
        "SELECT * FROM companies WHERE domain = $1 AND deleted_at IS NULL", domain
    )
    return _row_to_company(row) if row else None


async def upsert_company(
    conn: asyncpg.Connection,
    *,
    name: str,
    domain: str | None = None,
    linkedin_url: str | None = None,
    country: str | None = None,
    employee_band: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Company:
    """Idempotent upsert on the `domain` natural key.

    A company with no domain (some prospects have no site — entity-model.md
    §3.1) always inserts a new row; there is no natural key to upsert on.
    `attributes` merges with any existing envelope map rather than replacing
    it, so re-enrichment doesn't erase previously-known fields.
    """
    if attributes:
        _validate_attributes(attributes)
    attributes_json = _dump_json(attributes or {})

    if domain is None:
        row = await conn.fetchrow(
            """
            INSERT INTO companies (domain, name, linkedin_url, country, employee_band, attributes)
            VALUES (NULL, $1, $2, $3, $4, $5::jsonb)
            RETURNING *
            """,
            name,
            linkedin_url,
            country,
            employee_band,
            attributes_json,
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO companies (domain, name, linkedin_url, country, employee_band, attributes)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (domain) DO UPDATE SET
                name = EXCLUDED.name,
                linkedin_url = COALESCE(EXCLUDED.linkedin_url, companies.linkedin_url),
                country = COALESCE(EXCLUDED.country, companies.country),
                employee_band = COALESCE(EXCLUDED.employee_band, companies.employee_band),
                attributes = companies.attributes || EXCLUDED.attributes,
                updated_at = now()
            RETURNING *
            """,
            domain,
            name,
            linkedin_url,
            country,
            employee_band,
            attributes_json,
        )
    assert row is not None
    return _row_to_company(row)


# ============================================================================
# Contacts
# ============================================================================


async def get_contact(conn: asyncpg.Connection, contact_id: UUID) -> Contact | None:
    row = await conn.fetchrow("SELECT * FROM contacts WHERE id = $1", contact_id)
    return _row_to_contact(row) if row else None


async def get_contact_by_email(conn: asyncpg.Connection, email: str) -> Contact | None:
    row = await conn.fetchrow(
        "SELECT * FROM contacts WHERE email = $1 AND deleted_at IS NULL", email
    )
    return _row_to_contact(row) if row else None


async def upsert_contact(
    conn: asyncpg.Connection,
    *,
    email: str,
    email_status: EmailStatus = EmailStatus.UNVERIFIED,
    full_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    linkedin_url: str | None = None,
    company_id: UUID | None = None,
    attributes: dict[str, Any] | None = None,
) -> Contact:
    """Idempotent upsert on the `email` natural key.

    Job change (entity-model.md §3.2): the same human keeps the same row. If
    `company_id` differs from what's stored, the prior value is appended to
    `attributes.employment_history` before being overwritten — a full
    employment-history table is deferred (entity-model.md §6).
    """
    if attributes:
        _validate_attributes(attributes)
    attributes_json = _dump_json(attributes or {})

    async with conn.transaction():
        existing = await get_contact_by_email(conn, email)

        if existing is not None and company_id is not None and existing.company_id != company_id:
            history_entry = {
                "company_id": str(existing.company_id) if existing.company_id else None,
                "until": datetime.now(UTC).isoformat(),
            }
            await conn.execute(
                """
                UPDATE contacts
                SET attributes = jsonb_set(
                    attributes,
                    '{employment_history}',
                    COALESCE(attributes->'employment_history', '[]'::jsonb) || $2::jsonb
                )
                WHERE email = $1
                """,
                email,
                _dump_json([history_entry]),
            )

        row = await conn.fetchrow(
            """
            INSERT INTO contacts (email, email_status, full_name, first_name, last_name, title,
                                   linkedin_url, company_id, attributes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            ON CONFLICT (email) DO UPDATE SET
                email_status = EXCLUDED.email_status,
                full_name = COALESCE(EXCLUDED.full_name, contacts.full_name),
                first_name = COALESCE(EXCLUDED.first_name, contacts.first_name),
                last_name = COALESCE(EXCLUDED.last_name, contacts.last_name),
                title = COALESCE(EXCLUDED.title, contacts.title),
                linkedin_url = COALESCE(EXCLUDED.linkedin_url, contacts.linkedin_url),
                company_id = COALESCE(EXCLUDED.company_id, contacts.company_id),
                attributes = contacts.attributes || EXCLUDED.attributes,
                updated_at = now()
            RETURNING *
            """,
            email,
            email_status.value,
            full_name,
            first_name,
            last_name,
            title,
            linkedin_url,
            company_id,
            attributes_json,
        )
    assert row is not None
    return _row_to_contact(row)


# ============================================================================
# Leads
# ============================================================================


async def get_lead(conn: asyncpg.Connection, lead_id: UUID) -> Lead | None:
    row = await conn.fetchrow("SELECT * FROM leads WHERE id = $1", lead_id)
    return _row_to_lead(row) if row else None


async def create_lead(
    conn: asyncpg.Connection,
    *,
    contact_id: UUID,
    industry_pack: str,
    source: LeadSource,
    company_id: UUID | None = None,
    campaign_id: UUID | None = None,
    problem_statement: str | None = None,
    pain_category: PainCategory | None = None,
    team_size_band: TeamSizeBand | None = None,
    budget_band: BudgetBand | None = None,
    budget_source: BudgetSource | None = None,
) -> Lead:
    """Create a new lead ("pursuit").

    Raises DuplicateActiveLeadError — never a raw UniqueViolation — if this
    would break the single-thread rule (one active lead per contact, D2; one
    per company, R1). event-catalog.md §3 requires the constraint violation
    be caught and converted, so a caller can emit `lead.deferred` instead of
    crashing.
    """
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO leads (contact_id, company_id, campaign_id, industry_pack, source,
                                problem_statement, pain_category, team_size_band,
                                budget_band, budget_source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING *
            """,
            contact_id,
            company_id,
            campaign_id,
            industry_pack,
            source.value,
            problem_statement,
            pain_category.value if pain_category else None,
            team_size_band.value if team_size_band else None,
            budget_band.value if budget_band else None,
            budget_source.value if budget_source else None,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateActiveLeadError(exc.constraint_name or "unknown") from exc

    assert row is not None
    return _row_to_lead(row)


async def update_lead_status(conn: asyncpg.Connection, lead_id: UUID, status: LeadStatus) -> Lead:
    row = await conn.fetchrow(
        "UPDATE leads SET status = $2, updated_at = now() WHERE id = $1 RETURNING *",
        lead_id,
        status.value,
    )
    if row is None:
        raise RevenueEngineError(f"Lead not found: {lead_id}")
    return _row_to_lead(row)


# ============================================================================
# Events (outbox)
# ============================================================================


async def emit_event(
    conn: asyncpg.Connection,
    *,
    type: str,
    payload: dict[str, Any],
    correlation_id: UUID,
    actor: str,
    idempotency_key: str,
    causation_id: UUID | None = None,
    version: int = 1,
) -> Event:
    """Insert an event, or return the existing one on a duplicate
    `idempotency_key` — re-emission is a no-op, not an error (event-catalog.md
    §R3, §9.4). Payload schema validation against schemas/events/<type>.json
    is core/events.py::emit()'s job (M0.3); this function only persists.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO events (type, version, actor, correlation_id, causation_id,
                             idempotency_key, payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING *
        """,
        type,
        version,
        actor,
        correlation_id,
        causation_id,
        idempotency_key,
        _dump_json(payload),
    )
    if row is not None:
        return _row_to_event(row)

    existing = await conn.fetchrow(
        "SELECT * FROM events WHERE idempotency_key = $1", idempotency_key
    )
    assert existing is not None
    return _row_to_event(existing)


async def get_event(conn: asyncpg.Connection, event_id: UUID) -> Event | None:
    row = await conn.fetchrow("SELECT * FROM events WHERE event_id = $1", event_id)
    return _row_to_event(row) if row else None


# ============================================================================
# Jobs (queue)
# ============================================================================


async def enqueue_job(
    conn: asyncpg.Connection,
    *,
    type: str,
    payload: dict[str, Any],
    run_after: datetime | None = None,
) -> Job:
    row = await conn.fetchrow(
        """
        INSERT INTO jobs (type, payload, run_after)
        VALUES ($1, $2::jsonb, COALESCE($3, now()))
        RETURNING *
        """,
        type,
        _dump_json(payload),
        run_after,
    )
    assert row is not None
    return _row_to_job(row)


async def get_job(conn: asyncpg.Connection, job_id: UUID) -> Job | None:
    row = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    return _row_to_job(row) if row else None


async def claim_job(conn: asyncpg.Connection, *, worker_id: str) -> Job | None:
    """Claim one pending, due job for `worker_id` via SELECT ... FOR UPDATE
    SKIP LOCKED (build-spec §2). The row lock is held until the caller's
    transaction commits — call this inside a transaction you control.
    """
    row = await conn.fetchrow(
        """
        UPDATE jobs
        SET status = 'running', locked_by = $1, locked_at = now(), updated_at = now()
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = 'pending' AND run_after <= now()
            ORDER BY run_after
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """,
        worker_id,
    )
    return _row_to_job(row) if row else None


async def complete_job(conn: asyncpg.Connection, job_id: UUID) -> Job:
    row = await conn.fetchrow(
        "UPDATE jobs SET status = 'completed', updated_at = now() WHERE id = $1 RETURNING *",
        job_id,
    )
    if row is None:
        raise RevenueEngineError(f"Job not found: {job_id}")
    return _row_to_job(row)


async def mark_job_failed(
    conn: asyncpg.Connection,
    job_id: UUID,
    *,
    error: str,
    retry_after: datetime | None,
) -> Job:
    """Record a failure. `retry_after=None` dead-letters the job; a value
    resets it to pending at that time. The retry-vs-dead-letter decision and
    backoff calculation belong to core/queue.py (M0.3) — this only executes
    whichever outcome it's given.
    """
    status = JobStatus.PENDING.value if retry_after is not None else JobStatus.DEAD_LETTER.value
    row = await conn.fetchrow(
        """
        UPDATE jobs
        SET status = $2, attempts = attempts + 1, last_error = $3,
            run_after = COALESCE($4, run_after), locked_by = NULL, locked_at = NULL,
            updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        job_id,
        status,
        error,
        retry_after,
    )
    if row is None:
        raise RevenueEngineError(f"Job not found: {job_id}")
    return _row_to_job(row)


# ============================================================================
# Row -> model mapping
# ============================================================================


def _row_to_company(row: asyncpg.Record) -> Company:
    return Company(
        id=row["id"],
        name=row["name"],
        domain=row["domain"],
        linkedin_url=row["linkedin_url"],
        country=row["country"],
        employee_band=row["employee_band"],
        attributes=json.loads(row["attributes"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _row_to_contact(row: asyncpg.Record) -> Contact:
    return Contact(
        id=row["id"],
        email=row["email"],
        email_status=EmailStatus(row["email_status"]),
        full_name=row["full_name"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        title=row["title"],
        linkedin_url=row["linkedin_url"],
        company_id=row["company_id"],
        attributes=json.loads(row["attributes"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _row_to_lead(row: asyncpg.Record) -> Lead:
    return Lead(
        id=row["id"],
        contact_id=row["contact_id"],
        company_id=row["company_id"],
        campaign_id=row["campaign_id"],
        industry_pack=row["industry_pack"],
        source=LeadSource(row["source"]),
        status=LeadStatus(row["status"]),
        band=LeadBand(row["band"]) if row["band"] else None,
        current_score=row["current_score"],
        deal_id=row["deal_id"],
        budget_band=BudgetBand(row["budget_band"]) if row["budget_band"] else None,
        budget_source=BudgetSource(row["budget_source"]) if row["budget_source"] else None,
        problem_statement=row["problem_statement"],
        pain_category=PainCategory(row["pain_category"]) if row["pain_category"] else None,
        team_size_band=TeamSizeBand(row["team_size_band"]) if row["team_size_band"] else None,
        first_touched_at=row["first_touched_at"],
        last_activity_at=row["last_activity_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _row_to_event(row: asyncpg.Record) -> Event:
    return Event(
        event_id=row["event_id"],
        type=row["type"],
        version=row["version"],
        occurred_at=row["occurred_at"],
        actor=row["actor"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        idempotency_key=row["idempotency_key"],
        payload=json.loads(row["payload"]),
        processed_at=row["processed_at"],
    )


def _row_to_job(row: asyncpg.Record) -> Job:
    return Job(
        id=row["id"],
        type=row["type"],
        payload=json.loads(row["payload"]),
        status=JobStatus(row["status"]),
        run_after=row["run_after"],
        attempts=row["attempts"],
        locked_by=row["locked_by"],
        locked_at=row["locked_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
