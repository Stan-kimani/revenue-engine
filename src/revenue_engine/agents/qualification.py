"""Lead Qualification agent (agent-contracts.md §2, build-spec M1.2).

Public surface is exactly one job handler, `handle_score(conn, job)`,
registered in scripts/run_worker.py's `HANDLERS["qualification.score"]` —
`orchestrator/router.py` already routes both `lead.enriched` and
`reply.received` to that job type (M0.3, anticipating this milestone). This
module never imports another agent (CLAUDE.md §1 non-negotiable 5); it reads
via db/repositories, judges the fuzzy sub-scores via core/llm.complete_json,
and has no external side effects at all (agent-contracts.md §2: "Tools
allowed: none").

THE SPLIT THAT MATTERS: scoring is hybrid, not negotiable.
  - `qualification/score_lead.md` returns ONLY three fuzzy sub-scores with
    evidence (buying_intent, seniority_fit, narrative_fit) — no total, no
    band. `additionalProperties: false` on outputs/lead_subscores.json
    already prevents either from sneaking in.
  - Everything else — ICP field matches, size fit, engagement, budget fit,
    disqualifiers, the weighted sum, and band assignment — is code, in the
    pure functions below (`score_deterministic`, `_combine_llm_subscores`,
    `_assign_band`). Every one of them takes plain data in and returns plain
    data out: no `conn`, no I/O, so tests/unit/test_qualification_scoring.py
    exercises them directly without a database.
  - `lead_scores.deterministic_part` and `llm_part` are stored separately so
    the two halves can be audited independently (entity-model.md §3.5).

Scoring always uses the lead's PINNED `industry_pack` (`_pinned_pack`,
loading by name via `core.config.load_config`), never `core.config.get_config()`'s
process-wide "currently active" pack — the single most important behavioural
requirement of this milestone (entity-model.md §3.4: "if the pack's weights
change next month, old scores must remain interpretable").
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any
from uuid import UUID

import asyncpg

from ..core import events as core_events
from ..core.config import IndustryPack, load_config
from ..core.disqualifiers import evaluate_rule, parse_rule
from ..core.errors import RevenueEngineError
from ..core.llm import AnthropicClientProtocol, complete_json
from ..core.observability import TraceContext
from ..db import repositories as repo
from ..db.models import (
    AgentRunStatus,
    Company,
    Contact,
    Job,
    Lead,
    LeadBand,
    LeadSource,
    LeadStatus,
)

ACTOR = "agent:qualification"

_INBOUND_SOURCES = frozenset({LeadSource.WEBFORM, LeadSource.INBOUND_REPLY, LeadSource.REFERRAL})
_LLM_SUBSCORE_KEYS = ("buying_intent", "seniority_fit", "narrative_fit")


@cache
def _pinned_pack(pack_name: str) -> IndustryPack:
    """Loads the SPECIFIC named pack a lead is pinned to
    (`leads.industry_pack`, entity-model.md §3.4) — never
    `core.config.get_config()`'s process-wide default. Cached per pack name
    for the process lifetime, mirroring `get_config()`'s own caching
    semantics: config is read once per process, a weight change takes effect
    on the next process restart, not mid-run."""
    return load_config(industry_pack=pack_name).pack


# ============================================================================
# Deterministic scorer — pure functions, no I/O (deliverable #2)
# ============================================================================


def _attr_value(attributes: dict[str, Any], field: str) -> str | None:
    """One field's `value` out of an entity-model.md §2 attribute envelope,
    or None if the field was never written (below min_confidence_to_store at
    leadgen time, or genuinely never enriched)."""
    envelope = attributes.get(field)
    if not isinstance(envelope, dict):
        return None
    value = envelope.get("value")
    return value if isinstance(value, str) else None


def _icp_match_score(
    *, company: Company | None, contact: Contact, pack: IndustryPack
) -> tuple[float, str]:
    """Business model, employee band, geography, and seniority weight —
    four equal-weighted 0/1-ish signals of "does this look like our ICP at
    all." Deliberately broader than `_size_fit_score` below, which grades
    employee_band on its own (primary vs. secondary) for a distinct config
    weight — the two must not double-count the same fact against two
    different weights."""
    firmographics = pack.icp.get("firmographics", {})
    roles = pack.icp.get("roles", {})

    business_model = _attr_value(company.attributes, "business_model") if company else None
    business_model_signal = (
        1.0 if business_model in firmographics.get("business_models", []) else 0.0
    )

    employee_band = company.employee_band if company else None
    in_range = employee_band is not None and (
        employee_band in firmographics.get("employee_bands", [])
        or employee_band in firmographics.get("employee_bands_secondary", [])
    )
    employee_band_signal = 1.0 if in_range else 0.0

    geography = company.country if company else None
    geography_signal = 1.0 if geography in firmographics.get("geographies_preferred", []) else 0.0

    seniority = _attr_value(contact.attributes, "seniority")
    seniority_weight_map = roles.get("seniority_weight", {})
    seniority_signal = float(seniority_weight_map.get(seniority, 0.0)) if seniority else 0.0

    score = (
        business_model_signal + employee_band_signal + geography_signal + seniority_signal
    ) / 4.0
    evidence = (
        f"business_model={business_model!r} employee_band={employee_band!r} "
        f"country={geography!r} seniority={seniority!r}"
    )
    return score, evidence


def _size_fit_score(*, company: Company | None, pack: IndustryPack) -> tuple[float, str]:
    """Primary vs. secondary employee band (agent-contracts.md §2 /
    config comment: 'scored lower, not excluded'). A graduated signal
    distinct from `_icp_match_score`'s coarser in-range-at-all check."""
    firmographics = pack.icp.get("firmographics", {})
    band = company.employee_band if company else None
    if band and band in firmographics.get("employee_bands", []):
        return 1.0, f"employee_band={band!r} in primary range"
    if band and band in firmographics.get("employee_bands_secondary", []):
        return 0.5, f"employee_band={band!r} in secondary range"
    return 0.0, f"employee_band={band!r} outside configured ranges"


