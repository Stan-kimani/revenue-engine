"""Integration tests for core/events.py against a real Postgres instance.

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from revenue_engine.core import events as core_events
from revenue_engine.core.errors import EventPayloadValidationError, UnknownEventTypeError

pytestmark = pytest.mark.integration


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
        await connection.execute("TRUNCATE events RESTART IDENTITY CASCADE")
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
async def test_reemitting_same_idempotency_key_is_a_noop_not_an_error(conn: asyncpg.Connection):
    key = f"lead:{uuid.uuid4()}:captured"
    correlation_id = uuid.uuid4()

    first = await core_events.emit(
        conn,
        type="lead.captured",
        payload=_valid_lead_captured_payload(),
        correlation_id=correlation_id,
        actor="test",
        idempotency_key=key,
    )
    second = await core_events.emit(
        conn,
        type="lead.captured",
        payload=_valid_lead_captured_payload(),  # different payload, same key
        correlation_id=correlation_id,
        actor="test",
        idempotency_key=key,
    )

    assert first.event_id == second.event_id
    assert second.payload == first.payload  # second call's payload was discarded

    count = await conn.fetchval("SELECT count(*) FROM events WHERE idempotency_key = $1", key)
    assert count == 1


@pytest.mark.protected
async def test_payload_failing_schema_validation_is_never_inserted(conn: asyncpg.Connection):
    key = f"lead:{uuid.uuid4()}:captured"

    with pytest.raises(EventPayloadValidationError):
        await core_events.emit(
            conn,
            type="lead.captured",
            payload={"lead_id": "not-a-uuid"},  # missing required fields, bad format
            correlation_id=uuid.uuid4(),
            actor="test",
            idempotency_key=key,
        )

    count = await conn.fetchval("SELECT count(*) FROM events WHERE idempotency_key = $1", key)
    assert count == 0


async def test_unknown_event_type_raises_before_any_db_access(conn: asyncpg.Connection):
    with pytest.raises(UnknownEventTypeError):
        await core_events.emit(
            conn,
            type="totally.made.up.event",
            payload={},
            correlation_id=uuid.uuid4(),
            actor="test",
            idempotency_key="whatever",
        )

    count = await conn.fetchval("SELECT count(*) FROM events")
    assert count == 0


async def test_causation_id_and_correlation_id_persist(conn: asyncpg.Connection):
    correlation_id = uuid.uuid4()
    cause = await core_events.emit(
        conn,
        type="lead.captured",
        payload=_valid_lead_captured_payload(),
        correlation_id=correlation_id,
        actor="test",
        idempotency_key=f"lead:{uuid.uuid4()}:captured",
    )

    effect = await core_events.emit(
        conn,
        type="job.dead_lettered",
        payload={
            "job_id": str(uuid.uuid4()),
            "job_type": "test.whatever",
            "attempts": 5,
            "last_error": "boom",
        },
        correlation_id=correlation_id,
        actor="test",
        idempotency_key=f"job:{uuid.uuid4()}:dead_lettered:5",
        causation_id=cause.event_id,
    )

    assert effect.correlation_id == correlation_id
    assert effect.causation_id == cause.event_id
