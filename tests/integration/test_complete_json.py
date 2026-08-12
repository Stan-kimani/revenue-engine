"""Integration tests for core/llm.py::complete_json() against a real Postgres
instance (TEST_DATABASE_URL, never DATABASE_URL — tests/_db_safety.py).

The Anthropic client is always a stub here — never a real API call, no
ANTHROPIC_API_KEY needed. These tests live in tests/integration/, not
tests/unit/, because complete_json() writes agent_runs and (on failure)
emits llm.validation_failed — real Postgres writes are exactly what
tests/integration/ is for (CLAUDE.md §5: "Integration tests run against a
real test Postgres with mocked integrations" — the stub client is the
mocked integration). See docs/decisions.md for this placement rationale
relative to the milestone instruction's literal "tests/unit" wording.

Tests marked @pytest.mark.protected encode a business rule from an explicit
instruction and must not be weakened to make them pass.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import pytest

from revenue_engine.core import llm
from revenue_engine.core.errors import LLMValidationError
from revenue_engine.core.observability import TraceContext
from revenue_engine.db.models import AgentRunStatus

pytestmark = pytest.mark.integration

# database_url fixture comes from tests/integration/conftest.py (TEST_DATABASE_URL,
# never DATABASE_URL — see docs/decisions.md).


@pytest.fixture
async def conn(database_url: str):
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.execute("TRUNCATE agent_runs, events RESTART IDENTITY CASCADE")
        await connection.close()


# ---------------------------------------------------------------------------
# Stub Anthropic client — duck-types the real SDK's response shape
# (response.content[].text, response.usage.{input,output}_tokens) without
# importing or calling anthropic at all.
# ---------------------------------------------------------------------------


@dataclass
class _StubUsage:
    input_tokens: int = 10
    output_tokens: int = 20


@dataclass
class _StubBlock:
    text: str


@dataclass
class _StubResponse:
    content: list[_StubBlock]
    usage: _StubUsage = field(default_factory=_StubUsage)


class _StubMessages:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _StubResponse:
        self.calls.append(kwargs)
        text = self._responses.pop(0)
        return _StubResponse(content=[_StubBlock(text=text)])


class _StubClient:
    def __init__(self, responses: list[str]):
        self.messages = _StubMessages(responses)


_VALID_CLASSIFICATION = json.dumps(
    {
        "intent": "objection",
        "confidence": 0.8,
        "reasoning": "Prospect raised a price concern directly.",
        "objection_category": "price",
        "escalation_signals": [],
        "suggested_action": "send_followup",
    }
)

_MALFORMED_CLASSIFICATION = json.dumps({"intent": "objection"})  # missing required fields


async def _agent_run_for(conn: asyncpg.Connection, prompt_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM agent_runs WHERE prompt_id = $1", prompt_id)


def _classify_reply_variables() -> dict[str, Any]:
    return {
        "reply_text": "This is too expensive for us right now.",
        "thread_history": "(no prior messages)",
        "objection_vocabulary": {"price": "Cost, budget, or perceived expense."},
    }


async def test_succeeds_on_first_attempt_with_no_retry(conn: asyncpg.Connection):
    client = _StubClient([_VALID_CLASSIFICATION])
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id="t-1", correlation_id=correlation_id)

    result = await llm.complete_json(
        "sales/classify_reply.md",
        _classify_reply_variables(),
        "outputs/reply_classification.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="test",
        client=client,
    )

    assert result["intent"] == "objection"
    assert len(client.messages.calls) == 1

    row = await _agent_run_for(conn, "sales/classify_reply")
    assert row is not None
    assert row["status"] == AgentRunStatus.SUCCESS.value
    assert row["retry_count"] == 0
    assert row["tier"] == "fast"
    assert row["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.protected
async def test_retries_exactly_once_on_schema_invalid_response_then_succeeds(
    conn: asyncpg.Connection,
):
    """The stubbed client returns malformed JSON first, valid JSON second —
    exercising the retry path without any real API call."""
    client = _StubClient([_MALFORMED_CLASSIFICATION, _VALID_CLASSIFICATION])
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id="t-2", correlation_id=correlation_id)

    result = await llm.complete_json(
        "sales/classify_reply.md",
        _classify_reply_variables(),
        "outputs/reply_classification.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="test",
        client=client,
    )

    assert result["intent"] == "objection"
    assert len(client.messages.calls) == 2
    # the corrective turn must actually carry the validation error back to the model
    second_call_messages = client.messages.calls[1]["messages"]
    assert any("failed validation" in m["content"] for m in second_call_messages)

    row = await _agent_run_for(conn, "sales/classify_reply")
    assert row is not None
    assert row["status"] == AgentRunStatus.SUCCESS.value
    assert row["retry_count"] == 1

    failed_count = await conn.fetchval(
        "SELECT count(*) FROM events WHERE type = 'llm.validation_failed'"
    )
    assert failed_count == 0


@pytest.mark.protected
async def test_raises_and_writes_nothing_partial_after_two_consecutive_failures(
    conn: asyncpg.Connection,
):
    client = _StubClient([_MALFORMED_CLASSIFICATION, _MALFORMED_CLASSIFICATION])
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id="t-3", correlation_id=correlation_id)

    with pytest.raises(LLMValidationError):
        await llm.complete_json(
            "sales/classify_reply.md",
            _classify_reply_variables(),
            "outputs/reply_classification.json",
            trace,
            conn=conn,
            correlation_id=correlation_id,
            actor="test",
            client=client,
        )

    assert len(client.messages.calls) == 2

    runs = await conn.fetch("SELECT * FROM agent_runs WHERE prompt_id = $1", "sales/classify_reply")
    assert len(runs) == 1  # exactly one row — the failure, never a partial success row
    assert runs[0]["status"] == AgentRunStatus.FAILED.value
    assert runs[0]["retry_count"] == 1
    assert runs[0]["error"]

    events = await conn.fetch("SELECT * FROM events WHERE type = 'llm.validation_failed'")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["prompt_id"] == "sales/classify_reply"
    assert payload["prompt_version"] == 1
    assert payload["run_id"] == str(runs[0]["id"])
    assert payload["errors"]


async def test_v4_forces_pricing_present_through_the_full_call_path(conn: asyncpg.Connection):
    """V4 is a correcting validator — this must succeed on the first attempt
    (no retry), with pricing_present flipped to true in the returned dict."""
    proposal = {
        "title": "Operations diagnostic",
        "problem_statement": "Manual intake and reporting consume too much staff time.",
        "proposed_scope": [{"item": "Diagnostic sprint", "outcome": "A ranked map of leaks"}],
        "out_of_scope": [],
        "assumptions": [],
        "timeline": [{"phase": "Diagnostic", "duration": "1-2 weeks"}],
        "pricing_present": False,
        "pricing_placeholders": ["Estimated at $8,000 for the diagnostic phase"],
        "confidence": 0.7,
    }
    client = _StubClient([json.dumps(proposal)])
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id="t-4", correlation_id=correlation_id)

    result = await llm.complete_json(
        "sales/draft_proposal.md",
        {
            "conversation_history": "(thread)",
            "meeting_insights": "(insights)",
            "prospect_profile": {"personalization_anchors": []},
            "service_catalogue": {
                "entry": {},
                "core": {},
                "ongoing": {},
                "pricing": "HUMAN_SET_ONLY",
            },
            "voice_rules": "Be direct.",
        },
        "outputs/proposal_draft.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="test",
        client=client,
    )

    assert result["pricing_present"] is True
    assert len(client.messages.calls) == 1  # correcting validators never trigger a retry

    row = await _agent_run_for(conn, "sales/draft_proposal")
    assert row is not None
    assert row["retry_count"] == 0
    assert row["status"] == AgentRunStatus.SUCCESS.value


async def test_frontmatter_output_schema_mismatch_raises_before_any_call(conn: asyncpg.Connection):
    client = _StubClient([_VALID_CLASSIFICATION])
    correlation_id = uuid.uuid4()

    with pytest.raises(Exception, match="output_schema"):
        await llm.complete_json(
            "sales/classify_reply.md",
            _classify_reply_variables(),
            "outputs/objection_response.json",  # wrong on purpose
            None,
            conn=conn,
            correlation_id=correlation_id,
            actor="test",
            client=client,
        )
    assert client.messages.calls == []
