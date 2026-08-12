"""Golden tests for leadgen/enrich_company.md (docs/phase1-llm-boundary.md §5).

@pytest.mark.golden — excluded by default, run via `make golden`. Calls the
real Anthropic API (ANTHROPIC_API_KEY required) and writes to
TEST_DATABASE_URL through the real complete_json() path — no stubbed client
here, unlike tests/integration/test_llm.py.
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
        "leadgen/enrich_company.md",
        variables,
        "outputs/company_enrichment.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="golden-test",
    )


async def test_company_sparse_yields_nulls_and_insufficient_context(conn: asyncpg.Connection):
    """The core anti-fabrication check (docs/phase1-llm-boundary.md §2): given
    no usable research, the model must say so — never invent an industry,
    business model, or employee band to fill the schema."""
    result = await _run(conn, "company_sparse.json")

    assert result["insufficient_context"] is True
    for field in ("industry", "sub_industry", "business_model", "employee_band"):
        assert result[field]["value"] is None, (
            f"{field} should be null, not invented, on sparse input"
        )


async def test_company_rich_yields_confident_populated_fields(conn: asyncpg.Connection):
    """The counterpart to the sparse case: given clearly stated facts (34
    people, founded 2014, HubSpot/Airtable, hiring ops analysts), the model
    should NOT claim insufficient context and should populate at least
    industry and employee_band with reasonable confidence."""
    result = await _run(conn, "company_rich.json")

    assert result["insufficient_context"] is False
    assert result["employee_band"]["value"] is not None
    assert result["employee_band"]["confidence"] > 0.4
    assert result["industry"]["value"] is not None
