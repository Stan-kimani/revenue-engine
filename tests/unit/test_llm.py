"""Unit tests for core/llm.py's pure logic: prompt loading/rendering, and the
V1-V9 cross-field validators in isolation. No I/O, no database, no API key —
these test the internal `_`-prefixed functions directly (white-box), which is
how the V-hooks' individual behaviour is exercised without needing a real
conn or a stubbed Anthropic client for each one.

complete_json() end-to-end — the retry-once-then-fail control flow, which
needs a real conn for agent_runs/llm.validation_failed — is tested in
tests/integration/test_llm.py against TEST_DATABASE_URL with a stubbed
Anthropic client (never a real API call). See docs/decisions.md for why
those live in tests/integration/ rather than tests/unit/ despite needing no
API key: build-spec §8.1 groups "no network, no LLM" under unit; a real
Postgres write is exactly what tests/integration/ is for.
"""

from __future__ import annotations

import pytest

from revenue_engine.core import llm
from revenue_engine.core.errors import PromptRenderError

# ---------------------------------------------------------------------------
# Prompt loading + rendering
# ---------------------------------------------------------------------------


def test_real_prompt_loads_with_matching_id():
    template = llm._load_prompt("sales/classify_reply.md")
    assert template.id == "sales/classify_reply"
    assert template.tier == "fast"
    assert template.output_schema == "outputs/reply_classification.json"
    assert "reply_text" in template.variables


@pytest.mark.protected
def test_render_raises_on_unsupplied_variable():
    template = llm._load_prompt("sales/classify_reply.md")
    incomplete = {"reply_text": "too expensive"}  # missing thread_history, objection_vocabulary
    with pytest.raises(PromptRenderError) as exc_info:
        llm._render(template.body, incomplete, template.id)
    assert "thread_history" in exc_info.value.missing
    assert "objection_vocabulary" in exc_info.value.missing


def test_render_never_produces_empty_string_for_missing_variable():
    body = "Hello {{name}}, welcome."
    with pytest.raises(PromptRenderError):
        llm._render(body, {}, "test/prompt")


def test_render_substitutes_supplied_variables():
    body = "Hello {{name}}, your score is {{score}}."
    rendered = llm._render(body, {"name": "Ada", "score": 0.9}, "test/prompt")
    assert rendered == "Hello Ada, your score is 0.9."


def test_render_serializes_structured_variables_as_json():
    body = "Profile: {{profile}}"
    rendered = llm._render(body, {"profile": {"a": 1}}, "test/prompt")
    assert '"a": 1' in rendered


def test_strip_code_fence_removes_json_fence():
    fenced = '```json\n{"a": 1}\n```'
    assert llm._strip_code_fence(fenced) == '{"a": 1}'


def test_strip_code_fence_leaves_unfenced_text_alone():
    assert llm._strip_code_fence('{"a": 1}') == '{"a": 1}'


# ---------------------------------------------------------------------------
# V2 — word count tokenisation rule
#
# Rule (stated once in core/llm.py::_word_count, applied identically here):
# split on whitespace, keep tokens with at least one alphanumeric character.
# A hyphenated word or a contraction is one token, not two.
# ---------------------------------------------------------------------------


def test_word_count_counts_hyphenated_word_as_one():
    assert llm._word_count("follow-up is due") == 3


def test_word_count_counts_contraction_as_one():
    assert llm._word_count("don't wait") == 2


def test_word_count_ignores_punctuation_only_tokens():
    assert llm._word_count("wait - then go") == 3  # lone "-" is not a word


def test_v2_accepts_matching_word_count():
    output = {"body": "one two three four five", "word_count": 5}
    assert llm._v2_word_count_matches(output, {}) == []


def test_v2_rejects_mismatched_word_count():
    output = {"body": "one two three", "word_count": 40}
    errors = llm._v2_word_count_matches(output, {})
    assert errors and "40" in errors[0]


def test_v2_tolerates_plus_minus_two():
    output = {"body": "one two three four five", "word_count": 7}
    assert llm._v2_word_count_matches(output, {}) == []
    output["word_count"] = 8
    assert llm._v2_word_count_matches(output, {}) != []


# ---------------------------------------------------------------------------
# V1 / V3 — anchor existence
# ---------------------------------------------------------------------------

