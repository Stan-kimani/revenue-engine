"""Langfuse tracing wrapper (build-spec §7: Langfuse — traces, cost per run,
prompt versions). Every core/llm.py::complete_json() call records one span
here, carrying correlation_id, prompt id, prompt_version, tier, cost and
latency (docs/decisions.md: DECISION 1 — the official `langfuse` SDK, not a
hand-rolled ingestion client, kept isolated to this file behind
`record_span()` so replacing it later touches one module).

Targets the installed `langfuse` v4 (OTel-based) client API, confirmed by
introspecting the installed package during verification rather than assumed
from memory: `Langfuse(public_key=..., secret_key=..., host=...)` still
constructs the client, but span/generation creation is
`client.start_observation(as_type="generation", ...)` -> an object with
`.end()`, not the v2-era `.generation()` method. See docs/decisions.md.

Graceful degradation is the entire reason this is a separate module rather
than inline langfuse calls in core/llm.py: unconfigured (no
LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY) or unreachable must log a warning
and return — never raise, never block a job on an observability outage.
Every langfuse SDK call in this file is wrapped accordingly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraceContext:
    """One per logical run a caller wants correlated in Langfuse. M0.4 has
    no agents yet — golden/unit tests and, eventually, agents construct this
    directly. `trace_id` is a Langfuse trace id (an opaque string Langfuse
    groups spans by); `correlation_id` is this system's own cross-event
    correlation id (core/events.py), recorded as span metadata so a Langfuse
    trace can be cross-referenced back to the events/jobs it came from."""

    trace_id: str
    correlation_id: UUID


class _ObservationProtocol(Protocol):
    def end(self) -> Any: ...


class LangfuseClientProtocol(Protocol):
    """The subset of langfuse.Langfuse's surface record_span() uses. Lets
    tests inject a stub client — including one that raises, to exercise the
    "unreachable" degrade path — without a real SDK client or network."""

    def start_observation(self, **kwargs: Any) -> _ObservationProtocol: ...

    def flush(self) -> None: ...


def _configured() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY")) and bool(
        os.environ.get("LANGFUSE_SECRET_KEY")
    )


@cache
def _default_client() -> LangfuseClientProtocol | None:
    """Constructed at most once per process, only if both keys are present.
    Returns None rather than raising if construction itself fails (a
    malformed LANGFUSE_HOST, for instance) — that degrades exactly like an
    unreachable host does, not differently."""
    if not _configured():
        return None
    try:
        from langfuse import Langfuse

        # The real client's start_observation() is a precisely-typed overload
        # set (one per as_type literal); LangfuseClientProtocol declares a
        # loose **kwargs signature on purpose, since it exists for test
        # injection, not to mirror the SDK's full overload surface. The two
        # are functionally compatible (record_span always calls with a
        # matching set of kwargs) but not structurally identical to mypy.
        return Langfuse(  # type: ignore[return-value]
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
    except Exception:
        logger.warning(
            "Langfuse client construction failed; tracing disabled for this process",
            exc_info=True,
        )
        return None


def record_span(
    trace: TraceContext | None,
    *,
    name: str,
    prompt_id: str,
    prompt_version: int,
    tier: str,
    model: str,
    cost: Decimal | None,
    latency_ms: int | None,
    status: str,
    retry_count: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error: str | None = None,
    client: LangfuseClientProtocol | None = None,
) -> None:
    """Record one span for a single complete_json() call — one span per
    call, covering both attempts if a retry happened (retry_count carries
    that), not one span per attempt.

    Never raises. `trace=None` is a no-op (not every caller has a
    TraceContext yet), unconfigured is a no-op, and any exception from the
    langfuse client itself (construction, `.start_observation()`, `.end()`,
    `.flush()`) is caught and logged at WARNING — observability failure must
    never fail the job that triggered it (CLAUDE.md workflow: fail loudly on
    real defects, but this is explicitly not one).
    """
    if trace is None:
        return

    resolved_client = client if client is not None else _default_client()
    if resolved_client is None:
        logger.debug("Langfuse not configured; skipping span for %s", prompt_id)
        return

    try:
        observation = resolved_client.start_observation(
            trace_context={"trace_id": trace.trace_id},
            name=name,
            as_type="generation",
            model=model,
            metadata={
                "correlation_id": str(trace.correlation_id),
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
                "tier": tier,
                "retry_count": retry_count,
                "status": status,
                "latency_ms": latency_ms,
                "error": error,
            },
            usage_details={
                k: v
                for k, v in {"input": input_tokens, "output": output_tokens}.items()
                if v is not None
            },
            cost_details={"total": float(cost)} if cost is not None else None,
            level="ERROR" if status == "failed" else "DEFAULT",
        )
        observation.end()
        resolved_client.flush()
    except Exception:
        logger.warning(
            "Langfuse span recording failed for %s; continuing without it",
            prompt_id,
            exc_info=True,
        )
