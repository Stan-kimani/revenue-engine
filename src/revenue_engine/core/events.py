"""The only way to write to the events outbox (event-catalog.md §R1/§R3).

Events are facts: past tense, immutable, never retried, consumed by zero or
many. There is no `send.email` event — there is a `send_email` job, and
afterwards an `outreach.sent` event. Jobs live in core/queue.py.

`emit()` validates the payload against `schemas/events/<type>.json` BEFORE
persisting — a typo in an event type must fail loudly (UnknownEventTypeError),
never silently create a new type. All SQL stays in db/repositories.py (CLAUDE.md
§4); this module is validation + orchestration on top of
repositories.emit_event(), which already handles idempotency-key no-op
re-emission.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import jsonschema

from ..db import repositories as repo
from ..db.models import Event
from .errors import EventPayloadValidationError, UnknownEventTypeError

_EVENT_SCHEMAS_DIR = Path(__file__).resolve().parents[3] / "schemas" / "events"


@cache
def _load_validator(event_type: str) -> jsonschema.Draft202012Validator:
    """Cached: schema files don't change at runtime, and every emit() call
    would otherwise re-read and re-compile the same schema from disk."""
    schema_path = _EVENT_SCHEMAS_DIR / f"{event_type}.json"
    if not schema_path.is_file():
        raise UnknownEventTypeError(event_type)
    return jsonschema.Draft202012Validator(json.loads(schema_path.read_text()))


async def emit(
    conn: asyncpg.Connection,
    *,
    type: str,
    payload: dict[str, Any],
    correlation_id: UUID,
    actor: str,
    idempotency_key: str,
    causation_id: UUID | None = None,
    version: int = 1,
) -> Event:
    """Validate and persist one event.

    - Unknown `type` (no schemas/events/<type>.json) raises
      UnknownEventTypeError before any database access.
    - A payload that fails schema validation raises
      EventPayloadValidationError and is never inserted.
    - `correlation_id` is the caller's responsibility to propagate unchanged
      across a lead's lifetime; `causation_id` should be the event_id of
      whatever event triggered this one (event-catalog.md §R3). Neither is
      defaulted or inferred here — a handler that gets this wrong breaks
      traceability silently, so it must be explicit at every call site.
    - Re-emitting the same `idempotency_key` returns the existing row
      unchanged (repositories.emit_event) — a no-op, not an error.
    """
    validator = _load_validator(type)
    errors = [e.message for e in validator.iter_errors(payload)]
    if errors:
        raise EventPayloadValidationError(type, errors)

    return await repo.emit_event(
        conn,
        type=type,
        payload=payload,
        correlation_id=correlation_id,
        actor=actor,
        idempotency_key=idempotency_key,
        causation_id=causation_id,
        version=version,
    )
