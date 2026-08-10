"""Worker process (M0.3): dispatches the event outbox to jobs, and executes
the job queue. No agents exist yet — HANDLERS starts empty; a claimed job
with no registered handler fails cleanly through the normal retry/dead-letter
path (a poison job) rather than crashing the worker.

Event dispatch is single-process, elected via a Postgres advisory lock
(Correction 1, docs/decisions.md): FOR UPDATE SKIP LOCKED does not guarantee
strict occurred_at order across concurrent claimers, which matters for events
sharing a correlation_id (lead.scored must not be routed before lead.enriched
for the same lead). Concurrency lives on the job queue instead, where it's
safe (each job is independent once claimed).

Usage: uv run python scripts/run_worker.py
Env: DATABASE_URL (required), WORKER_CONCURRENCY (default 4),
     WORKER_POLL_INTERVAL_S (default 2), WORKER_RECLAIM_INTERVAL_S (default 30).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg
from dotenv import load_dotenv

from revenue_engine.core import queue as core_queue
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import Job
from revenue_engine.orchestrator import router

# Fixed, arbitrary key for the single-event-dispatcher advisory lock. Session
# (connection) scoped — held on one dedicated connection for the dispatch
# loop's entire lifetime, not the connection dispatch_one_event() itself uses.
_EVENT_DISPATCH_LOCK_KEY = 72711583

JobHandler = Callable[[asyncpg.Connection, Job], Awaitable[None]]

# Empty in production — no agents exist until M1.1+ (build-spec §10). Tests
# populate their own entries to exercise claim/execute/complete/fail/dead-letter
# machinery end to end without needing a real agent.
HANDLERS: dict[str, JobHandler] = {}

_STRUCTURED_FIELDS = (
    "correlation_id",
    "event_id",
    "event_type",
    "job_id",
    "job_type",
    "worker_id",
)

log = logging.getLogger("revenue_engine.worker")


class _JsonFormatter(logging.Formatter):
    """Structured (JSON-lines) logging carrying correlation_id on every line
    that has one — the whole point of correlation_id (event-catalog.md §R3)
    is being able to grep one lead's full journey out of the worker log.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


# ============================================================================
# Event dispatch
# ============================================================================


async def dispatch_one_event(pool: asyncpg.Pool) -> bool:
    """One claim -> route -> enqueue -> mark-processed cycle, atomic in a
    single transaction on a single connection (correctness requirement (a),
    docs/decisions.md): an event is never marked processed unless every job
    it routes to was enqueued in the same commit. If the process dies
    anywhere in this block, Postgres rolls back the whole transaction — the
    event stays unprocessed and gets claimed again on the next poll, and
    core/queue.py::enqueue_for_event's dedup-on-(event_id, job_type) makes
    that retry safe even against a previous aborted attempt at this same
    event.

    Returns True if an event was processed, False if the outbox was empty.
    """
    async with pool.acquire() as conn, conn.transaction():
        event = await repo.claim_unprocessed_event(conn)
        if event is None:
            return False

        specs = router.route(event.type)
        if not specs and not router.is_covered(event.type):
            log.warning(
                "no ROUTES or UNCONSUMED entry for event type — routing table gap",
                extra={"event_id": str(event.event_id), "event_type": event.type},
            )

        for spec in specs:
            job = await core_queue.enqueue_for_event(
                conn, event=event, job_type=spec.job_type, payload=dict(event.payload)
            )
            log.info(
                "enqueued job for event",
                extra={
                    "correlation_id": str(event.correlation_id),
                    "event_id": str(event.event_id),
                    "event_type": event.type,
                    "job_id": str(job.id),
                    "job_type": spec.job_type,
                },
            )

        await repo.mark_event_processed(conn, event.event_id)
        log.info(
            "event processed",
            extra={
                "correlation_id": str(event.correlation_id),
                "event_id": str(event.event_id),
                "event_type": event.type,
            },
        )
    return True


async def run_event_dispatch_loop(
    pool: asyncpg.Pool, shutdown: asyncio.Event, poll_interval_s: float
) -> None:
    """Elects a single dispatcher via `pg_try_advisory_lock` — an enforced
    guarantee, not an operational promise. If two `run_worker.py` processes
    start, only the one that wins the lock ever dispatches events; the other
    logs that and returns immediately, leaving job execution (this process's
    other loop) unaffected.
    """
    lock_conn = await pool.acquire()
    try:
        acquired = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", _EVENT_DISPATCH_LOCK_KEY
        )
        if not acquired:
            log.info("event dispatch lock held by another process; not dispatching here")
            return

        log.info("acquired event dispatch lock")
        try:
            while not shutdown.is_set():
                processed = await dispatch_one_event(pool)
                if not processed:
                    await _wait_or_shutdown(shutdown, poll_interval_s)
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", _EVENT_DISPATCH_LOCK_KEY)
    finally:
        await pool.release(lock_conn)


