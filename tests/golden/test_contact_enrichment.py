"""Golden tests for leadgen/enrich_decision_maker.md (docs/phase1-llm-boundary.md §5).

@pytest.mark.golden — excluded by default, run via `make golden`.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import asyncpg
import pytest

from revenue_engine.core import llm
from revenue_engine.core.observability import TraceContext

pytestmark = pytest.mark.golden

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


async def _run(conn: asyncpg.Connection, fixture_name: str) -> dict:
    variables = json.loads((_FIXTURES_DIR / fixture_name).read_text())
    correlation_id = uuid.uuid4()
    trace = TraceContext(trace_id=f"golden-{fixture_name}", correlation_id=correlation_id)
    return await llm.complete_json(
        "leadgen/enrich_decision_maker.md",
        variables,
        "outputs/contact_enrichment.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="golden-test",
    )


async def test_contact_founder_yields_high_seniority_and_decision_authority(
    conn: asyncpg.Connection,
):
    result = await _run(conn, "contact_founder.json")

    assert result["seniority"]["value"] == "founder_owner"
    assert result["decision_authority"]["value"] == "economic_buyer"
    assert result["insufficient_context"] is False


async def test_contact_ambiguous_title_does_not_force_a_confident_guess(conn: asyncpg.Connection):
    """A title that genuinely doesn't map to a clear seniority/authority
    level must not be forced into one — either insufficient_context is true,
    or the values returned carry visibly low confidence. Either is honest;
    a confident 'director' guess from 'Special Projects' alone is not."""
    result = await _run(conn, "contact_ambiguous_title.json")

    if not result["insufficient_context"]:
        assert result["seniority"]["confidence"] < 0.5
        assert result["decision_authority"]["confidence"] < 0.5


async def test_contact_enrichment_never_generates_an_email_address(conn: asyncpg.Connection):
    """V9, exercised end to end against a real model response, not just the
    unit-tested regex in isolation."""
    result = await _run(conn, "contact_founder.json")
    assert not llm._EMAIL_RE.search(json.dumps(result))
