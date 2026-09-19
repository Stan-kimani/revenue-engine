"""Unit tests for agents/qualification.py's deterministic scorer — pure
functions, no I/O, no database (M1.2 plan, deliverable #2). Every scoring
rule that isn't a judgment call gets exercised here with plainly constructed
objects, matching CLAUDE.md §5 ("every scoring, classification, or
state-transition rule gets a unit test").
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

import pytest

from revenue_engine.agents.qualification import (
    DisqualifierHit,
    _assign_band,
    _combine_llm_subscores,
    evaluate_disqualifiers,
    score_deterministic,
)
from revenue_engine.core.config import IndustryPack, ScoringConfig, VoiceConfig
from revenue_engine.db.models import (
    BudgetBand,
    Company,
    Contact,
    EmailStatus,
    Lead,
    LeadBand,
    LeadSource,
    LeadStatus,
)

_NOW = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Factories — plain constructed objects, no YAML, no database.
# ---------------------------------------------------------------------------


def _attr(value: str) -> dict[str, object]:
    return {
        "value": value,
        "confidence": 0.8,
        "evidence": "test evidence",
        "source": "llm:test",
        "run_id": None,
        "observed_at": "2026-01-01T00:00:00Z",
    }


def _make_scoring(**overrides: object) -> ScoringConfig:
    base: dict[str, object] = {
        "weights": {
            "icp_match": 0.30,
            "intent": 0.22,
            "engagement": 0.18,
            "size_fit": 0.15,
            "budget_fit": 0.15,
        },
        "budget_fit_map": {
            "40k_plus": 1.0,
            "15k_40k": 0.85,
            "5k_15k": 0.5,
            "under_5k": 0.1,
            "unknown": 0.4,
        },
        "bands": {"sql": 78, "mql": 58, "warm": 38},
        "min_confidence_to_store": 0.4,
        "guidance": "test guidance",
        "llm_subscore_weights": {
            "buying_intent": 0.3334,
            "seniority_fit": 0.3333,
            "narrative_fit": 0.3333,
        },
        "engagement_points": {"reply": 1.0, "meeting": 5.0},
        "engagement_saturation": 5.0,
    }
    base.update(overrides)
    return ScoringConfig(
        weights=MappingProxyType(dict(base["weights"])),  # type: ignore[arg-type]
        budget_fit_map=MappingProxyType(dict(base["budget_fit_map"])),  # type: ignore[arg-type]
        bands=MappingProxyType(dict(base["bands"])),  # type: ignore[arg-type]
        min_confidence_to_store=base["min_confidence_to_store"],  # type: ignore[arg-type]
        guidance=base["guidance"],  # type: ignore[arg-type]
        llm_subscore_weights=MappingProxyType(dict(base["llm_subscore_weights"])),  # type: ignore[arg-type]
        engagement_points=MappingProxyType(dict(base["engagement_points"])),  # type: ignore[arg-type]
        engagement_saturation=base["engagement_saturation"],  # type: ignore[arg-type]
    )


_DEFAULT_DISQUALIFIERS = [
    {
        "id": "too_small",
        "rule": 'employee_band == "1-10" AND revenue_signal in [pre_revenue, early]',
        "reason": "Needs business fundamentals more than automation.",
    },
    {
        "id": "competitor",
        "rule": "business_model in [automation_agency, ai_consultancy, rpa_vendor]",
        "reason": "They sell what we sell.",
    },
    {
        "id": "enterprise",
        "rule": 'employee_band in ["201-1000", "1000+"]',
        "reason": "Procurement cycle too long.",
    },
]


def _make_pack(
    *, scoring: ScoringConfig | None = None, icp_overrides: dict | None = None
) -> IndustryPack:
    icp: dict[str, object] = {
        "summary": "test",
        "firmographics": {
            "business_models": ["b2b_services", "agency"],
            "employee_bands": ["11-50", "51-200"],
            "employee_bands_secondary": ["1-10"],
            "geographies_preferred": ["US", "UK"],
        },
        "roles": {
            "target_titles": ["founder"],
            "seniority_weight": {
                "founder_owner": 1.0,
                "c_level": 0.9,
                "director": 0.75,
                "manager": 0.45,
                "ic": 0.1,
            },
        },
        "disqualifiers": _DEFAULT_DISQUALIFIERS,
    }
    icp.update(icp_overrides or {})
    voice = VoiceConfig(
        sender_persona="x", tone_rules=("x",), vocabulary_say=(), vocabulary_avoid=()
    )
    return IndustryPack(
        name="test-pack",
        version=1,
        status="draft",
        icp=MappingProxyType(icp),
        scoring=scoring or _make_scoring(),
        qualification=MappingProxyType({}),
        voice=voice,
        commercial_boundaries=MappingProxyType({}),
        objection_categories=MappingProxyType({}),
        sequences=MappingProxyType({}),
        service_catalogue=MappingProxyType({}),
        discovery=MappingProxyType({}),
        channels=MappingProxyType({}),
        account_limits=MappingProxyType({}),
        outreach_draft_bands=frozenset({"sql"}),
    )


def _make_company(
    *,
    employee_band: str | None = "11-50",
    country: str | None = "US",
    attributes: dict | None = None,
) -> Company:
    return Company(
        id=uuid4(),
        name="Acme Co",
        domain="acme.example",
        linkedin_url=None,
        country=country,
        employee_band=employee_band,
        attributes=attributes or {},
        created_at=_NOW,
        updated_at=_NOW,
        deleted_at=None,
    )


def _make_contact(*, attributes: dict | None = None) -> Contact:
    return Contact(
        id=uuid4(),
        email="pat@acme.example",
        email_status=EmailStatus.UNVERIFIED,
        full_name="Pat Smith",
        first_name="Pat",
        last_name="Smith",
        title="Founder",
        linkedin_url=None,
        company_id=uuid4(),
        attributes=attributes or {},
        created_at=_NOW,
        updated_at=_NOW,
        deleted_at=None,
    )


def _make_lead(
    *, source: LeadSource = LeadSource.MANUAL_IMPORT, budget_band: BudgetBand | None = None
) -> Lead:
    return Lead(
        id=uuid4(),
        contact_id=uuid4(),
        company_id=uuid4(),
        campaign_id=None,
        industry_pack="test-pack",
        source=source,
        status=LeadStatus.NEW,
        band=None,
        current_score=None,
        deal_id=None,
        budget_band=budget_band,
        budget_source=None,
        problem_statement=None,
        pain_category=None,
        team_size_band=None,
        profile=None,
        first_touched_at=None,
        last_activity_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        deleted_at=None,
    )


# ---------------------------------------------------------------------------
# Determinism (protected)
# ---------------------------------------------------------------------------


@pytest.mark.protected
def test_identical_input_and_pack_produce_byte_identical_deterministic_part():
    pack = _make_pack()
    company = _make_company(attributes={"business_model": _attr("agency")})
    contact = _make_contact(attributes={"seniority": _attr("founder_owner")})
    lead = _make_lead(budget_band=BudgetBand.FORTY_K_PLUS)

    first = score_deterministic(
        company=company, contact=contact, lead=lead, reply_count=3, meeting_count=1, pack=pack
    )
    second = score_deterministic(
        company=company, contact=contact, lead=lead, reply_count=3, meeting_count=1, pack=pack
    )

    assert first.deterministic_part == second.deterministic_part  # exact equality, not approx
    assert first.components == second.components


# ---------------------------------------------------------------------------
# Band boundaries (protected)
# ---------------------------------------------------------------------------


@pytest.mark.protected
@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (78.0, LeadBand.SQL),  # exactly at the sql threshold
        (77.99, LeadBand.MQL),  # just below it
        (58.0, LeadBand.MQL),  # exactly at the mql threshold
        (57.99, LeadBand.WARM),
        (38.0, LeadBand.WARM),  # exactly at the warm threshold
        (37.99, LeadBand.COLD),
        (0.0, LeadBand.COLD),
        (100.0, LeadBand.SQL),
    ],
)
def test_band_assignment_is_exact_at_threshold(total: float, expected: LeadBand):
    pack = _make_pack()
    assert _assign_band(total, pack, disqualified=False) == expected


# ---------------------------------------------------------------------------
# Disqualifiers (protected)
# ---------------------------------------------------------------------------


@pytest.mark.protected
def test_disqualifier_hit_forces_cold_regardless_of_other_components():
    pack = _make_pack()
    # Every OTHER signal is maximised: primary-icp business model, preferred
    # geography, founder-level seniority, max engagement, max budget. Only
    # `employee_band` (secondary band) + `revenue_signal=pre_revenue` trips
    # the `too_small` disqualifier — independently of the other three
    # deterministic components, which all still score at or near their max.
    company = _make_company(
        employee_band="1-10",
        country="US",
        attributes={"business_model": _attr("agency"), "revenue_signal": _attr("pre_revenue")},
    )
    contact = _make_contact(attributes={"seniority": _attr("founder_owner")})
    lead = _make_lead(budget_band=BudgetBand.FORTY_K_PLUS)

    result = score_deterministic(
        company=company, contact=contact, lead=lead, reply_count=10, meeting_count=2, pack=pack
    )

    assert result.disqualifiers == (
        DisqualifierHit(id="too_small", reason="Needs business fundamentals more than automation."),
    )
    # The numeric total is high on every OTHER axis — proves this isn't
    # "forced cold because the score happened to be low anyway."
    assert result.deterministic_part > 60.0

    band = _assign_band(
        result.deterministic_part + 20.0, pack, disqualified=bool(result.disqualifiers)
    )
    assert band == LeadBand.COLD


def test_evaluate_disqualifiers_never_fires_on_missing_data():
    """Absence of evidence must never trigger a disqualifier — a company
    with no attributes at all and no employee_band must disqualify on
    nothing, not be treated as matching every 'not equal' branch."""
    pack = _make_pack()
    company = _make_company(employee_band=None, country=None, attributes={})

    hits = evaluate_disqualifiers(company=company, pack=pack)

    assert hits == []


def test_evaluate_disqualifiers_skips_manual_enforcement_rules():
    pack = _make_pack(
        icp_overrides={
            "disqualifiers": [
                {
                    "id": "regulated_health",
                    "rule": "industry matches [clinic, hospital]",
                    "reason": "PHI",
                    "enforcement": "manual",
                }
            ]
        }
    )
    company = _make_company(attributes={"business_model": _attr("agency")})

    # Would raise if evaluate_disqualifiers tried to parse the manual rule —
    # it must skip it entirely.
    hits = evaluate_disqualifiers(company=company, pack=pack)
    assert hits == []


# ---------------------------------------------------------------------------
# icp_match and size_fit stay distinct (confirmed in the approved plan) —
# same "1-10" band contributes differently to each.
# ---------------------------------------------------------------------------


def test_icp_match_and_size_fit_do_not_double_count_the_same_band():
    pack = _make_pack()
    # business_model/geography/seniority all maxed so icp_match's employee_band
    # sub-signal is the only thing NOT already at 1.0 — isolates its effect.
    company = _make_company(
        employee_band="1-10", country="US", attributes={"business_model": _attr("agency")}
    )
    contact = _make_contact(attributes={"seniority": _attr("founder_owner")})
    lead = _make_lead()

    result = score_deterministic(
        company=company, contact=contact, lead=lead, reply_count=0, meeting_count=0, pack=pack
    )

    # icp_match treats "1-10" as simply in-range (primary ∪ secondary) — all
    # four of its sub-signals are satisfied, so it scores 1.0. size_fit grades
    # the same band down as secondary-only: 0.5. Different numbers from the
    # same underlying fact, each feeding a distinct configured weight.
    assert result.components["icp_match"]["score"] == 1.0
    assert result.components["size_fit"]["score"] == 0.5
    assert result.components["icp_match"]["score"] != result.components["size_fit"]["score"]


# ---------------------------------------------------------------------------
# Config-driven, not hardcoded (two differently-weighted packs -> different
# numbers) — this is also what makes "changing pack weights does not alter
# existing lead_scores rows" true: a re-score under a new pack computes a
# genuinely different number, it doesn't retroactively change an old one.
# ---------------------------------------------------------------------------


def test_two_differently_weighted_packs_produce_different_deterministic_parts():
    pack_a = _make_pack(scoring=_make_scoring())
    pack_b = _make_pack(
        scoring=_make_scoring(
            weights={
                "icp_match": 0.10,
                "intent": 0.22,
                "engagement": 0.48,
                "size_fit": 0.10,
                "budget_fit": 0.10,
            }
        )
    )
    # size_fit=0.5 (secondary band) and engagement<1.0 (only 1 reply, no
    # meeting) deliberately keep the four deterministic components UNEQUAL —
    # redistributing weight among unequal scores must change the total. (An
    # earlier version of this test used all-1.0 scores, under which any
    # redistribution that keeps the non-intent weights summing to the same
    # 0.78 produces an identical total by construction — that was a test
    # bug, not scorer behaviour worth asserting on.)
    company = _make_company(employee_band="1-10", attributes={"business_model": _attr("agency")})
    contact = _make_contact(attributes={"seniority": _attr("founder_owner")})
    lead = _make_lead(budget_band=BudgetBand.FORTY_K_PLUS)

    result_a = score_deterministic(
        company=company, contact=contact, lead=lead, reply_count=1, meeting_count=0, pack=pack_a
    )
    result_b = score_deterministic(
        company=company, contact=contact, lead=lead, reply_count=1, meeting_count=0, pack=pack_b
    )

    assert result_a.deterministic_part != result_b.deterministic_part


# ---------------------------------------------------------------------------
# LLM sub-score combination is config-driven (M1.2 Correction 3)
# ---------------------------------------------------------------------------


def test_llm_subscore_combination_uses_configured_weights():
    subscores = {
        "buying_intent": {"score": 1.0, "evidence": [], "confidence": 0.9},
        "seniority_fit": {"score": 0.0, "evidence": [], "confidence": 0.9},
        "narrative_fit": {"score": 0.0, "evidence": [], "confidence": 0.9},
        "overall_note": "test",
    }
    all_weight_on_buying_intent = _make_pack(
        scoring=_make_scoring(
            llm_subscore_weights={"buying_intent": 1.0, "seniority_fit": 0.0, "narrative_fit": 0.0}
        )
    )
    equal_thirds = _make_pack(scoring=_make_scoring())

    llm_part_concentrated, _ = _combine_llm_subscores(
        subscores=subscores, pack=all_weight_on_buying_intent
    )
    llm_part_equal, _ = _combine_llm_subscores(subscores=subscores, pack=equal_thirds)

    # weights.intent = 0.22 in both packs; only llm_subscore_weights differs.
    assert llm_part_concentrated == pytest.approx(100.0 * 0.22 * 1.0)
    assert llm_part_equal == pytest.approx(100.0 * 0.22 * 0.3334)
    assert llm_part_concentrated != llm_part_equal


# ---------------------------------------------------------------------------
# Engagement counts meetings, not just replies (M1.2 Correction 2)
# ---------------------------------------------------------------------------


def test_engagement_score_counts_meetings_not_just_replies():
    pack = _make_pack()
    company = _make_company()
    contact = _make_contact()
    lead = _make_lead()

    zero_engagement = score_deterministic(
        company=company, contact=contact, lead=lead, reply_count=0, meeting_count=0, pack=pack
    )
    meeting_only = score_deterministic(
        company=company, contact=contact, lead=lead, reply_count=0, meeting_count=1, pack=pack
    )

    assert zero_engagement.components["engagement"]["score"] == 0.0
    # engagement_points.meeting=5.0, engagement_saturation=5.0 -> saturates at 1.0
    assert meeting_only.components["engagement"]["score"] == 1.0
    assert meeting_only.deterministic_part > zero_engagement.deterministic_part
