"""Unit tests for core/observability.py. No network, no real Langfuse
account — every scenario is exercised with an injected stub client or by
manipulating environment variables, matching the graceful-degradation
contract: unconfigured or unreachable must log and continue, never raise.

Stub clients duck-type the installed langfuse v4 API
(client.start_observation(...) -> object with .end(), then client.flush()),
confirmed by introspecting the installed package during verification — not
the older client.generation()/.flush() shape."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

import pytest

from revenue_engine.core.observability import TraceContext, record_span

_TRACE = TraceContext(trace_id="trace-123", correlation_id=uuid.uuid4())


class _RaisingClient:
    """Simulates an unreachable Langfuse host — every call raises."""

    def start_observation(self, **kwargs: Any) -> Any:
        raise ConnectionError("could not connect to langfuse host")

    def flush(self) -> None:
        raise ConnectionError("could not connect to langfuse host")


class _StubObservation:
    def __init__(self) -> None:
        self.ended = False

    def end(self) -> None:
        self.ended = True


class _RecordingClient:
    def __init__(self) -> None:
        self.start_observation_calls: list[dict[str, Any]] = []
        self.flushed = False
        self.last_observation: _StubObservation | None = None

    def start_observation(self, **kwargs: Any) -> _StubObservation:
        self.start_observation_calls.append(kwargs)
        self.last_observation = _StubObservation()
        return self.last_observation

    def flush(self) -> None:
        self.flushed = True


def _call_record_span(client: Any) -> None:
    record_span(
        _TRACE,
        name="sales/classify_reply",
        prompt_id="sales/classify_reply",
        prompt_version=1,
        tier="fast",
        model="claude-haiku-4-5-20251001",
        cost=Decimal("0.0012"),
        latency_ms=850,
        status="success",
        retry_count=0,
        input_tokens=120,
        output_tokens=64,
        client=client,
    )


def test_unreachable_client_does_not_raise(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING):
        _call_record_span(_RaisingClient())  # must not raise
    assert any("Langfuse span recording failed" in r.message for r in caplog.records)


def test_successful_client_is_called_with_expected_fields():
    client = _RecordingClient()
    _call_record_span(client)

    assert client.flushed is True
    assert client.last_observation is not None
    assert client.last_observation.ended is True
    assert len(client.start_observation_calls) == 1
    call = client.start_observation_calls[0]
    assert call["trace_context"] == {"trace_id": _TRACE.trace_id}
    assert call["as_type"] == "generation"
    assert call["model"] == "claude-haiku-4-5-20251001"
    assert call["metadata"]["correlation_id"] == str(_TRACE.correlation_id)
    assert call["metadata"]["prompt_id"] == "sales/classify_reply"
    assert call["metadata"]["prompt_version"] == 1
    assert call["metadata"]["tier"] == "fast"
    assert call["metadata"]["retry_count"] == 0
    assert call["metadata"]["status"] == "success"
    assert call["metadata"]["latency_ms"] == 850
    assert call["usage_details"] == {"input": 120, "output": 64}
    assert call["cost_details"] == {"total": pytest.approx(0.0012)}
    assert call["level"] == "DEFAULT"


def test_failed_status_maps_to_error_level():
    client = _RecordingClient()
    record_span(
        _TRACE,
        name="x",
        prompt_id="x",
        prompt_version=1,
        tier="fast",
        model="m",
        cost=None,
        latency_ms=None,
        status="failed",
        retry_count=1,
        error="schema invalid",
        client=client,
    )
    assert client.start_observation_calls[0]["level"] == "ERROR"


def test_trace_none_is_a_noop_and_does_not_construct_a_client():
    calls: list[Any] = []

    class _ExplodingIfCalledClient:
        def start_observation(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise AssertionError("should never be called when trace is None")

        def flush(self) -> None:
            raise AssertionError("should never be called when trace is None")

    record_span(
        None,
        name="x",
        prompt_id="x",
        prompt_version=1,
        tier="fast",
        model="m",
        cost=None,
        latency_ms=None,
        status="success",
        retry_count=0,
        client=_ExplodingIfCalledClient(),
    )
    assert calls == []


def test_unconfigured_environment_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from revenue_engine.core import observability

    observability._default_client.cache_clear()

    with caplog.at_level(logging.DEBUG):
        record_span(
            _TRACE,
            name="x",
            prompt_id="x",
            prompt_version=1,
            tier="fast",
            model="m",
            cost=None,
            latency_ms=None,
            status="success",
            retry_count=0,
            client=None,  # no injected client — must not try to build a real one and must not raise
        )
    assert any("not configured" in r.message for r in caplog.records)
    observability._default_client.cache_clear()
