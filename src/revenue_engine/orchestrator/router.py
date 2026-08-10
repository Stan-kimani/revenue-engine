"""Event type -> job(s) mapping. The only routing table (build-spec §2).

A dict, not a chain of conditionals. Supports prefix subscription: a key
ending in `.*` matches any event type sharing that prefix (`"deal.closed.*"`
matches `"deal.closed.won"`).

Match precedence, in order — see `route()`:
  1. Exact key in ROUTES
  2. Exact key in UNCONSUMED (explicitly declared "no handler yet")
  3. Prefix key in ROUTES
  4. Prefix key in UNCONSUMED
An exact UNCONSUMED entry deliberately outranks a prefix ROUTES match. This
is what lets `"lead.qualified.*"` route to Sales while
`lead.qualified.mql`/`lead.qualified.warm`/`lead.qualified.cold` — each
listed explicitly in UNCONSUMED — are carved out of that same prefix instead
of being silently swept into it.

UNCONSUMED is a deliberate, reasoned allowlist (event-catalog.md §9
"Verification rule"), not an oversight tracker: every event type documented
in docs/event-catalog.md must appear as either a ROUTES key (exact or prefix)
or an UNCONSUMED key (exact or prefix), enforced by
tests/contracts/test_router_coverage.py. An event type appearing in neither
is what that contract test exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobSpec:
    job_type: str


# ============================================================================
# ROUTES — Phase 1 events with a Phase-1-agent consumer (leadgen, qualification,
# sales — agent-contracts.md §1-§3). None of these agents exist yet (M1.1-M1.4);
# the job_type strings describe what will eventually claim and run these jobs.
# ============================================================================

ROUTES: dict[str, list[JobSpec]] = {
    "lead.captured": [JobSpec("leadgen.enrich")],
    "lead.enriched": [JobSpec("qualification.score")],
    # Fan-out: qualification re-scores on engagement AND sales handles the
    # reply — one event, two independent consumers (event-catalog.md §R1).
    "reply.received": [JobSpec("qualification.score"), JobSpec("sales.handle_reply")],
    # Prefix: lead.qualified.sql routes here; mql/warm/cold are carved out
    # below via exact UNCONSUMED entries (see module docstring).
    "lead.qualified.*": [JobSpec("sales.start_sequence")],
    "followup.due": [JobSpec("sales.send_followup")],
    # agent-contracts.md §3 lists meeting.requested as a Sales consume
    # trigger, though event-catalog.md's own section header says Sales
    # *emits* it — the two docs disagree; followed agent-contracts.md's more
    # specific per-agent contract. Logged in docs/decisions.md.
    "meeting.requested": [JobSpec("sales.book_meeting")],
    "approval.granted": [JobSpec("sales.resume_gated_action")],
}


# ============================================================================
# UNCONSUMED — every other event type documented in event-catalog.md, with why.
# ============================================================================

UNCONSUMED: dict[str, str] = {
    # --- Phase 1 events with no Phase-1-agent consumer yet ---
    "campaign.published": (
        "Discovery is demand-driven (discovery.requested), not campaign-triggered "
        "(discovery-addendum.md) — no Phase-1-agent job is specified for this event."
    ),
    "lead.deferred": (
        "Consumed by a scheduled re-evaluation (orchestrator/schedules.py), not a "
        "routed job. schedules.py is not part of M0.3."
    ),
    "lead.enrichment_failed": (
        "Consumed by the human review queue / ops notifier (Slack). Slack "
        "integration is M1.3, not built."
    ),
    "lead.scored": (
        "Band decision is already carried in the payload; sole further consumer "
        "is Learning (Phase 3), not built."
    ),
    "lead.qualified.cold": "No consumer — cold leads take no action (agent-contracts.md §2).",
    "lead.qualified.mql": "Nurture is Phase 4, not built.",
    "lead.qualified.warm": "Nurture is Phase 4, not built.",
    "lead.routed_to_human": "Consumed by the Slack notifier. Slack integration is M1.3, not built.",
    "outreach.drafted": (
        "Consumed by the approval gate (core/approvals.py) or a send job. "
        "approvals.py is M1.3, not built."
    ),
    "outreach.sent": (
        "Consumed by the sequence scheduler (orchestrator/sequences.py, not in "
        "M0.3) and CRM Sync (Phase 2, not built)."
    ),
    "outreach.blocked": "Consumed by the ops notifier. Slack integration is M1.3, not built.",
    "reply.classified": "Consumed by CRM Sync (Phase 2), not built.",
    "reply.unmatched": "Consumed by the Slack notifier (event-catalog.md §7.2). M1.3, not built.",
    "deal.created": "Consumed by CRM Sync (Phase 2), not built.",
    "objection.logged": "Consumed by Learning (Phase 3), not built.",
    "contact.unsubscribed": "Consumed by CRM Sync (Phase 2), not built.",
    "meeting.scheduled": "Consumed by CRM Sync (Phase 2), not built.",
    "sequence.paused": (
        "Consumed by the sequence scheduler / CRM Sync; orchestrator/sequences.py is not in M0.3."
    ),
    "sequence.completed": (
        "Consumed by the sequence scheduler / CRM Sync; orchestrator/sequences.py is not in M0.3."
    ),
    # --- Discovery (discovery-addendum.md) — explicitly deferred past Phase 1 ---
    "discovery.requested": (
        "Discovery is out of scope until the ManualCsvProvider pilot clears its "
        "threshold — discovery-addendum.md §8 build order."
    ),
    "discovery.completed": "Same as discovery.requested — discovery-addendum.md §8.",
    "discovery.budget_exhausted": "Same as discovery.requested — discovery-addendum.md §8.",
    "discovery.yield_low": "Same as discovery.requested — discovery-addendum.md §8.",
    # --- Phase 2 events (event-catalog.md §4) ---
    "meeting.completed": "Phase 2 (meeting_intel, M2.1), not built.",
    "meeting.analyzed": (
        "Phase 2 emitter (meeting_intel, M2.1); Phase 2/3 consumers (CRM Sync, "
        "Learning), neither built."
    ),
    "crm.updated": "Phase 2 (crm_sync, M2.2), not built.",
    "crm.merge_proposed": "Phase 2 (crm_sync, M2.2), not built.",
    "deal.stage_changed": "Phase 2 (crm_sync, M2.2), not built.",
    "deal.closed.*": (
        "Phase 2 emitter (crm_sync, M2.2); Phase 3 consumer (learning, M3.1). Neither built."
    ),
    # --- Phase 3 events (event-catalog.md §5) ---
    "learning.published": "Phase 3 (learning, M3.1), not built.",
    "messaging.update_proposed": "Phase 3 (learning, M3.1), not built.",
    "icp.update_proposed": "Phase 3/4 (learning or market_intel), not built.",
    "sequence.update_proposed": "Phase 3 (learning, M3.1), not built.",
    # --- Phase 4 events (event-catalog.md §6) ---
    "market.insight_published": "Phase 4 (market_intel, M4.1), not built.",
    "campaign.assets_created": "Phase 4 (marketing, M4.2), not built.",
    # --- Operational/plumbing events (event-catalog.md §7) — never consumed
    # by domain agents by design; ops notifier is M1.3. ---
    "approval.requested": "Consumed by the ops notifier. Slack integration is M1.3, not built.",
    "approval.denied": "Consumed by the ops notifier. Slack integration is M1.3, not built.",
    "approval.expired": "Consumed by the ops notifier. Slack integration is M1.3, not built.",
    "job.dead_lettered": (
        "Emitted by core/queue.py itself (M0.3). Consumed by the ops notifier. "
        "Slack integration is M1.3, not built."
    ),
    "llm.validation_failed": "Emitted by core/llm.py, which is M0.4, not built.",
    "agent.run_failed": "Emitted by agent handlers, which don't exist yet.",
    "integration.degraded": "Emitted by integrations/*, which don't exist yet.",
}


def _prefix_lookup(event_type: str, keys: list[str]) -> bool:
    return any(key.endswith(".*") and event_type.startswith(key[:-1]) for key in keys)


def route(event_type: str) -> list[JobSpec]:
    """Resolve one event type to zero or more job specs, per the precedence
    order in this module's docstring. An event type covered by neither
    ROUTES nor UNCONSUMED (a genuine gap — see is_covered()) returns an empty
    list rather than raising: a routing-table gap should never crash the
    dispatch loop, but it is never silent either — the contract test
    (tests/contracts/test_router_coverage.py) fails the build over it, and
    the caller is expected to log a warning when route() returns [] for a
    type that also isn't in UNCONSUMED.
    """
    if event_type in ROUTES:
        return ROUTES[event_type]
    if event_type in UNCONSUMED:
        return []
    for prefix, specs in ROUTES.items():
        if prefix.endswith(".*") and event_type.startswith(prefix[:-1]):
            return specs
    return []


def is_covered(event_type: str) -> bool:
    """True if `event_type` is accounted for — routed or explicitly
    unconsumed, exact or via prefix. Used by the contract test to verify
    every event type in docs/event-catalog.md is covered."""
    if event_type in ROUTES or event_type in UNCONSUMED:
        return True
    return _prefix_lookup(event_type, list(ROUTES)) or _prefix_lookup(event_type, list(UNCONSUMED))