def _engagement_score(
    *, reply_count: int, meeting_count: int, pack: IndustryPack
) -> tuple[float, str]:
    """Replies (from `messages`) and meetings (from `meetings`) — meetings
    are the strongest engagement signal available and are weighted far
    higher via `pack.scoring.engagement_points` (M1.2 Correction 2,
    docs/decisions.md). Opens/clicks are absent from this formula entirely,
    not present at weight 0: `messages` has no opened_at/clicked_at column
    (migrations/0001), so there is nothing to count yet — an instrumentation
    gap, not a deliberate down-weighting choice."""
    points = pack.scoring.engagement_points
    raw = reply_count * points.get("reply", 0.0) + meeting_count * points.get("meeting", 0.0)
    saturation = pack.scoring.engagement_saturation
    score = min(1.0, raw / saturation) if saturation > 0 else 0.0
    evidence = f"replies={reply_count} meetings={meeting_count} raw_points={raw}"
    return score, evidence


def _budget_fit_score(*, lead: Lead, pack: IndustryPack) -> tuple[float, str]:
    key = lead.budget_band.value if lead.budget_band else "unknown"
    budget_map = pack.scoring.budget_fit_map
    score = float(budget_map.get(key, budget_map.get("unknown", 0.0)))
    return score, f"budget_band={key!r}"


@dataclass(frozen=True)
class DisqualifierHit:
    id: str
    reason: str


def evaluate_disqualifiers(*, company: Company | None, pack: IndustryPack) -> list[DisqualifierHit]:
    """Only `icp.disqualifiers[]` entries NOT marked `enforcement: manual`
    are evaluated here — core/config.py already guaranteed at boot that every
    one of those parses under core/disqualifiers.py's grammar (M1.2
    Correction 1), so `parse_rule` below is never expected to raise; if it
    somehow does, that is a config/loader drift bug, not a data problem, and
    surfaces as a RevenueEngineError rather than silently skipping the
    disqualifier."""
    business_model = _attr_value(company.attributes, "business_model") if company else None
    revenue_signal = _attr_value(company.attributes, "revenue_signal") if company else None
    employee_band = company.employee_band if company else None

    hits: list[DisqualifierHit] = []
    for entry in pack.icp.get("disqualifiers", []):
        if entry.get("enforcement") == "manual":
            continue
        try:
            parsed = parse_rule(entry["rule"])
        except Exception as exc:  # noqa: BLE001 - see docstring: should be unreachable post-boot
            raise RevenueEngineError(
                f"disqualifier '{entry['id']}' failed to parse at scoring time despite "
                "passing core/config.py's boot-time check — pack/loader drift"
            ) from exc
        if evaluate_rule(
            parsed,
            employee_band=employee_band,
            business_model=business_model,
            revenue_signal=revenue_signal,
        ):
            hits.append(DisqualifierHit(id=entry["id"], reason=entry["reason"]))
    return hits