# ============================================================================
# Job execution
# ============================================================================


async def process_one_job(conn: asyncpg.Connection, job: Job, worker_id: str) -> None:
    correlation_id = job.payload.get("correlation_id", str(job.id))
    extra = {
        "correlation_id": correlation_id,
        "job_id": str(job.id),
        "job_type": job.type,
        "worker_id": worker_id,
    }

    handler = HANDLERS.get(job.type)
    if handler is None:
        # Poison-job-safe (correctness requirement (c)): an unregistered job
        # type fails through the normal retry/dead-letter path instead of
        # crashing the worker or blocking any other job behind it.
        log.warning("no handler registered for job type", extra=extra)
        await core_queue.fail(
            conn, job.id, error=f"no handler registered for job type '{job.type}'"
        )
        return

    try:
        await handler(conn, job)
    except Exception as exc:  # noqa: BLE001 - a poison job must not crash the worker
        log.exception("job handler raised", extra=extra)
        await core_queue.fail(conn, job.id, error=str(exc))
        return

    await core_queue.complete(conn, job.id)
    log.info("job completed", extra=extra)


async def run_job_loop(
    pool: asyncpg.Pool,
    worker_id: str,
    shutdown: asyncio.Event,
    poll_interval_s: float,
    concurrency: int,
) -> None:
    while not shutdown.is_set():
        async with pool.acquire() as conn, conn.transaction():
            jobs = await core_queue.claim(conn, worker_id=worker_id, limit=concurrency)

        if not jobs:
            await _wait_or_shutdown(shutdown, poll_interval_s)
            continue

        async def _run(job: Job) -> None:
            async with pool.acquire() as job_conn:
                await process_one_job(job_conn, job, worker_id)

        # Graceful shutdown (SIGTERM/SIGINT): the loop condition is only
        # re-checked after this gather completes — an in-flight batch is
        # always finished, never cancelled mid-job, and each job's lock is
        # released by process_one_job's own complete()/fail() call before
        # this function returns.
        await asyncio.gather(*(_run(j) for j in jobs))


async def run_reclaim_loop(
    pool: asyncpg.Pool, shutdown: asyncio.Event, worker_id: str, interval_s: float
) -> None:
    while not shutdown.is_set():
        async with pool.acquire() as conn, conn.transaction():
            reclaimed = await core_queue.reclaim_stale(conn)
        if reclaimed:
            log.info(
                "reclaimed stale job(s)",
                extra={"worker_id": worker_id},
            )
            for job in reclaimed:
                log.info(
                    "reclaimed job",
                    extra={"job_id": str(job.id), "job_type": job.type, "worker_id": worker_id},
                )
        await _wait_or_shutdown(shutdown, interval_s)


async def _wait_or_shutdown(shutdown: asyncio.Event, timeout_s: float) -> None:
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=timeout_s)
    except TimeoutError:
        pass


# ============================================================================
# Entry point
# ============================================================================


async def main() -> int:
    load_dotenv()
    configure_logging()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL is not set")
        return 1

    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    concurrency = int(os.environ.get("WORKER_CONCURRENCY", "4"))
    poll_interval_s = float(os.environ.get("WORKER_POLL_INTERVAL_S", "2"))
    reclaim_interval_s = float(os.environ.get("WORKER_RECLAIM_INTERVAL_S", "30"))

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=concurrency + 2)
    shutdown = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows's default proactor
            # event loop; fall back to the classic signal module. Production
            # runs on Linux (build-spec §7), where add_signal_handler works.
            signal.signal(sig, lambda *_: shutdown.set())

    log.info("worker starting", extra={"worker_id": worker_id})
    try:
        await asyncio.gather(
            run_event_dispatch_loop(pool, shutdown, poll_interval_s),
            run_job_loop(pool, worker_id, shutdown, poll_interval_s, concurrency),
            run_reclaim_loop(pool, shutdown, worker_id, reclaim_interval_s),
        )
    finally:
        await pool.close()
        log.info("worker stopped", extra={"worker_id": worker_id})

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
