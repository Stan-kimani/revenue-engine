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
async def test_request_sent_to_model_includes_the_output_schema(conn: asyncpg.Connection):
    """Regression guard (docs/decisions.md, 2026-08-13): complete_json used to
    validate the response against the schema but never showed the model the
    schema in the request — the model then invented plausible-looking enum
    values and field names from prose alone ('follow_up' instead of
    'send_followup', 'escalate_to_human' instead of 'escalate_human', etc.).
    This asserts what was actually SENT, not just what complete_json does
    with the reply — the class of check the original 190-test suite had none
    of, which is exactly why the bug was invisible to it."""
    client = _StubClient([_VALID_CLASSIFICATION])
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id="t-schema", correlation_id=correlation_id)

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

    assert len(client.messages.calls) == 1
    call_kwargs = client.messages.calls[0]
    assert "output_config" in call_kwargs
    output_format = call_kwargs["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    sent_schema = output_format["schema"]

    # The exact real enum, as sent -- this is what would have caught the
    # original bug ('follow_up' is not one of [...]).
    assert sent_schema["properties"]["intent"]["enum"] == [
        "interested",
        "objection",
        "not_now",
        "unsubscribe",
        "out_of_office",
        "referral",
        "unclear",
    ]
    # objection_category mixes an enum with null in the real schema -- must
    # be transformed to anyOf for the structured-output API, never sent as
    # an enum containing null (unsupported there).
    category = sent_schema["properties"]["objection_category"]
    assert "enum" not in category
    assert {"type": "null"} in category["anyOf"]

    # Round 2 of this bug (docs/decisions.md, 2026-08-14): the API also
    # rejects a fixed set of bounding keywords outright with a 400
    # ("For 'number' type, properties maximum, minimum are not supported").
    # None of them may survive anywhere in what's actually sent.
    unsupported = _find_unsupported_keywords(sent_schema)
    assert not unsupported, f"unsupported keyword(s) sent to the API: {unsupported}"
    # confidence's 0..1 bound is real in the schema on disk -- prove it's
    # actually one of the keywords being stripped, not just that the stub
    # happens not to trip over an empty set.
    on_disk = llm._load_output_schema("outputs/reply_classification.json")
    assert "maximum" in json.dumps(on_disk)


def _find_unsupported_keywords(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in llm._STRUCTURED_OUTPUT_UNSUPPORTED_KEYWORDS:
                found.add(key)
            found |= _find_unsupported_keywords(value)
    elif isinstance(node, list):
        for item in node:
            found |= _find_unsupported_keywords(item)
    return found


@pytest.mark.protected
async def test_confidence_out_of_bounds_is_still_rejected_after_structured_output_strip(
    conn: asyncpg.Connection,
):
    """Structured outputs enforces shape and enum membership only -- the
    request-side schema has its `minimum`/`maximum` bound on `confidence`
    stripped (the API 400s otherwise). This proves the bound still survives
    via post-call jsonschema validation against the untouched on-disk
    schema: an out-of-range confidence must still be rejected, retried, and
    ultimately raise -- the retry/validation path is not dead code just
    because structured outputs exists."""
    out_of_bounds = json.dumps(
        {
            "intent": "objection",
            "confidence": 1.5,  # schema bounds this to [0, 1] -- stripped from the request
            "reasoning": "Prospect raised a price concern directly.",
            "objection_category": "price",
            "escalation_signals": [],
            "suggested_action": "send_followup",
        }
    )
    client = _StubClient([out_of_bounds, out_of_bounds])
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id="t-bounds", correlation_id=correlation_id)

    with pytest.raises(LLMValidationError) as exc_info:
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

    assert len(client.messages.calls) == 2  # retried once, then gave up -- not silently accepted
    assert any("1.5" in e or "maximum" in e.lower() for e in exc_info.value.errors)

    row = await _agent_run_for(conn, "sales/classify_reply")
    assert row is not None
    assert row["status"] == AgentRunStatus.FAILED.value


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
    # the corrective turn must carry both the validation error AND the
    # schema back to the model (docs/decisions.md, 2026-08-13) -- attempt 2
    # must not be anchored on attempt 1's wrong shape with only a bare error
    # string to go on.
    second_call_messages = client.messages.calls[1]["messages"]
    corrective = second_call_messages[-1]["content"]
    assert "failed validation" in corrective
    assert '"objection_category"' in corrective  # schema text, not just the error
    # output_config must still be present on the retry call too, not just
    # the first attempt.
    assert "output_config" in client.messages.calls[1]

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
    # Version-agnostic by design: the point of this assertion is that the event
    # records the version actually used (what makes prompt-version performance
    # comparison possible), not that the prompt is pinned at some literal
    # version. Read the version straight from the prompt's own frontmatter
    # rather than hardcoding it, so a future prompt-version bump can't silently
    # turn this into a stale assertion again.
    assert payload["prompt_version"] == llm._load_prompt("sales/classify_reply.md").version
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


# ---------------------------------------------------------------------------
# word_count is no longer model-supplied (docs/decisions.md, 2026-08-14):
# models cannot reliably count their own words, and rejecting an otherwise-
# good draft over a self-report discrepancy burned a retry on a non-defect.
# complete_json injects the real, computed count before schema validation;
# V2 now enforces draft_initial_outreach.md's actual 120-word rule.
# ---------------------------------------------------------------------------


def _draft_initial_outreach_variables() -> dict[str, Any]:
    return {
        "account_brief": {
            "angle": "General fit for the ICP.",
            "supporting_anchors": [],
            "proof_to_reference": [],
            "avoid": [],
            "confidence": 0.5,
        },
        "prospect_profile": {"personalization_anchors": []},
        "sender_persona": "A practitioner writing to another operator.",
        "voice_rules": "Be direct. Short sentences.",
        "constraints": "Do not assert any specific fact not grounded in an anchor.",
    }


def _outreach_draft_response(word_count_words: int) -> str:
    """Deliberately omits word_count entirely -- the model is no longer
    asked for it (schemas/outputs/outreach_draft.json no longer requires
    it; draft_initial_outreach.md no longer instructs the model to report
    it)."""
    body = " ".join(["word"] * word_count_words)
    return json.dumps(
        {
            "subject": "a quick question about intake",
            "body": body,
            "cta": {"kind": "question", "text": "Worth a quick reply?"},
            "facts_asserted": [],
            "tone_check": {"matches_voice_rules": True, "notes": "Direct, no fluff."},
        }
    )


@pytest.mark.protected
async def test_body_exceeding_word_limit_is_rejected_after_structured_output_strip(
    conn: asyncpg.Connection,
):
    """A draft whose real body is well over draft_initial_outreach.md's
    120-word rule must still be rejected -- retried once, then raise, since
    both stubbed attempts here are equally over limit. Proves V2 is
    reachable (not preempted by the schema's own, deliberately looser
    word_count bounds)."""
    too_long = _outreach_draft_response(150)
    client = _StubClient([too_long, too_long])
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id="t-wc-over", correlation_id=correlation_id)

    with pytest.raises(LLMValidationError) as exc_info:
        await llm.complete_json(
            "sales/draft_initial_outreach.md",
            _draft_initial_outreach_variables(),
            "outputs/outreach_draft.json",
            trace,
            conn=conn,
            correlation_id=correlation_id,
            actor="test",
            client=client,
        )

    assert len(client.messages.calls) == 2
    assert any("150" in e for e in exc_info.value.errors)


@pytest.mark.protected
async def test_body_within_word_limit_succeeds_with_injected_word_count(
    conn: asyncpg.Connection,
):
    """The counterpart: a draft within the limit succeeds on the first
    attempt, and the returned dict carries the real, code-computed
    word_count -- the model didn't report one at all here, and none was
    needed."""
    within_limit = _outreach_draft_response(80)
    client = _StubClient([within_limit])
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id="t-wc-under", correlation_id=correlation_id)

    result = await llm.complete_json(
        "sales/draft_initial_outreach.md",
        _draft_initial_outreach_variables(),
        "outputs/outreach_draft.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="test",
        client=client,
    )

    assert len(client.messages.calls) == 1
    assert result["word_count"] == 80

    row = await _agent_run_for(conn, "sales/draft_initial_outreach")
    assert row is not None
    assert row["status"] == AgentRunStatus.SUCCESS.value
    assert row["retry_count"] == 0


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