@dataclass(frozen=True)
class DeterministicScoreResult:
    icp_match: float
    size_fit: float
    engagement: float
    budget_fit: float
    disqualifiers: tuple[DisqualifierHit, ...]
    deterministic_part: float
    """0-100 scale: 100 * sum(weight * component_score) across the four
    deterministic components."""
    components: dict[str, Any]
    """The deterministic slice of lead_scores.components (entity-model.md
    §3.5) — {icp_match: {score, weight, evidence}, ..., disqualifiers_hit: [...]}."""


def score_deterministic(
    *,
    company: Company | None,
    contact: Contact,
    lead: Lead,
    reply_count: int,
    meeting_count: int,
    pack: IndustryPack,
) -> DeterministicScoreResult:
    """The pure function deliverable #2 asks for: everything in this agent
    that is a rule rather than a judgment, in one place, callable with plain
    constructed objects and no database (tests/unit/test_qualification_scoring.py)."""
    icp_score, icp_evidence = _icp_match_score(company=company, contact=contact, pack=pack)
    size_score, size_evidence = _size_fit_score(company=company, pack=pack)
    engagement_score, engagement_evidence = _engagement_score(
        reply_count=reply_count, meeting_count=meeting_count, pack=pack
    )
    budget_score, budget_evidence = _budget_fit_score(lead=lead, pack=pack)
    disqualifier_hits = evaluate_disqualifiers(company=company, pack=pack)

    weights = pack.scoring.weights
    deterministic_part = 100.0 * (
        weights["icp_match"] * icp_score
        + weights["size_fit"] * size_score
        + weights["engagement"] * engagement_score
        + weights["budget_fit"] * budget_score
    )
    components = {
        "icp_match": {"score": icp_score, "weight": weights["icp_match"], "evidence": icp_evidence},
        "size_fit": {"score": size_score, "weight": weights["size_fit"], "evidence": size_evidence},
        "engagement": {
            "score": engagement_score,
            "weight": weights["engagement"],
            "evidence": engagement_evidence,
        },
        "budget_fit": {
            "score": budget_score,
            "weight": weights["budget_fit"],
            "evidence": budget_evidence,
        },
        "disqualifiers_hit": [{"id": h.id, "reason": h.reason} for h in disqualifier_hits],
    }
    return DeterministicScoreResult(
        icp_match=icp_score,
        size_fit=size_score,
        engagement=engagement_score,
        budget_fit=budget_score,
        disqualifiers=tuple(disqualifier_hits),
        deterministic_part=deterministic_part,
        components=components,
    )


def _combine_llm_subscores(
    *, subscores: dict[str, Any], pack: IndustryPack
) -> tuple[float, dict[str, Any]]:
    """Combines score_lead.md's three fuzzy sub-scores into the pack's
    single `weights["intent"]` component, using `pack.scoring.llm_subscore_weights`
    — a scoring decision that lives in config, not a hardcoded formula (M1.2
    Correction 3, docs/decisions.md), so tuning it later doesn't require a
    code change or make historical `lead_scores` rows uninterpretable."""
    sub_weights = pack.scoring.llm_subscore_weights
    intent_score = sum(
        float(subscores[k]["score"]) * float(sub_weights[k]) for k in _LLM_SUBSCORE_KEYS
    )
    llm_part = 100.0 * pack.scoring.weights["intent"] * intent_score
    component = {
        "score": intent_score,
        "weight": pack.scoring.weights["intent"],
        "sub_weights": dict(sub_weights),
        "sub_scores": {
            k: {
                "score": subscores[k]["score"],
                "evidence": subscores[k]["evidence"],
                "confidence": subscores[k]["confidence"],
            }
            for k in _LLM_SUBSCORE_KEYS
        },
        "overall_note": subscores.get("overall_note", ""),
    }
    return llm_part, component


