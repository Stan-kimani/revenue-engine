"""Golden test chain for leadgen/build_prospect_profile.md ->
sales/draft_initial_outreach.md (docs/phase1-llm-boundary.md §5:
profile_no_anchors.json "must yield an outreach draft with empty
facts_asserted"). Two real LLM calls, deliberately: the fixture's contract is
about what a downstream outreach draft does with a profile that has nothing
groundable in it, not just about build_prospect_profile's own output shape.

@pytest.mark.golden — excluded by default, run via `make golden`.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import asyncpg
import pytest

from revenue_engine.core import llm
from revenue_engine.core.config import get_config
from revenue_engine.core.observability import TraceContext

pytestmark = pytest.mark.golden

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


async def test_profile_no_anchors_yields_outreach_draft_with_empty_facts_asserted(
    conn: asyncpg.Connection,
):
    config = get_config()
    variables = json.loads((_FIXTURES_DIR / "profile_no_anchors.json").read_text())
    variables["icp_definition"] = dict(config.pack.icp)

    correlation_id = uuid.uuid4()
    profile_trace = TraceContext(
        trace_id="golden-profile-no-anchors", correlation_id=correlation_id
    )
    profile = await llm.complete_json(
        "leadgen/build_prospect_profile.md",
        variables,
        "outputs/prospect_profile.json",
        profile_trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="golden-test",
    )

    assert profile["personalization_anchors"] == []

    outreach_trace = TraceContext(
        trace_id="golden-outreach-no-anchors", correlation_id=correlation_id
    )
    outreach = await llm.complete_json(
        "sales/draft_initial_outreach.md",
        {
            "account_brief": {
                "angle": "General fit for the ICP; no specific grounded facts available yet.",
                "supporting_anchors": [],
                "proof_to_reference": [],
                "avoid": ["Specific claims about their operations — nothing is grounded"],
                "confidence": 0.3,
            },
            "prospect_profile": profile,
            "sender_persona": config.pack.voice.sender_persona,
            "voice_rules": config.pack.voice.as_prompt_text(),
            "constraints": (
                "Do not assert any specific fact about this company or contact — none is grounded."
            ),
        },
        "outputs/outreach_draft.json",
        outreach_trace,
        conn=conn,
        correlation_id=correlation_id,
        actor="golden-test",
        causation_id=None,
    )

    assert outreach["facts_asserted"] == []