_PROFILE_WITH_ANCHORS = {
    "personalization_anchors": [
        {"anchor_id": "anchor_1", "fact": "x", "source": "y", "confidence": 0.8},
        {"anchor_id": "anchor_2", "fact": "x", "source": "y", "confidence": 0.8},
    ]
}


def test_v1_accepts_facts_with_known_anchors():
    output = {"facts_asserted": [{"claim": "c", "anchor_id": "anchor_1"}]}
    variables = {"prospect_profile": _PROFILE_WITH_ANCHORS}
    assert llm._v1_facts_asserted_anchors_exist(output, variables) == []


@pytest.mark.protected
def test_v1_rejects_facts_with_unknown_anchor_id():
    output = {"facts_asserted": [{"claim": "c", "anchor_id": "anchor_9"}]}
    variables = {"prospect_profile": _PROFILE_WITH_ANCHORS}
    errors = llm._v1_facts_asserted_anchors_exist(output, variables)
    assert errors and "anchor_9" in errors[0]


def test_v1_empty_facts_asserted_is_always_valid():
    variables = {"prospect_profile": {"personalization_anchors": []}}
    assert llm._v1_facts_asserted_anchors_exist({"facts_asserted": []}, variables) == []


def test_v3_rejects_supporting_anchor_not_in_profile():
    output = {"supporting_anchors": ["anchor_9"]}
    variables = {"prospect_profile": _PROFILE_WITH_ANCHORS}
    errors = llm._v3_supporting_anchors_exist(output, variables)
    assert errors and "anchor_9" in errors[0]


def test_v3_accepts_known_supporting_anchor():
    output = {"supporting_anchors": ["anchor_1", "anchor_2"]}
    variables = {"prospect_profile": _PROFILE_WITH_ANCHORS}
    assert llm._v3_supporting_anchors_exist(output, variables) == []


# ---------------------------------------------------------------------------
# V4 — pricing honesty (correcting, never rejecting)
# ---------------------------------------------------------------------------

_BASE_PROPOSAL: dict = {
    "title": "Proposal",
    "problem_statement": "x",
    "proposed_scope": [{"item": "Build", "outcome": "y"}],
    "out_of_scope": [],
    "assumptions": [],
    "timeline": [],
    "pricing_present": False,
    "pricing_placeholders": [],
    "confidence": 0.8,
}


@pytest.mark.protected
def test_v4_forces_pricing_present_true_when_currency_found_but_model_said_false():
    output = {**_BASE_PROPOSAL, "pricing_placeholders": ["Estimated at $12,000 for phase one"]}
    llm._v4_pricing_present_honest(output, {})
    assert output["pricing_present"] is True


def test_v4_leaves_pricing_present_false_when_no_currency_pattern():
    output = {
        **_BASE_PROPOSAL,
        "proposed_scope": [{"item": "Build", "outcome": "a workflow automated end to end"}],
        "timeline": [{"phase": "Build", "duration": "3-6 weeks"}],
    }
    llm._v4_pricing_present_honest(output, {})
    assert output["pricing_present"] is False


def test_v4_does_not_flag_a_bare_duration_or_headcount_number():
    output = {
        **_BASE_PROPOSAL,
        "proposed_scope": [{"item": "i", "outcome": "50 people onboarded in 3-6 weeks"}],
    }
    llm._v4_pricing_present_honest(output, {})
    assert output["pricing_present"] is False


def test_v4_is_a_noop_when_model_already_said_true():
    output = {**_BASE_PROPOSAL, "pricing_present": True}
    llm._v4_pricing_present_honest(output, {})
    assert output["pricing_present"] is True


# ---------------------------------------------------------------------------
# V5 — precedents_used subset (correcting)
# ---------------------------------------------------------------------------


def test_v5_strips_precedent_ids_not_in_retrieved_set():
    output = {"precedents_used": ["p1", "p2", "p3"]}
    variables = {"retrieved_precedents": [{"id": "p1"}, {"id": "p2"}]}
    llm._v5_precedents_used_subset(output, variables)
    assert output["precedents_used"] == ["p1", "p2"]


def test_v5_keeps_all_when_all_retrieved():
    output = {"precedents_used": ["p1"]}
    variables = {"retrieved_precedents": ["p1", "p2"]}
    llm._v5_precedents_used_subset(output, variables)
    assert output["precedents_used"] == ["p1"]