def _assign_band(total: float, pack: IndustryPack, *, disqualified: bool) -> LeadBand:
    """Band assignment from `scoring.bands` thresholds — never LLM-decided
    (agent-contracts.md §2). A disqualifier hit forces `cold` regardless of
    the numeric total; the total itself is still stored, for audit, rather
    than zeroed."""
    if disqualified:
        return LeadBand.COLD
    bands = pack.scoring.bands
    if total >= bands["sql"]:
        return LeadBand.SQL
    if total >= bands["mql"]:
        return LeadBand.MQL
    if total >= bands["warm"]:
        return LeadBand.WARM
    return LeadBand.COLD


# ============================================================================
# Prompt variable text — local to this agent (agents never import other
# agents, CLAUDE.md §1 non-negotiable 5 / agent-contracts.md §10 rule 1 — a
# near-identical helper already exists in agents/leadgen.py for its own
# prompts; small enough that duplicating it here is cheaper than the risk of
# touching M1.1's shipped code for a milestone scoped to qualification only).
# ============================================================================


def _icp_definition_text(pack: IndustryPack) -> str:
    lines: list[str] = []
    summary = pack.icp.get("summary")
    if summary:
        lines.append(str(summary).strip())
    target_titles = pack.icp.get("roles", {}).get("target_titles")
    if target_titles:
        lines.append("Target roles: " + ", ".join(target_titles))
    return "\n".join(lines)


def _engagement_summary_text(*, reply_count: int, meeting_count: int) -> str:
    return f"{reply_count} inbound reply(ies), {meeting_count} meeting(s) so far."


# ============================================================================
# Handler
# ============================================================================


