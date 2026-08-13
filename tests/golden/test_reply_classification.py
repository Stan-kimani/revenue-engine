"""Golden tests for sales/classify_reply.md (docs/phase1-llm-boundary.md §5).

@pytest.mark.golden — excluded by default, run via `make golden`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg
import pytest

from revenue_engine.core import llm
from revenue_engine.core.config import get_config
from revenue_engine.core.observability import TraceContext

pytestmark = pytest.mark.golden

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


async def _run(conn: asyncpg.Connection, fixture_name: str) -> dict:
    reply_text = (_FIXTURES_DIR / fixture_name).read_text().strip()
    variables = {
        "reply_text": reply_text,
        "thread_history": "(no prior messages in this thread)",
        "objection_vocabulary": dict(get_config().pack.objection_categories),
    }
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id=f"golden-{fixture_name}", correlation_id=correlation_id)
    return await llm.complete_json(
        "sales/classify_reply.md",
        variables,
        "outputs/reply_classification.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="golden-test",
    )


async def test_price_objection_classifies_as_objection_price(conn: asyncpg.Connection):
    result = await _run(conn, "reply_price_objection.txt")
    assert result["intent"] == "objection"
    assert result["objection_category"] == "price"


async def test_angry_reply_flags_anger_escalation_signal(conn: asyncpg.Connection):
    result = await _run(conn, "reply_angry.txt")
    assert "anger" in result["escalation_signals"]
    assert result["suggested_action"] in ("escalate_human", "suppress_contact")


async def test_out_of_office_classifies_as_out_of_office_not_not_now(conn: asyncpg.Connection):
    """The specific confusion this fixture guards against: an OOO
    autoresponder reads superficially like "not now", but it is not a human
    decision to defer — intent must be out_of_office, a distinct value."""
    result = await _run(conn, "reply_ooo.txt")
    assert result["intent"] == "out_of_office"
    assert result["intent"] != "not_now"


async def test_polite_unsubscribe_classifies_as_unsubscribe(conn: asyncpg.Connection):
    result = await _run(conn, "reply_polite_unsubscribe.txt")
    assert result["intent"] == "unsubscribe"
    assert result["suggested_action"] == "suppress_contact"


async def test_cryptic_single_letter_reply_is_unclear_with_low_confidence(
    conn: asyncpg.Connection,
):
    result = await _run(conn, "reply_cryptic.txt")
    assert result["intent"] == "unclear"
    assert result["confidence"] < 0.5


async def test_referral_reply_names_the_referred_contact(conn: asyncpg.Connection):
    result = await _run(conn, "reply_referral.txt")
    assert result["intent"] == "referral"
    assert result["referral_contact_named"] is not None
    assert "Priya" in result["referral_contact_named"]


async def test_not_now_budget_deferral_no_explicit_date(conn: asyncpg.Connection):
    """Budget-timing deferral: intent is not_now, not objection.

    The fixture says "don't have the budget this quarter / check back later in
    the year" — a timing deferral, not a cost-vs-value dispute.  V7 enforces
    objection_category is null when intent != objection.  "Later in the year"
    is not an explicit calendar date, so requested_resume_date must be null
    (V8 corrects any non-null value when intent != not_now, but here we assert
    the model doesn't hallucinate a date in the first place).
    """
    result = await _run(conn, "reply_not_now_budget.txt")
    assert result["intent"] == "not_now"
    assert result["objection_category"] is None
    assert result["suggested_action"] == "pause_sequence"
    assert result["requested_resume_date"] is None
