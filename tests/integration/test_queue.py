"""Integration tests for core/queue.py against a real Postgres instance.

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from revenue_engine.core import queue as core_queue
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import JobStatus

pytestmark = pytest.mark.integration

# database_url fixture comes from tests/integration/conftest.py (TEST_DATABASE_URL,
# never DATABASE_URL — see docs/decisions.md).


@pytest.fixture
async def conn(database_url: str):
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute("TRUNCATE jobs, events RESTART IDENTITY CASCADE")
        await connection.close()


async def test_job_failing_max_attempts_dead_letters_and_emits_event(conn: asyncpg.Connection):
    config = core_queue._load_config()
    job = await repo.enqueue_job(conn, type="test.always_fails", payload={})

    result = job
    for i in range(config.max_attempts - 1):
        result = await core_queue.fail(conn, job.id, error=f"attempt {i}")
        assert result.status == JobStatus.PENDING

    final = await core_queue.fail(conn, job.id, error="final attempt")
    assert final.status == JobStatus.DEAD_LETTER
    assert final.attempts == config.max_attempts

    event = await conn.fetchrow(
        "SELECT * FROM events WHERE type = 'job.dead_lettered' AND payload->>'job_id' = $1",
        str(job.id),
    )
    assert event is not None


async def test_stale_locked_job_is_reclaimed_after_visibility_timeout(conn: asyncpg.Connection):
    job = await repo.enqueue_job(conn, type="test.stale", payload={})
    await repo.claim_job(conn, worker_id="dead-worker")

    # Simulate a worker that died: backdate the lock well past the configured
    # visibility timeout (config/base.yaml: 300s).
    await conn.execute(
        "UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = $1", job.id
    )

    reclaimed = await core_queue.reclaim_stale(conn)
    reclaimed_ids = {j.id for j in reclaimed}
    assert job.id in reclaimed_ids

    refreshed = await repo.get_job(conn, job.id)
    assert refreshed is not None
    assert refreshed.attempts == 1
    assert refreshed.status in (JobStatus.PENDING, JobStatus.DEAD_LETTER)
    assert refreshed.locked_by is None
    assert refreshed.locked_at is None


async def test_stale_job_reclaimed_repeatedly_eventually_dead_letters(conn: asyncpg.Connection):
    """The bug Correction 2 fixed: a job whose worker keeps dying must
    eventually dead-letter, not be reclaimed forever with attempts stuck."""
    config = core_queue._load_config()
    job = await repo.enqueue_job(conn, type="test.chronically_stale", payload={})

    for _ in range(config.max_attempts):
        await repo.claim_job(conn, worker_id="dead-worker")
        await conn.execute(
            "UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = $1", job.id
        )
        await core_queue.reclaim_stale(conn)
        refreshed = await repo.get_job(conn, job.id)
        assert refreshed is not None
        if refreshed.status == JobStatus.DEAD_LETTER:
            break
        # still pending — must be claimable again for the next iteration
        assert refreshed.status == JobStatus.PENDING

    final = await repo.get_job(conn, job.id)
    assert final is not None
    assert final.status == JobStatus.DEAD_LETTER
    assert final.attempts == config.max_attempts


@pytest.mark.protected
async def test_two_concurrent_workers_never_claim_the_same_job(database_url: str):
    """Two real, independent connections claiming concurrently — not a mock.
    A mocked version of this test would only prove the mock behaves as
    programmed, not that SELECT ... FOR UPDATE SKIP LOCKED actually prevents
    double-claiming under real concurrent access.
    """
    setup_conn = await asyncpg.connect(database_url)
    try:
        await setup_conn.execute("TRUNCATE jobs RESTART IDENTITY CASCADE")
        job_ids = set()
        for i in range(20):
            job = await repo.enqueue_job(setup_conn, type="test.concurrent", payload={"i": i})
            job_ids.add(job.id)
    finally:
        await setup_conn.close()

    conn_a = await asyncpg.connect(database_url)
    conn_b = await asyncpg.connect(database_url)
    try:

        async def claim_and_commit(conn: asyncpg.Connection, worker_id: str):
            async with conn.transaction():
                return await repo.claim_jobs(conn, worker_id=worker_id, limit=15)

        claimed_a, claimed_b = await asyncio.gather(
            claim_and_commit(conn_a, "worker-a"),
            claim_and_commit(conn_b, "worker-b"),
        )

        ids_a = {j.id for j in claimed_a}
        ids_b = {j.id for j in claimed_b}

        assert ids_a.isdisjoint(ids_b), "the same job was claimed by both workers"
        assert ids_a | ids_b == job_ids, "every job was claimed by exactly one worker"
    finally:
        await conn_a.execute("TRUNCATE jobs RESTART IDENTITY CASCADE")
        await conn_a.close()
        await conn_b.close()
