"""Contract test: every event type documented in docs/event-catalog.md must be
either a ROUTES key or an UNCONSUMED key in orchestrator/router.py (exact or
prefix) — event-catalog.md §9 "Verification rule." An event type in neither
is what this test exists to catch.

Event types are extracted directly from the live document text (backtick-quoted,
dotted tokens, with `{a|b}` brace expansion for headings like
`lead.qualified.{cold|warm|mql|sql}`) rather than hand-copied into a constant
here, so a new event type added to event-catalog.md without a routing decision
fails this test instead of silently going unnoticed.

Three tokens matching the extraction pattern are not event types and are
excluded explicitly, by name, with why — not silently:
  - `send.email` — R1 gives it as the negative example of what NOT to name an
    event ("There is no `send.email` event").
  - `approvals.expiry` — a config key path (`config/thresholds.yaml` ->
    `approvals.expiry`), not an event.
  - `events.idempotency_key` — a column reference (`events.idempotency_key`),
    not an event.
"""

from __future__ import annotations

import re
from pathlib import Path

from revenue_engine.orchestrator import router

EVENT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "docs" / "event-catalog.md"

# Requires at least one dot, matching event-catalog.md's own naming rule (§R4:
# "<entity>.<past-tense-verb>, lowercase, dot-separated") — this is what keeps
# single-word backtick tokens like `thread_id` or `approval_id` out, without
# needing a hand-maintained allowlist of "real" event names.
_TOKEN_RE = re.compile(r"`([a-zA-Z0-9_.{}|*]+\.[a-zA-Z0-9_.{}|*]*)`")
_BRACE_RE = re.compile(r"\{([^}]+)\}")

_NOT_EVENT_TYPES = {
    "send.email",
    "approvals.expiry",
    "events.idempotency_key",
}


def _expand(token: str) -> list[str]:
    match = _BRACE_RE.search(token)
    if not match:
        return [token]
    alternatives = match.group(1).split("|")
    return [token[: match.start()] + alt + token[match.end() :] for alt in alternatives]


def _extract_documented_event_types() -> set[str]:
    text = EVENT_CATALOG_PATH.read_text(encoding="utf-8")
    found: set[str] = set()
    for token in _TOKEN_RE.findall(text):
        found.update(_expand(token))
    return found - _NOT_EVENT_TYPES


def test_extraction_finds_a_realistic_number_of_event_types():
    """Sanity check on the extraction itself, not the router: if this drops
    to near-zero, the regex broke, not the routing table."""
    documented = _extract_documented_event_types()
    assert len(documented) > 40


def test_every_documented_event_type_is_routed_or_unconsumed():
    documented = _extract_documented_event_types()
    uncovered = sorted(t for t in documented if not router.is_covered(t))
    assert uncovered == [], (
        f"Event type(s) in docs/event-catalog.md with no ROUTES or UNCONSUMED entry: {uncovered}"
    )
