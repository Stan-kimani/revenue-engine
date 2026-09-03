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


class UnknownEventTypeError(RevenueEngineError):
    """Raised when core/events.py::emit() is asked to emit a type with no
    schemas/events/<type>.json file. A typo in an event type must fail loudly,
    not create a new type by accident (event-catalog.md §9.2)."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(f"Unknown event type '{event_type}': no schemas/events/{event_type}.json")


class EventPayloadValidationError(RevenueEngineError):
    """Raised when an event payload fails validation against its schema.
    The event is never inserted (core/events.py::emit() validates before
    calling repositories.emit_event)."""

    def __init__(self, event_type: str, errors: list[str]) -> None:
        self.event_type = event_type
        self.errors = errors
        super().__init__(f"Invalid payload for event '{event_type}': {'; '.join(errors)}")


class DisqualifierRuleError(RevenueEngineError):
    """Raised by core/disqualifiers.py::parse_rule() when a pack's
    icp.disqualifiers[].rule string uses anything beyond the small
    `FIELD == "VALUE"` / `FIELD in [...]` (AND-combined) grammar the
    deterministic scorer can evaluate, or references a field the scorer
    cannot read. core/config.py turns this into a ConfigError at boot for
    every disqualifier not marked `enforcement: manual` (docs/decisions.md,
    M1.2 Correction 1) — an unenforceable disqualifier must never sit
    silently in the pack looking like protection that isn't there."""

    def __init__(self, rule: str, reason: str) -> None:
        self.rule = rule
        self.reason = reason
        super().__init__(f"Unparseable disqualifier rule {rule!r}: {reason}")


class ConfigError(RevenueEngineError):
    """Raised by core/config.py when config/base.yaml or the selected
    industry pack fails to load or validate. A hard boot failure, not a
    warning (CLAUDE.md §1 non-negotiable 9): a bad scoring-weight sum, a
    pack that fails schemas/entities/industry_pack.json, or objection-category
    drift between the pack and the two output schemas that reference it must
    stop the process, not degrade silently."""


class PromptRenderError(RevenueEngineError):
    """Raised by core/llm.py when a prompt template references a
    {{variable}} with no supplied value. Fails loudly before any API call —
    never renders an empty string into a prompt (docs/phase1-llm-boundary.md)."""

    def __init__(self, prompt_id: str, missing: list[str]) -> None:
        self.prompt_id = prompt_id
        self.missing = missing
        super().__init__(
            f"Prompt '{prompt_id}' references undefined variable(s): {', '.join(missing)}"
        )


class LLMValidationError(RevenueEngineError):
    """Raised by core/llm.py::complete_json() when a model response still
    fails schema or cross-field (V1-V9) validation after one retry. No
    partial output is ever written — the caller gets this exception and
    an `llm.validation_failed` event, nothing else."""

    def __init__(self, prompt_id: str, prompt_version: int, errors: list[str]) -> None:
        self.prompt_id = prompt_id
        self.prompt_version = prompt_version
        self.errors = errors
        super().__init__(
            f"'{prompt_id}' v{prompt_version} failed validation twice: {'; '.join(errors)}"
        )


class ApprovalNotFoundError(RevenueEngineError):
    """Raised by core/approvals.py::resolve() when `approval_id` matches no
    row at all — distinct from ApprovalAlreadyDecidedError (M1.3)."""

    def __init__(self, approval_id: object) -> None:
        self.approval_id = approval_id
        super().__init__(f"Approval not found: {approval_id}")


class ApprovalAlreadyDecidedError(RevenueEngineError):
    """Raised by core/approvals.py::resolve() when `approval_id` exists but
    its status already left 'pending' — a resolved approval is never
    re-decided (M1.3 plan deliverable 2). The database enforces this too
    (migrations/0005's `approvals_forbid_redecision` trigger); this is the
    typed surface for the ordinary, expected "already decided" case that
    core/approvals.py::resolve()'s own `WHERE status='pending'` guard
    produces (a clean zero-row UPDATE, not a trigger exception)."""

    def __init__(self, approval_id: object, current_status: str) -> None:
        self.approval_id = approval_id
        self.current_status = current_status
        super().__init__(
            f"Approval {approval_id} already decided (status={current_status}), "
            "cannot be re-decided"
        )
