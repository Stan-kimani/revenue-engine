"""Integration tests for the event -> job dispatch cycle (scripts/run_worker.py
+ core/events.py + core/queue.py + orchestrator/router.py) against a real
Postgres instance.

scripts/run_worker.py is not part of the src/ package, so it is loaded
directly by file path, same as scripts/migrate.py in tests/integration/test_migrate.py.

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest

from revenue_engine.core import events as core_events
from revenue_engine.core import queue as core_queue
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import JobStatus
from revenue_engine.orchestrator import router

pytestmark = pytest.mark.integration

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py"


def _load_run_worker_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_worker_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_worker = _load_run_worker_module()


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set (checked .env and the shell environment)")
    return url


@pytest.fixture
async def conn(database_url: str):
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute("TRUNCATE events, jobs RESTART IDENTITY CASCADE")
        await connection.close()


def _valid_lead_captured_payload() -> dict:
    return {
        "lead_id": str(uuid.uuid4()),
        "contact_id": str(uuid.uuid4()),
        "company_id": None,
        "campaign_id": None,
        "source": "discovery",
        "industry_pack": "b2b-service-firms",
    }


@pytest.mark.protected
async def test_event_never_marked_processed_unless_jobs_were_enqueued(
    conn: asyncpg.Connection,
):
    """Simulates a crash mid-transaction, after the job insert but before
    mark_event_processed — the exact sequence correctness requirement (a)
    protects against. Everything must roll back together: the event stays
    unprocessed AND no orphan job exists, not one or the other.
    """
    event = await core_events.emit(
        conn,
        type="lead.captured",  # has a real ROUTES entry -> leadgen.enrich
        payload=_valid_lead_captured_payload(),
        correlation_id=uuid.uuid4(),
        actor="test",
        idempotency_key=f"lead:{uuid.uuid4()}:captured",
    )

    class _SimulatedCrash(Exception):
        pass

    with pytest.raises(_SimulatedCrash):
        async with conn.transaction():
            claimed = await repo.claim_unprocessed_event(conn)
            assert claimed is not None
            assert claimed.event_id == event.event_id

            specs = router.route(claimed.type)
            assert specs, "test assumes lead.captured has a real route"
            for spec in specs:
                await core_queue.enqueue_for_event(
                    conn, event=claimed, job_type=spec.job_type, payload=dict(claimed.payload)
                )

            # Simulate the process dying here — after the job insert, before
            # mark_event_processed. Nothing after this line executes.
            raise _SimulatedCrash("simulated mid-transaction failure")

    # The connection is still usable after a rolled-back transaction — only
    # the transaction aborted, not the connection.
    reread_event = await repo.get_event(conn, event.event_id)
    assert reread_event is not None
    assert reread_event.processed_at is None

    orphan_jobs = await conn.fetch(
        "SELECT id FROM jobs WHERE (payload->>'source_event_id')::uuid = $1", event.event_id
    )
    assert orphan_jobs == []


@pytest.mark.protected
async def test_event_marked_processed_only_after_jobs_committed_together(
    conn: asyncpg.Connection,
):
    """The positive case, alongside the crash test above: a real, successful
    dispatch does commit the event-processed flag and its jobs together."""
    event = await core_events.emit(
        conn,
        type="lead.captured",
        payload=_valid_lead_captured_payload(),
        correlation_id=uuid.uuid4(),
        actor="test",
        idempotency_key=f"lead:{uuid.uuid4()}:captured",
    )

    processed = await run_worker.dispatch_one_event(_SinglePoolLike(conn))
    assert processed is True

    reread_event = await repo.get_event(conn, event.event_id)
    assert reread_event is not None
    assert reread_event.processed_at is not None

    jobs = await conn.fetch(
        "SELECT * FROM jobs WHERE (payload->>'source_event_id')::uuid = $1", event.event_id
    )
    assert len(jobs) == 1
    assert jobs[0]["type"] == "leadgen.enrich"


async def test_poison_job_does_not_block_other_jobs(conn: asyncpg.Connection):
    """A job with no registered handler (a poison job here — no agents exist
    in M0.3) must fail cleanly through the normal retry path, not crash the
    worker or prevent a different, healthy job from completing right after.
    """
    good_job = await repo.enqueue_job(conn, type="test.good", payload={})
    poison_job = await repo.enqueue_job(conn, type="test.unregistered_poison", payload={})

    claimed = await core_queue.claim(conn, worker_id="w1", limit=10)
    claimed_by_id = {j.id: j for j in claimed}
    assert good_job.id in claimed_by_id
    assert poison_job.id in claimed_by_id

    async def _succeed(_conn: asyncpg.Connection, _job) -> None:
        return None

    run_worker.HANDLERS["test.good"] = _succeed
    try:
        # Process the poison job first — if it crashed the worker, the good
        # job below would never run.
        await run_worker.process_one_job(conn, claimed_by_id[poison_job.id], "w1")
        await run_worker.process_one_job(conn, claimed_by_id[good_job.id], "w1")
    finally:
        del run_worker.HANDLERS["test.good"]

    refreshed_good = await repo.get_job(conn, good_job.id)
    refreshed_poison = await repo.get_job(conn, poison_job.id)
    assert refreshed_good is not None
    assert refreshed_poison is not None

    assert refreshed_good.status == JobStatus.COMPLETED
    assert refreshed_poison.status == JobStatus.PENDING  # first failure, retried
    assert refreshed_poison.attempts == 1
    assert refreshed_poison.last_error is not None
    assert "no handler registered" in refreshed_poison.last_error


class _SinglePoolLike:
    """dispatch_one_event() takes an asyncpg.Pool (async with pool.acquire()
    as conn). This adapts a single already-connected test connection to that
    same interface without opening a real pool, so the test can reuse the
    shared `conn` fixture (and its truncate-on-teardown) directly.
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)


class _AcquireContext:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None