async def handle_score(
    conn: asyncpg.Connection,
    job: Job,
    *,
    client: AnthropicClientProtocol | None = None,
) -> None:
    """`job.payload` is the copied `lead.enriched` OR `reply.received` event
    payload plus `source_event_id`/`correlation_id`
    (core/queue.py::enqueue_for_event) — both carry `lead_id`, which is all
    this handler trusts from the payload; everything else is re-read from
    the database (event-catalog.md §R2)."""
    lead_id = UUID(job.payload["lead_id"])
    correlation_id = UUID(job.payload["correlation_id"])
    causation_id = UUID(job.payload["source_event_id"])

    lead = await repo.get_lead(conn, lead_id)
    if lead is None:
        raise RevenueEngineError(f"qualification.score: lead not found: {lead_id}")
    contact = await repo.get_contact(conn, lead.contact_id)
    if contact is None:
        raise RevenueEngineError(f"qualification.score: contact not found: {lead.contact_id}")
    company = await repo.get_company(conn, lead.company_id) if lead.company_id is not None else None

    pack = _pinned_pack(lead.industry_pack)

    reply_count = await repo.count_inbound_messages(conn, lead_id)
    meeting_count = await repo.count_meetings(conn, lead_id)

    deterministic = score_deterministic(
        company=company,
        contact=contact,
        lead=lead,
        reply_count=reply_count,
        meeting_count=meeting_count,
        pack=pack,
    )

    trace = TraceContext(trace_id=str(job.id), correlation_id=correlation_id)
    subscores = await complete_json(
        "qualification/score_lead.md",
        {
            "prospect_profile": lead.profile or {},
            "icp_definition": _icp_definition_text(pack),
            "engagement_summary": _engagement_summary_text(
                reply_count=reply_count, meeting_count=meeting_count
            ),
            "scoring_guidance": pack.scoring.guidance,
        },
        "outputs/lead_subscores.json",
        trace,
        conn=conn,
        correlation_id=correlation_id,
        actor=ACTOR,
        causation_id=causation_id,
        client=client,
    )

    run = await repo.get_latest_agent_run(
        conn,
        agent=ACTOR,
        prompt_id="qualification/score_lead",
        trigger_event=causation_id,
        status=AgentRunStatus.SUCCESS,
    )
    if run is None:
        # Should be unreachable: the complete_json() call above just
        # succeeded, which means it just inserted exactly this row (same
        # reasoning as agents/leadgen.py's identical check).
        raise RevenueEngineError(
            "qualification.score: complete_json() succeeded but its agent_runs "
            "row could not be read back (get_latest_agent_run)"
        )

    llm_part, intent_component = _combine_llm_subscores(subscores=subscores, pack=pack)
    disqualified = bool(deterministic.disqualifiers)
    components = {**deterministic.components, "intent": intent_component}

    # Round the two parts first, then derive `total` as their EXACT Decimal
    # sum — never round all three independently from the underlying floats.
    # Rounding total = deterministic_part + llm_part separately from each
    # part can disagree by a cent (e.g. 33.335 + 10.335 = 43.67 rounds
    # cleanly, but 33.335 alone and 10.335 alone can each round either up or
    # down), which would silently break the "deterministic_part + llm_part
    # reconciles to total" invariant the milestone's own test suite checks.
    deterministic_part_decimal = _to_decimal(deterministic.deterministic_part)
    llm_part_decimal = _to_decimal(llm_part)
    total_decimal = deterministic_part_decimal + llm_part_decimal
    band = _assign_band(float(total_decimal), pack, disqualified=disqualified)

    score_row = await repo.insert_lead_score(
        conn,
        lead_id=lead_id,
        total=total_decimal,
        band=band,
        components=components,
        deterministic_part=deterministic_part_decimal,
        llm_part=llm_part_decimal,
        prompt_version=run.prompt_version,
        model=run.model,
        run_id=run.id,
    )
    await repo.refresh_lead_band_and_score(
        conn,
        lead_id,
        current_score=total_decimal,
        band=band,
        status=LeadStatus.SCORED,
    )

    scored_event = await core_events.emit(
        conn,
        type="lead.scored",
        payload={
            "lead_id": str(lead_id),
            "score_id": str(score_row.id),
            "total": float(total_decimal),
            "band": band.value,
            "deterministic_part": float(deterministic_part_decimal),
            "llm_part": float(llm_part_decimal),
            "industry_pack": lead.industry_pack,
            "prompt_version": run.prompt_version,
            "run_id": str(run.id),
        },
        correlation_id=correlation_id,
        actor=ACTOR,
        idempotency_key=f"lead:{lead_id}:scored:{score_row.id}",
        causation_id=causation_id,
    )

    await core_events.emit(
        conn,
        type=f"lead.qualified.{band.value}",
        payload={
            "lead_id": str(lead_id),
            "band": band.value,
            "total": float(total_decimal),
            "campaign_id": str(lead.campaign_id) if lead.campaign_id else None,
            "source": lead.source.value,
        },
        correlation_id=correlation_id,
        actor=ACTOR,
        idempotency_key=f"lead:{lead_id}:qualified:{score_row.id}",
        causation_id=scored_event.event_id,
    )

    if lead.source in _INBOUND_SOURCES:
        # R2 inbound bypass — a source-based branch, never an inflated score
        # (entity-model.md §5, agent-contracts.md §2).
        await core_events.emit(
            conn,
            type="lead.routed_to_human",
            payload={
                "lead_id": str(lead_id),
                "band": band.value,
                "total": float(total_decimal),
                "source": lead.source.value,
                "reason": "inbound_bypass",
            },
            correlation_id=correlation_id,
            actor=ACTOR,
            idempotency_key=f"lead:{lead_id}:routed_to_human:{score_row.id}",
            causation_id=scored_event.event_id,
        )


def _to_decimal(value: float) -> Decimal:
    """lead_scores' numeric(5,2) columns want a Decimal, not a float
    (CLAUDE.md §1 non-negotiable 12 is about money specifically, but the same
    "never let a float silently drift" discipline applies here) — rounded
    once, at the persistence boundary, from the pure functions' float
    arithmetic."""
    return Decimal(str(round(value, 2)))
