"""Lead Generation agent (agent-contracts.md §1, build-spec M1.1).

Public surface is exactly one job handler, `handle_enrich(conn, job)`,
registered in scripts/run_worker.py's `HANDLERS["leadgen.enrich"]` —
`orchestrator/router.py` already routes `lead.captured` to that job type
(M0.3). This module never imports another agent (CLAUDE.md §1 non-negotiable
5); it reads via db/repositories, judges via core/llm.complete_json, and acts
via integrations/prospecting — nothing else.

Runs all three leadgen LLM tasks — enrich_company, enrich_decision_maker,
then build_prospect_profile (which depends on the first two's output) —
before writing anything. If any of the three fails validation twice
(core/llm.py::complete_json()'s own internal retry), nothing is written and
`lead.enrichment_failed` is emitted instead (M1.1 "no partial write"
requirement: a lead is never left with an enriched company and no matching
contact/profile).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from ..core import events as core_events
from ..core.config import IndustryPack, get_config
from ..core.errors import LLMValidationError, RevenueEngineError
from ..core.llm import AnthropicClientProtocol, complete_json
from ..core.observability import TraceContext
from ..db import repositories as repo
from ..db.models import AgentRunStatus, Company, Contact, Job, LeadStatus

ACTOR = "agent:leadgen"


async def handle_enrich(
    conn: asyncpg.Connection,
    job: Job,
    *,
    client: AnthropicClientProtocol | None = None,
) -> None:
    """`job.payload` is the copied `lead.captured` event payload plus
    `source_event_id`/`correlation_id` (core/queue.py::enqueue_for_event) —
    `causation_id` for anything this handler emits is `source_event_id`, the
    triggering event's own id.

    `client` is a test injection point, the same pattern as
    core/llm.py::complete_json()'s own `client` parameter. There is no
    provider here any more: enrichment stopped verifying email at M1.4a
    (see below).
    """

    lead_id = UUID(job.payload["lead_id"])
    contact_id = UUID(job.payload["contact_id"])
    company_id_raw = job.payload.get("company_id")
    company_id = UUID(company_id_raw) if company_id_raw else None
    correlation_id = UUID(job.payload["correlation_id"])
    causation_id = UUID(job.payload["source_event_id"])

    lead = await repo.get_lead(conn, lead_id)
    if lead is None:
        raise RevenueEngineError(f"leadgen.enrich: lead not found: {lead_id}")
    contact = await repo.get_contact(conn, contact_id)
    if contact is None:
        raise RevenueEngineError(f"leadgen.enrich: contact not found: {contact_id}")
    company = await repo.get_company(conn, company_id) if company_id is not None else None

    await repo.update_lead_status(conn, lead_id, LeadStatus.ENRICHING)

    if company is None:
        # agent-contracts.md §1's `lead.enrichment_failed` reason enum names
        # this case explicitly — a lead with no company to research, not an
        # anomaly worth a raised exception / job retry.
        await _emit_enrichment_failed(
            conn,
            lead_id=lead_id,
            reason="no_domain",
            attempts=1,
            last_error="lead has no associated company to enrich",
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        await repo.update_lead_status(conn, lead_id, LeadStatus.ENRICH_FAILED)
        return

    pack = get_config().pack
    trace = TraceContext(trace_id=str(job.id), correlation_id=correlation_id)
    raw_research = _raw_research_text(company=company, contact=contact)

    try:
        company_enrichment = await complete_json(
            "leadgen/enrich_company.md",
            {
                "company_name": company.name,
                "domain": company.domain or "",
                "raw_research": raw_research,
                "industry_pack_vocabulary": _vocabulary_text(pack),
            },
            "outputs/company_enrichment.json",
            trace,
            conn=conn,
            correlation_id=correlation_id,
            actor=ACTOR,
            causation_id=causation_id,
            client=client,
        )
        contact_enrichment = await complete_json(
            "leadgen/enrich_decision_maker.md",
            {
                "full_name": _contact_display_name(contact),
                "title": contact.title or "",
                "company_summary": company.name,
                "raw_research": raw_research,
            },
            "outputs/contact_enrichment.json",
            trace,
            conn=conn,
            correlation_id=correlation_id,
            actor=ACTOR,
            causation_id=causation_id,
            client=client,
        )
        prospect_profile = await complete_json(
            "leadgen/build_prospect_profile.md",
            {
                "company_enrichment": company_enrichment,
                "contact_enrichment": contact_enrichment,
                "icp_definition": _icp_definition_text(pack),
                "raw_research": raw_research,
            },
            "outputs/prospect_profile.json",
            trace,
            conn=conn,
            correlation_id=correlation_id,
            actor=ACTOR,
            causation_id=causation_id,
            client=client,
        )
    except LLMValidationError as exc:
        # complete_json() already retried once internally and emitted its
        # own llm.validation_failed (core/llm.py) — this is the
        # leadgen-level conversion of THAT failure into the business-outcome
        # event agent-contracts.md documents (universal failure rule 0.5),
        # not a second, separate retry loop. `attempts=2` is complete_json's
        # own fixed internal attempt count for the one prompt that failed
        # (core/llm.py::_MAX_ATTEMPTS), not a job-level retry count — logged
        # as a judgment call in docs/decisions.md since agent-contracts.md's
        # "after 3 attempts" language isn't precisely defined at this
        # milestone.
        await _emit_enrichment_failed(
            conn,
            lead_id=lead_id,
            reason="provider_error",
            attempts=2,
            last_error=str(exc),
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        await repo.update_lead_status(conn, lead_id, LeadStatus.ENRICH_FAILED)
        return

    # M1.4a: enrichment no longer verifies. Verification happens once, at
    # import (scripts/import_leads.py -> integrations/email_verification.py),
    # and contacts.email_status is write-once from there. Re-verifying here
    # with the syntax-only CSV provider silently clobbered a paid verdict back
    # to 'unverified' — see docs/decisions.md.

    company_run = await repo.get_latest_agent_run(
        conn,
        agent=ACTOR,
        prompt_id="leadgen/enrich_company",
        trigger_event=causation_id,
        status=AgentRunStatus.SUCCESS,
    )
    contact_run = await repo.get_latest_agent_run(
        conn,
        agent=ACTOR,
        prompt_id="leadgen/enrich_decision_maker",
        trigger_event=causation_id,
        status=AgentRunStatus.SUCCESS,
    )
    profile_run = await repo.get_latest_agent_run(
        conn,
        agent=ACTOR,
        prompt_id="leadgen/build_prospect_profile",
        trigger_event=causation_id,
        status=AgentRunStatus.SUCCESS,
    )
    if company_run is None or contact_run is None or profile_run is None:
        # Should be unreachable: each complete_json() call above just
        # succeeded, which means it just inserted exactly this row
        # (docs/decisions.md, get_latest_agent_run's own docstring). A typed
        # exception here, not a silent skip, is the honest response to a
        # provenance chain that couldn't be recovered.
        raise RevenueEngineError(
            "leadgen.enrich: a complete_json() call succeeded but its "
            "agent_runs row could not be read back (get_latest_agent_run)"
        )

    min_confidence = pack.scoring.min_confidence_to_store
    company_attributes, company_fields = _company_attribute_envelopes(
        company_enrichment,
        source="llm:enrich_company",
        run_id=company_run.id,
        min_confidence=min_confidence,
    )
    contact_attributes, contact_fields = _contact_attribute_envelopes(
        contact_enrichment,
        source="llm:enrich_decision_maker",
        run_id=contact_run.id,
        min_confidence=min_confidence,
    )

    await repo.upsert_company(
        conn,
        name=company.name,
        domain=company.domain,
        linkedin_url=company.linkedin_url,
        country=company.country,
        employee_band=company.employee_band,
        attributes=company_attributes,
    )
    await repo.upsert_contact(
        conn,
        email=contact.email,
        full_name=contact.full_name,
        first_name=contact.first_name,
        last_name=contact.last_name,
        title=contact.title,
        linkedin_url=contact.linkedin_url,
        company_id=contact.company_id,
        attributes=contact_attributes,
    )
    await repo.update_lead_profile(
        conn,
        lead_id,
        {
            **prospect_profile,
            "run_id": str(profile_run.id),
            "prompt_version": profile_run.prompt_version,
            "generated_at": datetime.now(UTC).isoformat(),
        },
    )

    await core_events.emit(
        conn,
        type="lead.enriched",
        payload={
            "lead_id": str(lead_id),
            "contact_id": str(contact_id),
            "company_id": str(company_id) if company_id else None,
            "fields_enriched": company_fields + contact_fields,
            # The stored verdict, not a fresh check (upsert_contact preserves it).
            "email_status": contact.email_status.value,
            "run_id": str(profile_run.id),
        },
        correlation_id=correlation_id,
        actor=ACTOR,
        idempotency_key=f"lead:{lead_id}:enriched",
        causation_id=causation_id,
    )


async def _emit_enrichment_failed(
    conn: asyncpg.Connection,
    *,
    lead_id: UUID,
    reason: str,
    attempts: int,
    last_error: str,
    correlation_id: UUID,
    causation_id: UUID,
) -> None:
    await core_events.emit(
        conn,
        type="lead.enrichment_failed",
        payload={
            "lead_id": str(lead_id),
            "reason": reason,
            "attempts": attempts,
            "last_error": last_error,
        },
        correlation_id=correlation_id,
        actor=ACTOR,
        # attempts in the key, mirroring core/queue.py::_emit_dead_lettered's
        # own pattern — a later failure with a different attempt count must
        # not be swallowed by an earlier one's idempotency_key.
        idempotency_key=f"lead:{lead_id}:enrichment_failed:{attempts}",
        causation_id=causation_id,
    )


# ============================================================================
# Attribute envelopes (entity-model.md §2) — model supplies value/confidence/
# evidence, code adds source/run_id/observed_at, per field below the pack's
# min_confidence_to_store is simply not written (phase1-llm-boundary.md §2).
# ============================================================================


def _envelope_from_field(
    field: Any, *, source: str, run_id: UUID, min_confidence: float
) -> dict[str, Any] | None:
    """One model-supplied `{value, confidence, evidence}` sub-object from a
    schemas/outputs/*.json response -> the full attribute envelope, or None
    if under the confidence threshold."""
    if not isinstance(field, dict):
        return None
    confidence = field.get("confidence")
    if not isinstance(confidence, int | float) or confidence < min_confidence:
        return None
    return {
        "value": field.get("value"),
        "confidence": confidence,
        "evidence": field.get("evidence"),
        "source": source,
        "run_id": str(run_id),
        "observed_at": datetime.now(UTC).isoformat(),
    }


def _envelope_from_items(
    items: Any, *, source: str, run_id: UUID, min_confidence: float
) -> dict[str, Any] | None:
    """Wraps a whole array (tech_signals, likely_responsibilities,
    inferred_pains) as one envelope's `value` — these fields carry a
    confidence PER ITEM (or, for tech_signals, none at all), not one for the
    field itself, so there is no single model-supplied top-level confidence
    to read (docs/decisions.md, M1.1: "tech_signals as one array-valued
    envelope", extended to the other two array-of-items fields for the same
    reason). The envelope's confidence is the max across items — tech_signals
    items carry no confidence field at all and are treated as fully
    confident (1.0), since the schema already requires each one to cite
    `evidence` to be included. Empty, or all-below-threshold, returns None."""
    if not isinstance(items, list) or not items:
        return None
    confidences = [
        i.get("confidence", 1.0) for i in items if isinstance(i, dict) and "confidence" in i
    ]
    confidence = max(confidences) if confidences else 1.0
    if confidence < min_confidence:
        return None
    return {
        "value": items,
        "confidence": confidence,
        "evidence": None,
        "source": source,
        "run_id": str(run_id),
        "observed_at": datetime.now(UTC).isoformat(),
    }


_COMPANY_SCALAR_FIELDS = (
    "industry",
    "sub_industry",
    "business_model",
    "employee_band",
    "revenue_signal",
    "positioning_summary",
)


def _company_attribute_envelopes(
    output: dict[str, Any], *, source: str, run_id: UUID, min_confidence: float
) -> tuple[dict[str, Any], list[str]]:
    attributes: dict[str, Any] = {}
    fields: list[str] = []
    for field_name in _COMPANY_SCALAR_FIELDS:
        envelope = _envelope_from_field(
            output.get(field_name), source=source, run_id=run_id, min_confidence=min_confidence
        )
        if envelope is not None:
            attributes[field_name] = envelope
            fields.append(field_name)

    tech_signals_envelope = _envelope_from_items(
        output.get("tech_signals"), source=source, run_id=run_id, min_confidence=min_confidence
    )
    if tech_signals_envelope is not None:
        attributes["tech_signals"] = tech_signals_envelope
        fields.append("tech_signals")

    return attributes, fields


_CONTACT_SCALAR_FIELDS = ("seniority", "decision_authority", "functional_area")
_CONTACT_ARRAY_FIELDS = ("likely_responsibilities", "inferred_pains")


def _contact_attribute_envelopes(
    output: dict[str, Any], *, source: str, run_id: UUID, min_confidence: float
) -> tuple[dict[str, Any], list[str]]:
    attributes: dict[str, Any] = {}
    fields: list[str] = []
    for field_name in _CONTACT_SCALAR_FIELDS:
        envelope = _envelope_from_field(
            output.get(field_name), source=source, run_id=run_id, min_confidence=min_confidence
        )
        if envelope is not None:
            attributes[field_name] = envelope
            fields.append(field_name)

    for field_name in _CONTACT_ARRAY_FIELDS:
        envelope = _envelope_from_items(
            output.get(field_name), source=source, run_id=run_id, min_confidence=min_confidence
        )
        if envelope is not None:
            attributes[field_name] = envelope
            fields.append(field_name)

    return attributes, fields


# ============================================================================
# Prompt variable text — built from what's actually known (CSV import) or
# the industry pack. No web search/fetch tool exists yet (out of scope for
# M1.1); enrich_company/enrich_decision_maker are explicitly designed to
# handle input this sparse and return nulls + insufficient_context: true
# rather than guess (phase1-llm-boundary.md §2, §5).
# ============================================================================


def _contact_display_name(contact: Contact) -> str:
    if contact.full_name:
        return contact.full_name
    parts = [p for p in (contact.first_name, contact.last_name) if p]
    return " ".join(parts) if parts else contact.email


def _raw_research_text(*, company: Company, contact: Contact) -> str:
    lines = [f"Company name: {company.name}"]
    if company.domain:
        lines.append(f"Company domain: {company.domain}")
    lines.append(f"Contact name: {_contact_display_name(contact)}")
    if contact.title:
        lines.append(f"Contact title: {contact.title}")
    if contact.linkedin_url:
        lines.append(f"Contact LinkedIn: {contact.linkedin_url}")
    note = contact.attributes.get("import_note", {})
    note_value = note.get("value") if isinstance(note, dict) else None
    if note_value:
        lines.append(f"Note at import: {note_value}")
    return "\n".join(lines)


def _vocabulary_text(pack: IndustryPack) -> str:
    firmographics = pack.icp.get("firmographics", {})
    lines: list[str] = []
    business_models = firmographics.get("business_models")
    if business_models:
        lines.append("Business models: " + ", ".join(business_models))
    employee_bands = firmographics.get("employee_bands")
    if employee_bands:
        lines.append("Primary employee bands: " + ", ".join(employee_bands))
    geographies = firmographics.get("geographies_preferred")
    if geographies:
        lines.append("Preferred geographies: " + ", ".join(geographies))
    return "\n".join(lines)


def _icp_definition_text(pack: IndustryPack) -> str:
    lines: list[str] = []
    summary = pack.icp.get("summary")
    if summary:
        lines.append(str(summary).strip())
    vocabulary = _vocabulary_text(pack)
    if vocabulary:
        lines.append(vocabulary)
    target_titles = pack.icp.get("roles", {}).get("target_titles")
    if target_titles:
        lines.append("Target roles: " + ", ".join(target_titles))
    return "\n".join(lines)
