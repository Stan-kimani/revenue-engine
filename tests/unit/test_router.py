"""Unit tests for orchestrator/router.py — pure functions, no I/O, no database
(build-spec §8.1)."""

from __future__ import annotations

from revenue_engine.orchestrator.router import JobSpec, is_covered, route


def test_exact_route_takes_precedence():
    assert route("lead.captured") == [JobSpec("leadgen.enrich")]


def test_fan_out_reply_received_routes_to_two_jobs():
    assert route("reply.received") == [
        JobSpec("qualification.score"),
        JobSpec("sales.handle_reply"),
    ]


def test_prefix_routing_matches_lead_qualified_sql_to_lead_qualified_star():
    assert route("lead.qualified.sql") == [JobSpec("sales.draft_outreach")]


def test_mql_reaches_the_draft_handler_which_gates_on_pack_draft_bands():
    """M1.4a: mql is routed (not carved out); agents/sales.py no-ops unless the
    pinned pack's outreach.draft_bands includes mql."""
    assert route("lead.qualified.mql") == [JobSpec("sales.draft_outreach")]


def test_exact_unconsumed_carves_out_of_the_broader_prefix_route():
    """warm/cold each carry an exact UNCONSUMED entry — without that, they'd
    fall through to the same lead.qualified.* prefix as sql."""
    assert route("lead.qualified.warm") == []
    assert route("lead.qualified.cold") == []


def test_deal_closed_prefix_is_unconsumed_not_routed():
    assert route("deal.closed.won") == []
    assert route("deal.closed.lost") == []


def test_unconsumed_prefix_is_covered():
    assert is_covered("deal.closed.won") is True
    assert is_covered("deal.closed.lost") is True


def test_unknown_event_type_is_neither_routed_nor_covered():
    assert route("totally.made.up.event") == []
    assert is_covered("totally.made.up.event") is False


def test_routed_and_unconsumed_event_types_are_covered():
    assert is_covered("lead.captured") is True
    assert is_covered("lead.qualified.mql") is True
