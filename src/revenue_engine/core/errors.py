"""Typed exceptions raised by repositories.py and, later, agents/orchestrator.

CLAUDE.md §4: never let a raw driver exception (asyncpg.PostgresError and
friends) surface past repositories.py. Callers catch these instead.
"""

from __future__ import annotations


class RevenueEngineError(Exception):
    """Base class for all typed errors raised in this codebase."""


class DuplicateActiveLeadError(RevenueEngineError):
    """Raised when creating a lead would violate the single-thread rule.

    Covers both `one_active_lead_per_contact` (D2) and
    `one_active_lead_per_company` (R1) — the caller distinguishes via
    `constraint_name`. Converting the underlying UniqueViolation into this is
    what lets a caller emit `lead.deferred` instead of crashing
    (event-catalog.md §3, "Implementation note").
    """

    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name
        super().__init__(f"Duplicate active lead: {constraint_name}")


class InvalidAttributeEnvelopeError(RevenueEngineError):
    """Raised when a value written to an `attributes` JSONB column does not
    match schemas/entities/attribute.json (entity-model.md §2)."""

    def __init__(self, field: str, errors: list[str]) -> None:
        self.field = field
        self.errors = errors
        super().__init__(f"Invalid attribute envelope for '{field}': {'; '.join(errors)}")
