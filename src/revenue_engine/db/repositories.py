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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    ActionType,
    AgentRun,
    AgentRunStatus,
    Approval,
    ApprovalStatus,
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
    LeadCreationResult,
    LeadScore,
    LeadSource,
    LeadStatus,
    Message,
    PainCategory,
    SendingPause,
    SendState,
    Suppression,
    SuppressionReason,
    TeamSizeBand,
    Tier,
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


_LEAD_INSERT_COLUMNS = """
    contact_id, company_id, campaign_id, industry_pack, source, status,
    problem_statement, pain_category, team_size_band, budget_band, budget_source
"""


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
) -> LeadCreationResult:
    """Create a new lead ("pursuit"). Total over three outcomes — see
    LeadCreationResult's docstring — never raises a raw database exception.

    The two single-thread constraints are handled differently, deliberately:

    - Violating `one_active_lead_per_company` (R1) is a normal business
      outcome (event-catalog.md §3), never an error. It's caught here, and a
      second row is inserted with `status='deferred'` — event-catalog.md §3's
      documented recovery path — instead of raising. The caller (a later
      milestone) emits `lead.deferred`; this function never does, since
      `core/events.py` doesn't exist yet.
    - Violating `one_active_lead_per_contact` (D2) has no analogous
      documented business outcome — it raises `DuplicateActiveLeadError`, not
      a raw `UniqueViolationError`.

    The deferred-placeholder insert is wrapped separately: both single-thread
    indexes exclude `status='deferred'` from their predicate (identically —
    see docs/decisions.md), so that insert cannot violate either one, and
    mechanically no other constraint on `leads` can fire either, since every
    other column reuses values that already passed on the first attempt. It
    is nonetheless not trusted to always succeed — any database error there
    returns `LeadCreationResult(failed=True, error=...)` instead of
    propagating, so a future migration that (for example) lets the two
    indexes' exclusion lists drift apart can't turn into an unhandled
    exception here.

    Concurrent-deferred-insert safety (migrations/0004, docs/decisions.md,
    M1.1 Correction 1): a second caller racing to defer the SAME contact
    against the SAME already-occupied company (e.g. two overlapping
    scripts/import_leads.py runs over the same CSV) is resolved by the
    `one_deferred_lead_per_contact` partial unique index, not by trusting the
    caller to check first. The INSERT below targets that index with
    `ON CONFLICT ... DO NOTHING`; when the conflict fires, the existing
    deferred row is re-read and returned instead — both racing callers
    converge on the same lead row, never two.
    """
    try:
        row = await conn.fetchrow(
            f"""
            INSERT INTO leads ({_LEAD_INSERT_COLUMNS})
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            contact_id,
            company_id,
            campaign_id,
            industry_pack,
            source.value,
            LeadStatus.NEW.value,
            problem_statement,
            pain_category.value if pain_category else None,
            team_size_band.value if team_size_band else None,
            budget_band.value if budget_band else None,
            budget_source.value if budget_source else None,
        )
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name != "one_active_lead_per_company":
            raise DuplicateActiveLeadError(exc.constraint_name or "unknown") from exc

        blocking = None
        try:
            async with conn.transaction():
                blocking = await conn.fetchrow(
                    """
                    SELECT id FROM leads
                    WHERE company_id = $1
                      AND status NOT IN
                          ('deferred', 'converted', 'disqualified', 'unsubscribed', 'dormant')
                      AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    company_id,
                )
                deferred_row = await conn.fetchrow(
                    f"""
                    INSERT INTO leads ({_LEAD_INSERT_COLUMNS})
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (contact_id) WHERE status = 'deferred' AND deleted_at IS NULL
                    DO NOTHING
                    RETURNING *
                    """,
                    contact_id,
                    company_id,
                    campaign_id,
                    industry_pack,
                    source.value,
                    LeadStatus.DEFERRED.value,
                    problem_statement,
                    pain_category.value if pain_category else None,
                    team_size_band.value if team_size_band else None,
                    budget_band.value if budget_band else None,
                    budget_source.value if budget_source else None,
                )
                if deferred_row is None:
                    # Lost the race: a concurrent caller's deferred insert for
                    # this exact contact committed first and this one was
                    # skipped by ON CONFLICT DO NOTHING (migrations/0004).
                    # Re-read the row it created rather than insert a second
                    # one.
                    deferred_row = await conn.fetchrow(
                        """
                        SELECT * FROM leads
                        WHERE contact_id = $1 AND status = 'deferred' AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        contact_id,
                    )
        except asyncpg.PostgresError as inner_exc:
            return LeadCreationResult(
                lead=None,
                deferred=False,
                failed=True,
                blocked_by_lead_id=blocking["id"] if blocking else None,
                error=str(inner_exc),
            )

        if deferred_row is None:
            # Should be unreachable: ON CONFLICT DO NOTHING only fires when a
            # deferred row for this contact already exists, so the re-read
            # above must find it. Not asserted — a concurrency-sensitive path
            # failing here should surface as a typed, loggable result, not a
            # bare AssertionError.
            return LeadCreationResult(
                lead=None,
                deferred=False,
                failed=True,
                blocked_by_lead_id=blocking["id"] if blocking else None,
                error=(
                    "deferred insert conflicted on one_deferred_lead_per_contact "
                    "but no existing deferred row was found on re-read"
                ),
            )

        return LeadCreationResult(
            lead=_row_to_lead(deferred_row),
            deferred=True,
            blocked_by_lead_id=blocking["id"] if blocking else None,
        )

    assert row is not None
    return LeadCreationResult(lead=_row_to_lead(row), deferred=False)


async def update_lead_status(conn: asyncpg.Connection, lead_id: UUID, status: LeadStatus) -> Lead:
    row = await conn.fetchrow(
        "UPDATE leads SET status = $2, updated_at = now() WHERE id = $1 RETURNING *",
        lead_id,
        status.value,
    )
    if row is None:
        raise RevenueEngineError(f"Lead not found: {lead_id}")
    return _row_to_lead(row)


async def update_lead_profile(
    conn: asyncpg.Connection, lead_id: UUID, profile: dict[str, Any]
) -> Lead:
    """Write the schema-validated leadgen/build_prospect_profile output
    (migrations/0003) after all three enrichment LLM calls have succeeded —
    agents/leadgen.py never calls this until it holds all three, so a lead
    never has a partial/inconsistent profile written (M1.1, "no partial
    write" requirement)."""
    row = await conn.fetchrow(
        "UPDATE leads SET profile = $2::jsonb, updated_at = now() WHERE id = $1 RETURNING *",
        lead_id,
        _dump_json(profile),
    )
    if row is None:
        raise RevenueEngineError(f"Lead not found: {lead_id}")
    return _row_to_lead(row)


async def get_active_or_deferred_lead_by_contact(
    conn: asyncpg.Connection, contact_id: UUID
) -> Lead | None:
    """The most recent non-terminal lead for a contact — 'new', 'deferred',
    or anywhere in between (excludes converted/disqualified/unsubscribed/
    dormant). Used by scripts/import_leads.py as a re-import idempotency
    PRE-CHECK: an optimisation that avoids re-running create_lead (and the
    enrichment work its lead.captured event would trigger) for a row that's
    already in the pipeline.

    This is explicitly NOT the thing that makes re-import safe under
    concurrency — check-then-act has an inherent race window. The actual
    guarantee is structural: `one_active_lead_per_contact` /
    `one_active_lead_per_company` (migrations/0001) for the active case, and
    `one_deferred_lead_per_contact` (migrations/0004) for the deferred case.
    A caller that loses a race against this pre-check still gets a typed,
    idempotent outcome from create_lead() (DuplicateActiveLeadError, or the
    re-read deferred row) rather than a duplicate."""
    row = await conn.fetchrow(
        """
        SELECT * FROM leads
        WHERE contact_id = $1
          AND status NOT IN ('converted', 'disqualified', 'unsubscribed', 'dormant')
          AND deleted_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        contact_id,
    )
    return _row_to_lead(row) if row else None


async def refresh_lead_band_and_score(
    conn: asyncpg.Connection,
    lead_id: UUID,
    *,
    current_score: Decimal,
    band: LeadBand,
    status: LeadStatus,
) -> Lead:
    """Refresh `leads.current_score`/`band`/`status` after a new `lead_scores`
    row is inserted — `current_score` is a denormalised cache of the latest
    append-only score (entity-model.md §3.5), never the source of truth
    itself."""
    row = await conn.fetchrow(
        """
        UPDATE leads
        SET current_score = $2, band = $3, status = $4, last_activity_at = now(), updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        lead_id,
        current_score,
        band.value,
        status.value,
    )
    if row is None:
        raise RevenueEngineError(f"Lead not found: {lead_id}")
    return _row_to_lead(row)


# ============================================================================
# Lead scores (append-only, entity-model.md §3.5) & engagement counts
# ============================================================================


async def insert_lead_score(
    conn: asyncpg.Connection,
    *,
    lead_id: UUID,
    total: Decimal,
    band: LeadBand,
    components: dict[str, Any],
    deterministic_part: Decimal,
    llm_part: Decimal,
    prompt_version: int,
    model: str,
    run_id: UUID,
) -> LeadScore:
    """Always a plain INSERT, never an upsert — lead_scores is append-only by
    design (entity-model.md §3.5: 'Never updated. Never deleted.'). Every
    re-score is a new row, so a lead's score history is auditable."""
    row = await conn.fetchrow(
        """
        INSERT INTO lead_scores (lead_id, total, band, components, deterministic_part,
                                  llm_part, prompt_version, model, run_id)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
        RETURNING *
        """,
        lead_id,
        total,
        band.value,
        _dump_json(components),
        deterministic_part,
        llm_part,
        prompt_version,
        model,
        run_id,
    )
    assert row is not None
    return _row_to_lead_score(row)


async def get_lead_scores(conn: asyncpg.Connection, lead_id: UUID) -> list[LeadScore]:
    """All score history for one lead, oldest first — used by tests asserting
    append-only behaviour and, later, by the Learning Agent."""
    rows = await conn.fetch(
        "SELECT * FROM lead_scores WHERE lead_id = $1 ORDER BY scored_at ASC", lead_id
    )
    return [_row_to_lead_score(row) for row in rows]


async def count_inbound_messages(conn: asyncpg.Connection, lead_id: UUID) -> int:
    """Replies — the one engagement signal `messages` actually carries today
    (agents/qualification.py's engagement component). No opened_at/clicked_at
    columns exist yet (migrations/0001), so opens/clicks cannot be counted at
    all here, not merely down-weighted."""
    count = await conn.fetchval(
        "SELECT count(*) FROM messages WHERE lead_id = $1 AND direction = 'inbound'", lead_id
    )
    return int(count)


async def count_meetings(conn: asyncpg.Connection, lead_id: UUID) -> int:
    """Meetings booked for this lead — the strongest engagement signal
    available (M1.2 Correction 2, docs/decisions.md)."""
    count = await conn.fetchval("SELECT count(*) FROM meetings WHERE lead_id = $1", lead_id)
    return int(count)


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


async def claim_unprocessed_event(conn: asyncpg.Connection) -> Event | None:
    """Claim one unprocessed event via SELECT ... FOR UPDATE SKIP LOCKED,
    oldest first. The row lock is held until the caller's transaction commits
    — call this inside a transaction you control, and mark it processed
    (`mark_event_processed`) in that same transaction (M0.3 correctness
    requirement (a): an event must never be marked processed without its jobs
    having been enqueued in the same commit — see core/queue.py and
    docs/decisions.md for the full mechanism).

    Ordering note: FOR UPDATE SKIP LOCKED does not guarantee strict
    occurred_at order under concurrent claimers — a row skipped by one
    claimer isn't re-offered to the next in order. M0.3 runs a single event
    dispatcher (docs/decisions.md), so this doesn't matter in practice yet;
    documented as a known scaling limit, not silently relied upon.
    """
    row = await conn.fetchrow(
        """
        SELECT * FROM events
        WHERE processed_at IS NULL
        ORDER BY occurred_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    )
    return _row_to_event(row) if row else None


async def mark_event_processed(conn: asyncpg.Connection, event_id: UUID) -> None:
    await conn.execute("UPDATE events SET processed_at = now() WHERE event_id = $1", event_id)


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


async def get_job_by_source_event(
    conn: asyncpg.Connection, *, job_type: str, source_event_id: UUID
) -> Job | None:
    """Backs core/queue.py::enqueue_for_event()'s dedup-on-(event_id, job_type)
    upsert. `jobs` has no dedicated column for this — `source_event_id` is
    read out of the jsonb `payload` (embedded there by enqueue_for_event).
    """
    row = await conn.fetchrow(
        "SELECT * FROM jobs WHERE type = $1 AND (payload->>'source_event_id')::uuid = $2 LIMIT 1",
        job_type,
        source_event_id,
    )
    return _row_to_job(row) if row else None


async def claim_jobs(conn: asyncpg.Connection, *, worker_id: str, limit: int) -> list[Job]:
    """Claim up to `limit` pending, due jobs for `worker_id` via
    SELECT ... FOR UPDATE SKIP LOCKED (build-spec §2). Ties on `run_after`
    (the common case for jobs enqueued together in one transaction) are
    broken by `created_at` so ordering is deterministic, not
    insertion-order-by-accident. Reclaiming stale 'running' jobs is
    deliberately NOT done here — see `reclaim_stale_jobs`.
    """
    rows = await conn.fetch(
        """
        UPDATE jobs
        SET status = 'running', locked_by = $1, locked_at = now(), updated_at = now()
        WHERE id IN (
            SELECT id FROM jobs
            WHERE status = 'pending' AND run_after <= now()
            ORDER BY run_after, created_at
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        RETURNING *
        """,
        worker_id,
        limit,
    )
    return [_row_to_job(row) for row in rows]


async def claim_job(conn: asyncpg.Connection, *, worker_id: str) -> Job | None:
    """Single-job convenience wrapper over `claim_jobs` (M0.2 call sites)."""
    jobs = await claim_jobs(conn, worker_id=worker_id, limit=1)
    return jobs[0] if jobs else None


async def reclaim_stale_jobs(
    conn: asyncpg.Connection, *, visibility_timeout_s: int, max_attempts: int
) -> list[Job]:
    """Sweep for 'running' jobs whose lock has outlived the visibility
    timeout — a worker that died mid-job must not strand its work forever.

    Deliberately a separate query from `claim_jobs`, not folded into it
    (docs/decisions.md, Correction 2): combining "claim pending" and "reclaim
    stale" in one query with a single `attempts` treatment either never
    increments `attempts` for the reclaim case — letting a job whose worker
    keeps dying be reclaimed forever and never dead-letter — or conflates two
    operationally distinct events into one query that's harder to reason
    about and test in isolation. This sweep increments `attempts` and decides
    pending-vs-dead-letter in the same statement, so a repeatedly-crashing
    job's job reaches `max_attempts` and stops being reclaimed.

    Returns every reclaimed job, including ones that landed in
    'dead_letter' — the caller (core/queue.py) is responsible for emitting
    `job.dead_lettered` for those; this function only persists.
    """
    rows = await conn.fetch(
        """
        UPDATE jobs
        SET status = CASE WHEN attempts + 1 >= $2 THEN 'dead_letter' ELSE 'pending' END,
            attempts = attempts + 1,
            locked_by = NULL,
            locked_at = NULL,
            updated_at = now(),
            last_error = 'reclaimed: worker lock exceeded visibility timeout'
        WHERE id IN (
            SELECT id FROM jobs
            WHERE status = 'running' AND locked_at < now() - $1::interval
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        timedelta(seconds=visibility_timeout_s),
        max_attempts,
    )
    return [_row_to_job(row) for row in rows]


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
# Agent runs (core/llm.py::complete_json() — M0.4)
# ============================================================================


async def insert_agent_run(
    conn: asyncpg.Connection,
    *,
    agent: str,
    trigger_event: UUID | None,
    trace_id: str | None,
    prompt_id: str,
    prompt_version: int,
    tier: Tier,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cost: Decimal | None,
    latency_ms: int | None,
    status: AgentRunStatus,
    error: str | None,
    retry_count: int,
) -> AgentRun:
    """Insert one agent_runs row. Always a plain INSERT, never an upsert —
    each complete_json() call (success or failure) is its own row; there is
    no natural key to dedupe on and none is wanted (docs/decisions.md)."""
    row = await conn.fetchrow(
        """
        INSERT INTO agent_runs (agent, trigger_event, trace_id, prompt_id, prompt_version,
                                 tier, model, input_tokens, output_tokens, cost, latency_ms,
                                 status, error, retry_count)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        RETURNING *
        """,
        agent,
        trigger_event,
        trace_id,
        prompt_id,
        prompt_version,
        tier.value,
        model,
        input_tokens,
        output_tokens,
        cost,
        latency_ms,
        status.value,
        error,
        retry_count,
    )
    assert row is not None
    return _row_to_agent_run(row)


async def get_agent_run(conn: asyncpg.Connection, run_id: UUID) -> AgentRun | None:
    row = await conn.fetchrow("SELECT * FROM agent_runs WHERE id = $1", run_id)
    return _row_to_agent_run(row) if row else None


async def get_latest_agent_run(
    conn: asyncpg.Connection,
    *,
    agent: str,
    prompt_id: str,
    trigger_event: UUID | None,
    status: AgentRunStatus,
) -> AgentRun | None:
    """Recovers the agent_runs.id that a just-completed core/llm.py::
    complete_json() call wrote, since complete_json() itself returns only the
    parsed output dict (M1.1, docs/decisions.md — chosen over changing
    complete_json()'s return contract).

    Safe to call immediately after a `complete_json()` call in the same
    handler because each `(agent, prompt_id, trigger_event)` is written by at
    most one in-flight call at a time: jobs are claimed exclusively (SELECT
    ... FOR UPDATE SKIP LOCKED, core/queue.py) and `trigger_event` is the
    causation_id of the event that triggered this specific job, so no other
    worker is writing a row with the same three values concurrently.
    `ORDER BY created_at DESC LIMIT 1` also makes this correct across a
    crash-and-retry (a reclaimed job re-running the same call would insert a
    second row for the same trigger_event; this always returns the most
    recent one, which is the caller's own)."""
    row = await conn.fetchrow(
        """
        SELECT * FROM agent_runs
        WHERE agent = $1 AND prompt_id = $2
          AND trigger_event IS NOT DISTINCT FROM $3
          AND status = $4
        ORDER BY created_at DESC
        LIMIT 1
        """,
        agent,
        prompt_id,
        trigger_event,
        status.value,
    )
    return _row_to_agent_run(row) if row else None


# ============================================================================
# Approvals (M1.3) — the human-in-the-loop gate, CLAUDE.md §1 non-negotiable 8.
# ============================================================================


async def insert_approval(
    conn: asyncpg.Connection,
    *,
    action_type: ActionType,
    payload: dict[str, Any],
    requested_by_agent: str | None,
    token: str,
    expires_at: datetime | None,
    correlation_id: UUID | None,
    causation_id: UUID | None,
    dedupe_key: str | None,
) -> Approval:
    """Insert a new `pending` approval, idempotent on `dedupe_key`: a second
    request for the same still-pending logical action (e.g. a retried job
    re-requesting approval for the same draft) returns the EXISTING row
    rather than creating a duplicate or raising — the same idempotency
    posture as `emit_event`'s idempotency_key and `create_lead`'s deferred-row
    handling (both already established in this codebase), not a new pattern.
    `dedupe_key IS NULL` always inserts a fresh row: migrations/0005's
    partial unique index excludes NULL, so there is nothing to conflict on.
    """
    if dedupe_key is not None:
        row = await conn.fetchrow(
            """
            INSERT INTO approvals (action_type, payload, requested_by_agent, status, token,
                                    expires_at, correlation_id, causation_id, dedupe_key)
            VALUES ($1, $2::jsonb, $3, 'pending', $4, $5, $6, $7, $8)
            ON CONFLICT (dedupe_key) WHERE status = 'pending' AND dedupe_key IS NOT NULL
            DO NOTHING
            RETURNING *
            """,
            action_type.value,
            _dump_json(payload),
            requested_by_agent,
            token,
            expires_at,
            correlation_id,
            causation_id,
            dedupe_key,
        )
        if row is not None:
            return _row_to_approval(row)
        existing = await conn.fetchrow(
            "SELECT * FROM approvals WHERE dedupe_key = $1 AND status = 'pending'", dedupe_key
        )
        assert existing is not None
        return _row_to_approval(existing)

    row = await conn.fetchrow(
        """
        INSERT INTO approvals (action_type, payload, requested_by_agent, status, token,
                                expires_at, correlation_id, causation_id, dedupe_key)
        VALUES ($1, $2::jsonb, $3, 'pending', $4, $5, $6, $7, NULL)
        RETURNING *
        """,
        action_type.value,
        _dump_json(payload),
        requested_by_agent,
        token,
        expires_at,
        correlation_id,
        causation_id,
    )
    assert row is not None
    return _row_to_approval(row)


async def get_approval(conn: asyncpg.Connection, approval_id: UUID) -> Approval | None:
    row = await conn.fetchrow("SELECT * FROM approvals WHERE id = $1", approval_id)
    return _row_to_approval(row) if row else None


async def resolve_approval(
    conn: asyncpg.Connection,
    approval_id: UUID,
    *,
    status: ApprovalStatus,
    decided_by: str,
    decision_reason: str | None,
) -> Approval | None:
    """`status` must be GRANTED or DENIED — expiry has its own function below,
    since it's system-driven, not human-driven, and carries a different
    `decided_by`. Returns None if `approval_id` doesn't exist or is no longer
    `pending` (already decided) — the `WHERE status = 'pending'` guard is
    what makes a second resolve() attempt a clean no-op instead of a
    re-decision; migrations/0005's `approvals_forbid_redecision` trigger is
    the backstop for any write path that skips this guard."""
    row = await conn.fetchrow(
        """
        UPDATE approvals
        SET status = $2, decided_by = $3, decision_reason = $4, decided_at = now()
        WHERE id = $1 AND status = 'pending'
        RETURNING *
        """,
        approval_id,
        status.value,
        decided_by,
        decision_reason,
    )
    return _row_to_approval(row) if row else None


async def expire_cancel_approval(
    conn: asyncpg.Connection, approval_id: UUID, *, reason: str
) -> Approval | None:
    """The `on_expiry: cancel` branch of event-catalog.md §7.1: a terminal
    transition to `expired`, `decided_by='system:expiry'`. The
    `on_expiry: escalate` branch never calls this — an escalated approval
    stays `pending` (core/approvals.py::expire_stale)."""
    row = await conn.fetchrow(
        """
        UPDATE approvals
        SET status = 'expired', decided_by = 'system:expiry', decision_reason = $2,
            decided_at = now()
        WHERE id = $1 AND status = 'pending'
        RETURNING *
        """,
        approval_id,
        reason,
    )
    return _row_to_approval(row) if row else None


async def get_pending_approvals_older_than(
    conn: asyncpg.Connection, *, action_type: ActionType, cutoff: datetime
) -> list[Approval]:
    """Pending rows of one `action_type` requested before `cutoff` — the
    caller (core/approvals.py::expire_stale) computes `cutoff` from
    `thresholds.yaml`'s per-action-type TTL, so this stays a plain
    comparison, no interval arithmetic in Python call sites."""
    rows = await conn.fetch(
        "SELECT * FROM approvals WHERE status = 'pending' AND action_type = $1 AND created_at < $2",
        action_type.value,
        cutoff,
    )
    return [_row_to_approval(row) for row in rows]


# ============================================================================
# Outreach messages, suppressions, sending pauses (M1.4a) — the send path.
# Every function the send gate (core/sending.py) reads is here, and each one
# reads live state: nothing is cached between a draft and a send.
# ============================================================================


async def insert_outbound_draft(
    conn: asyncpg.Connection,
    *,
    lead_id: UUID,
    contact_id: UUID,
    campaign_id: UUID | None,
    subject: str,
    body_text: str,
    sequence_step: int,
    prompt_version: int,
    from_address: str,
    to_address: str,
) -> Message:
    row = await conn.fetchrow(
        """
        INSERT INTO messages (lead_id, contact_id, campaign_id, direction, channel, subject,
                              body_text, sequence_step, prompt_version, from_address,
                              to_address, send_state)
        VALUES ($1, $2, $3, 'outbound', 'email', $4, $5, $6, $7, $8, $9, 'drafted')
        RETURNING *
        """,
        lead_id,
        contact_id,
        campaign_id,
        subject,
        body_text,
        sequence_step,
        prompt_version,
        from_address,
        to_address,
    )
    assert row is not None
    return _row_to_message(row)


async def get_message(conn: asyncpg.Connection, message_id: UUID) -> Message | None:
    row = await conn.fetchrow("SELECT * FROM messages WHERE id = $1", message_id)
    return _row_to_message(row) if row else None


async def get_outbound_message_for_lead_step(
    conn: asyncpg.Connection, *, lead_id: UUID, sequence_step: int
) -> Message | None:
    """The existing outbound message for this lead and step, if any — the
    draft handler's idempotency check (a redelivered lead.qualified.sql must
    not draft a second email)."""
    row = await conn.fetchrow(
        """
        SELECT * FROM messages
        WHERE lead_id = $1 AND sequence_step = $2 AND direction = 'outbound'
          AND deleted_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        lead_id,
        sequence_step,
    )
    return _row_to_message(row) if row else None


async def set_message_approval(
    conn: asyncpg.Connection, message_id: UUID, approval_id: UUID
) -> None:
    await conn.execute(
        "UPDATE messages SET approval_id = $2, updated_at = now() WHERE id = $1",
        message_id,
        approval_id,
    )


async def lock_sending_domain(conn: asyncpg.Connection, sending_domain: str) -> None:
    """Transaction-scoped advisory lock serialising gate evaluation +
    reservation per sending domain: without it, two workers could both read
    4 sends against a cap of 5 and both send. Released at commit/rollback."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", sending_domain.lower()
    )


async def count_sends_since(
    conn: asyncpg.Connection, *, sending_domain: str, since: datetime
) -> int:
    """Every outbound message reserved or sent on the domain since `since` —
    first touches and follow-ups alike (deliverability.md §4: "The cap counts
    every outbound message on the domain"). A reservation ('sending') and an
    ambiguous 'send_unknown' both count: either may have been delivered."""
    count = await conn.fetchval(
        """
        SELECT count(*) FROM messages
        WHERE direction = 'outbound'
          AND send_state IN ('sending', 'sent', 'send_unknown')
          AND lower(split_part(from_address::text, '@', 2)) = lower($1)
          AND send_started_at >= $2
        """,
        sending_domain,
        since,
    )
    return int(count)


async def get_last_send_started_at(
    conn: asyncpg.Connection, *, sending_domain: str
) -> datetime | None:
    value: datetime | None = await conn.fetchval(
        """
        SELECT max(send_started_at) FROM messages
        WHERE direction = 'outbound'
          AND send_state IN ('sending', 'sent', 'send_unknown')
          AND lower(split_part(from_address::text, '@', 2)) = lower($1)
        """,
        sending_domain,
    )
    return value


async def find_active_suppressions(
    conn: asyncpg.Connection, *, address: str, domain: str, now: datetime
) -> list[Suppression]:
    """Address-level rows for `address`, plus domain-wide rows (address IS
    NULL) for `domain`. Expired temporary suppressions are excluded."""
    rows = await conn.fetch(
        """
        SELECT * FROM suppressions
        WHERE (address = $1 OR (address IS NULL AND domain = $2))
          AND (expires_at IS NULL OR expires_at > $3)
        ORDER BY created_at
        """,
        address,
        domain,
        now,
    )
    return [_row_to_suppression(row) for row in rows]


async def insert_suppression(
    conn: asyncpg.Connection,
    *,
    address: str | None,
    domain: str | None,
    reason: SuppressionReason,
    source: str,
    expires_at: datetime | None = None,
) -> Suppression:
    row = await conn.fetchrow(
        """
        INSERT INTO suppressions (address, domain, reason, source, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        address,
        domain,
        reason.value,
        source,
        expires_at,
    )
    assert row is not None
    return _row_to_suppression(row)


async def count_suppressions_since(
    conn: asyncpg.Connection, *, reason: SuppressionReason, since: datetime
) -> int:
    """Address-level suppressions only: one unsubscribe may also write a
    domain-wide row, and counting both would double-count one event in the
    health metrics."""
    count = await conn.fetchval(
        """
        SELECT count(*) FROM suppressions
        WHERE reason = $1 AND address IS NOT NULL AND created_at >= $2
        """,
        reason.value,
        since,
    )
    return int(count)


async def get_open_sending_pause(
    conn: asyncpg.Connection, *, sending_domain: str
) -> SendingPause | None:
    row = await conn.fetchrow(
        "SELECT * FROM sending_pauses WHERE sending_domain = $1 AND resumed_at IS NULL",
        sending_domain,
    )
    return _row_to_sending_pause(row) if row else None


async def open_sending_pause(
    conn: asyncpg.Connection, *, sending_domain: str, reason: str, metrics: dict[str, Any]
) -> tuple[SendingPause, bool]:
    """Returns (pause, created). Idempotent on the one-open-pause-per-domain
    index: a concurrent or repeated breach returns the existing open pause."""
    row = await conn.fetchrow(
        """
        INSERT INTO sending_pauses (sending_domain, reason, metrics)
        VALUES ($1, $2, $3::jsonb)
        ON CONFLICT (sending_domain) WHERE resumed_at IS NULL DO NOTHING
        RETURNING *
        """,
        sending_domain,
        reason,
        _dump_json(metrics),
    )
    if row is not None:
        return _row_to_sending_pause(row), True
    existing = await get_open_sending_pause(conn, sending_domain=sending_domain)
    assert existing is not None
    return existing, False


async def resume_sending_pause(
    conn: asyncpg.Connection, *, sending_domain: str, resumed_by: str, resume_reason: str
) -> SendingPause | None:
    row = await conn.fetchrow(
        """
        UPDATE sending_pauses
        SET resumed_at = now(), resumed_by = $2, resume_reason = $3
        WHERE sending_domain = $1 AND resumed_at IS NULL
        RETURNING *
        """,
        sending_domain,
        resumed_by,
        resume_reason,
    )
    return _row_to_sending_pause(row) if row else None


async def reserve_message_for_send(
    conn: asyncpg.Connection, message_id: UUID, *, now: datetime
) -> Message | None:
    """drafted -> sending. None if the row is no longer 'drafted' — the
    single transition that makes a second send of one message impossible."""
    row = await conn.fetchrow(
        """
        UPDATE messages SET send_state = 'sending', send_started_at = $2, updated_at = now()
        WHERE id = $1 AND send_state = 'drafted'
        RETURNING *
        """,
        message_id,
        now,
    )
    return _row_to_message(row) if row else None


async def block_message(conn: asyncpg.Connection, message_id: UUID, *, reason: str) -> None:
    await conn.execute(
        """
        UPDATE messages SET send_state = 'blocked', send_block_reason = $2, updated_at = now()
        WHERE id = $1 AND send_state = 'drafted'
        """,
        message_id,
        reason,
    )


async def mark_message_sent(
    conn: asyncpg.Connection,
    message_id: UUID,
    *,
    provider_message_id: str,
    thread_id: str | None,
    sent_at: datetime,
) -> Message | None:
    row = await conn.fetchrow(
        """
        UPDATE messages
        SET send_state = 'sent', provider_message_id = $2, thread_id = $3, sent_at = $4,
            updated_at = now()
        WHERE id = $1 AND send_state = 'sending'
        RETURNING *
        """,
        message_id,
        provider_message_id,
        thread_id,
        sent_at,
    )
    return _row_to_message(row) if row else None


async def mark_message_send_outcome(
    conn: asyncpg.Connection, message_id: UUID, *, state: SendState, reason: str
) -> None:
    """sending -> send_failed | send_unknown."""
    if state not in (SendState.SEND_FAILED, SendState.SEND_UNKNOWN):
        raise ValueError(f"not a send outcome state: {state}")
    await conn.execute(
        """
        UPDATE messages SET send_state = $2, send_block_reason = $3, updated_at = now()
        WHERE id = $1 AND send_state = 'sending'
        """,
        message_id,
        state.value,
        reason,
    )


async def list_held_message_ids(conn: asyncpg.Connection, *, send_job_type: str) -> list[UUID]:
    """Approved drafts still in 'drafted' with no pending/running send job —
    the messages a health pause (or a sandbox/config fix) left held.
    scripts/resume_sending.py re-enqueues exactly these."""
    rows = await conn.fetch(
        """
        SELECT m.id FROM messages m
        JOIN approvals a ON a.id = m.approval_id
        WHERE m.direction = 'outbound' AND m.send_state = 'drafted' AND a.status = 'granted'
          AND NOT EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.type = $1 AND j.status IN ('pending', 'running')
                AND j.payload->>'message_id' = m.id::text
          )
        ORDER BY m.created_at
        """,
        send_job_type,
    )
    return [row["id"] for row in rows]


async def mark_lead_touched(conn: asyncpg.Connection, lead_id: UUID, *, at: datetime) -> None:
    await conn.execute(
        """
        UPDATE leads
        SET first_touched_at = COALESCE(first_touched_at, $2), last_activity_at = $2,
            updated_at = now()
        WHERE id = $1
        """,
        lead_id,
        at,
    )


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
        profile=json.loads(row["profile"]) if row["profile"] is not None else None,
        first_touched_at=row["first_touched_at"],
        last_activity_at=row["last_activity_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _row_to_lead_score(row: asyncpg.Record) -> LeadScore:
    return LeadScore(
        id=row["id"],
        lead_id=row["lead_id"],
        total=row["total"],
        band=LeadBand(row["band"]),
        components=json.loads(row["components"]),
        deterministic_part=row["deterministic_part"],
        llm_part=row["llm_part"],
        prompt_version=row["prompt_version"],
        model=row["model"],
        run_id=row["run_id"],
        scored_at=row["scored_at"],
    )


def _row_to_approval(row: asyncpg.Record) -> Approval:
    return Approval(
        id=row["id"],
        action_type=ActionType(row["action_type"]),
        payload=json.loads(row["payload"]),
        requested_by_agent=row["requested_by_agent"],
        status=ApprovalStatus(row["status"]),
        decided_by=row["decided_by"],
        token=row["token"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        decision_reason=row["decision_reason"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        dedupe_key=row["dedupe_key"],
        created_at=row["created_at"],
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


def _row_to_agent_run(row: asyncpg.Record) -> AgentRun:
    return AgentRun(
        id=row["id"],
        agent=row["agent"],
        trigger_event=row["trigger_event"],
        trace_id=row["trace_id"],
        prompt_id=row["prompt_id"],
        prompt_version=row["prompt_version"],
        tier=Tier(row["tier"]),
        model=row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost=row["cost"],
        latency_ms=row["latency_ms"],
        status=AgentRunStatus(row["status"]),
        error=row["error"],
        retry_count=row["retry_count"],
        created_at=row["created_at"],
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


def _row_to_message(row: asyncpg.Record) -> Message:
    return Message(
        id=row["id"],
        lead_id=row["lead_id"],
        contact_id=row["contact_id"],
        campaign_id=row["campaign_id"],
        direction=row["direction"],
        channel=row["channel"],
        provider_message_id=row["provider_message_id"],
        thread_id=row["thread_id"],
        subject=row["subject"],
        body_text=row["body_text"],
        sequence_step=row["sequence_step"],
        prompt_version=row["prompt_version"],
        approval_id=row["approval_id"],
        from_address=row["from_address"],
        to_address=row["to_address"],
        send_state=SendState(row["send_state"]) if row["send_state"] else None,
        send_started_at=row["send_started_at"],
        send_block_reason=row["send_block_reason"],
        sent_at=row["sent_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_suppression(row: asyncpg.Record) -> Suppression:
    return Suppression(
        id=row["id"],
        address=row["address"],
        domain=row["domain"],
        reason=SuppressionReason(row["reason"]),
        source=row["source"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def _row_to_sending_pause(row: asyncpg.Record) -> SendingPause:
    return SendingPause(
        id=row["id"],
        sending_domain=row["sending_domain"],
        reason=row["reason"],
        metrics=json.loads(row["metrics"]),
        paused_at=row["paused_at"],
        resumed_at=row["resumed_at"],
        resumed_by=row["resumed_by"],
        resume_reason=row["resume_reason"],
    )
