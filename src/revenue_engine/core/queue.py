"""Job queue mechanics — the imperative "do this" side of the events/jobs
split (event-catalog.md §1). Jobs are consumed exactly once (SELECT ... FOR
UPDATE SKIP LOCKED), retried with bounded jittered backoff, and eventually
dead-lettered. Events (core/events.py) are immutable facts, never retried.

All SQL stays in db/repositories.py (CLAUDE.md §4); this module adds backoff
policy, dead-letter decisions, and config on top of the repository
primitives.

Handler idempotency: this module guarantees a job is never held by two
workers at once (the SKIP LOCKED claim), and that a claimed-then-crashed job
is eventually reclaimed (`reclaim_stale`) — it does NOT make a handler's
*body* idempotent. A job can be claimed, partially executed, and then
reclaimed after a crash and run again from the start. Handler code must be
safe to re-run (upserts on natural keys, emit() with a deterministic
idempotency_key — never a blind INSERT), same as every M0.2 repository
function already is.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from ..db import repositories as repo
from ..db.models import Event, Job, JobStatus
from . import events as core_events
from .config import QueueConfig, get_config
from .errors import RevenueEngineError


def _compute_backoff_s(attempts: int, config: QueueConfig) -> float:
    """Full jitter: min(cap, base * 2^attempts) * uniform(0.5, 1.0).

    Bounded (capped) and jittered (not naive doubling) so a burst of jobs
    failing at the same moment — e.g. a downstream API outage — don't all
    retry in lockstep and hammer it again the instant it recovers.
    """
    # `2**attempts` types as Any in typeshed (int.__pow__ falls back to Any
    # for a non-literal exponent, to allow for negative exponents returning
    # float) — the explicit float() stops that Any from propagating out.
    exponential = config.backoff_base_s * float(2**attempts)
    capped: float = min(config.backoff_cap_s, exponential)
    return capped * (0.5 + random.random() * 0.5)


def _job_correlation_id(job: Job) -> UUID:
    """Jobs enqueued from an event carry the event's correlation_id in their
    payload (see enqueue_for_event) so the chain stays traceable through a
    dead-letter. Jobs enqueued via the generic enqueue() have none — fall
    back to the job's own id rather than leaving it unset."""
    raw = job.payload.get("correlation_id")
    return UUID(raw) if raw else job.id


async def enqueue(
    conn: asyncpg.Connection,
    *,
    job_type: str,
    payload: dict[str, Any],
    run_after: datetime | None = None,
) -> Job:
    return await repo.enqueue_job(conn, type=job_type, payload=payload, run_after=run_after)


async def enqueue_for_event(
    conn: asyncpg.Connection, *, event: Event, job_type: str, payload: dict[str, Any]
) -> Job:
    """Enqueue a job produced by routing one event, deterministically
    deduplicated on (event_id, job_type) — an upsert (check for an existing
    job first, insert only if absent), never a blind insert.

    This is what makes re-processing the same event safe: if the dispatch
    transaction (claim event -> route -> enqueue jobs -> mark processed,
    scripts/run_worker.py) is retried after a crash before it committed, the
    event is still unprocessed and gets claimed again — this function then
    finds the job(s) that attempt already tried to create instead of
    duplicating them. Both `source_event_id` and the event's `correlation_id`
    are embedded in the job's payload — the former is the dedup key, the
    latter keeps the correlation chain traceable through to a dead-letter.
    """
    existing = await repo.get_job_by_source_event(
        conn, job_type=job_type, source_event_id=event.event_id
    )
    if existing is not None:
        return existing
    return await repo.enqueue_job(
        conn,
        type=job_type,
        payload={
            **payload,
            "source_event_id": str(event.event_id),
            "correlation_id": str(event.correlation_id),
        },
    )


async def claim(conn: asyncpg.Connection, *, worker_id: str, limit: int) -> list[Job]:
    return await repo.claim_jobs(conn, worker_id=worker_id, limit=limit)


async def complete(conn: asyncpg.Connection, job_id: UUID) -> Job:
    return await repo.complete_job(conn, job_id)


async def fail(conn: asyncpg.Connection, job_id: UUID, *, error: str) -> Job:
    """Record a failure. Dead-letters (and emits `job.dead_lettered`) once
    the attempt about to be recorded reaches `max_attempts`; otherwise
    schedules a bounded, jittered retry.
    """
    job = await repo.get_job(conn, job_id)
    if job is None:
        raise RevenueEngineError(f"Job not found: {job_id}")

    config = get_config().queue
    next_attempts = job.attempts + 1

    if next_attempts >= config.max_attempts:
        dead_lettered = await repo.mark_job_failed(conn, job_id, error=error, retry_after=None)
        await _emit_dead_lettered(conn, dead_lettered, error)
        return dead_lettered

    delay = _compute_backoff_s(job.attempts, config)
    retry_after = datetime.now(UTC) + timedelta(seconds=delay)
    return await repo.mark_job_failed(conn, job_id, error=error, retry_after=retry_after)


async def reclaim_stale(conn: asyncpg.Connection) -> list[Job]:
    """Sweep for jobs whose worker died mid-run (locked_at older than the
    visibility timeout) — a worker that dies must not strand its work
    forever. Dead-letters any that reached max_attempts as part of the same
    sweep (repositories.reclaim_stale_jobs increments attempts there, not
    here — see docs/decisions.md, Correction 2); this function's job is only
    to emit `job.dead_lettered` for whichever ones did, since the repository
    layer never emits events (CLAUDE.md separation).
    """
    config = get_config().queue
    reclaimed = await repo.reclaim_stale_jobs(
        conn,
        visibility_timeout_s=config.visibility_timeout_s,
        max_attempts=config.max_attempts,
    )
    for job in reclaimed:
        if job.status == JobStatus.DEAD_LETTER:
            await _emit_dead_lettered(
                conn, job, job.last_error or "reclaimed: worker lock exceeded visibility timeout"
            )
    return reclaimed


async def _emit_dead_lettered(conn: asyncpg.Connection, job: Job, error: str) -> None:
    await core_events.emit(
        conn,
        type="job.dead_lettered",
        payload={
            "job_id": str(job.id),
            "job_type": job.type,
            "attempts": job.attempts,
            "last_error": error,
        },
        correlation_id=_job_correlation_id(job),
        actor="core.queue",
        # Idempotency key includes attempts: a job can only dead-letter once
        # per attempts-count, and attempts never decreases, so this can never
        # collide across two genuinely different dead-letter events.
        idempotency_key=f"job:{job.id}:dead_lettered:{job.attempts}",
    )