# ---------------------------------------------------------------------------
# V6 — subscore evidence
# ---------------------------------------------------------------------------


def test_v6_rejects_high_score_with_no_evidence():
    output = {
        "buying_intent": {"score": 0.6, "evidence": [], "confidence": 0.5},
        "seniority_fit": {"score": 0.1, "evidence": [], "confidence": 0.5},
        "narrative_fit": {"score": 0.1, "evidence": [], "confidence": 0.5},
        "overall_note": "n",
    }
    errors = llm._v6_subscore_evidence_nonempty(output, {})
    assert len(errors) == 1
    assert "buying_intent" in errors[0]


def test_v6_allows_low_score_with_no_evidence():
    output = {
        "buying_intent": {"score": 0.2, "evidence": [], "confidence": 0.5},
        "seniority_fit": {"score": 0.0, "evidence": [], "confidence": 0.5},
        "narrative_fit": {"score": 0.0, "evidence": [], "confidence": 0.5},
        "overall_note": "n",
    }
    assert llm._v6_subscore_evidence_nonempty(output, {}) == []


def test_v6_allows_high_score_with_evidence():
    output = {
        "buying_intent": {"score": 0.9, "evidence": ["hiring for ops role"], "confidence": 0.8},
        "seniority_fit": {"score": 0.0, "evidence": [], "confidence": 0.5},
        "narrative_fit": {"score": 0.0, "evidence": [], "confidence": 0.5},
        "overall_note": "n",
    }
    assert llm._v6_subscore_evidence_nonempty(output, {}) == []


# ---------------------------------------------------------------------------
# V7 / V8 — reply_classification cross fields
# ---------------------------------------------------------------------------


def test_v7_rejects_objection_category_without_objection_intent():
    output = {"intent": "not_now", "objection_category": "price"}
    assert llm._v7_objection_category_only_if_objection(output, {}) != []


def test_v7_allows_objection_category_when_intent_is_objection():
    output = {"intent": "objection", "objection_category": "price"}
    assert llm._v7_objection_category_only_if_objection(output, {}) == []


def test_v7_allows_null_category_for_any_intent():
    output = {"intent": "unclear", "objection_category": None}
    assert llm._v7_objection_category_only_if_objection(output, {}) == []


def test_v8_nulls_resume_date_when_intent_is_not_not_now():
    output = {"intent": "interested", "requested_resume_date": "2026-09-01"}
    llm._v8_resume_date_only_if_not_now(output, {})
    assert output["requested_resume_date"] is None


def test_v8_keeps_resume_date_when_intent_is_not_now():
    output = {"intent": "not_now", "requested_resume_date": "2026-09-01"}
    llm._v8_resume_date_only_if_not_now(output, {})
    assert output["requested_resume_date"] == "2026-09-01"


# ---------------------------------------------------------------------------
# V9 — no email address anywhere in contact_enrichment output
# ---------------------------------------------------------------------------


@pytest.mark.protected
def test_v9_rejects_output_containing_email_address():
    output = {
        "inferred_pains": [{"pain": "reach me at a@b.com", "reasoning": "r", "confidence": 0.5}]
    }
    assert llm._v9_no_email_regex(output, {}) != []


def test_v9_allows_output_with_no_email_address():
    output = {"inferred_pains": [{"pain": "manual reporting", "reasoning": "r", "confidence": 0.5}]}
    assert llm._v9_no_email_regex(output, {}) == []


# ---------------------------------------------------------------------------
# Registry wiring — each output schema's validators are exactly the ones the
# spec table names, nothing added, nothing missing.
# ---------------------------------------------------------------------------


def test_rejecting_validator_registry_matches_spec_table():
    assert set(llm._REJECTING_VALIDATORS.keys()) == {
        "outputs/outreach_draft.json",
        "outputs/account_brief.json",
        "outputs/lead_subscores.json",
        "outputs/reply_classification.json",
        "outputs/contact_enrichment.json",
    }


def test_correcting_validator_registry_matches_spec_table():
    assert set(llm._CORRECTING_VALIDATORS.keys()) == {
        "outputs/proposal_draft.json",
        "outputs/objection_response.json",
        "outputs/reply_classification.json",
    }
