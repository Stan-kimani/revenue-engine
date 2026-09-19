"""Unit tests for integrations/email_verification.py — pure mapping plus the
adapter's failure modes. No network: the API costs credits per address."""

from __future__ import annotations

import httpx
import pytest

from revenue_engine.db.models import EmailStatus
from revenue_engine.integrations.email_verification import (
    LEGACY_STATUS_MAP,
    EmailListVerifyProvider,
    default_provider,
    map_response,
)

# ---------------------------------------------------------------------------
# Flags are orthogonal to status — the trap a single-column lookup would fall into
# ---------------------------------------------------------------------------


def test_role_true_beats_status_valid():
    """The whole reason for the JSON endpoint: an address can be status
    'valid' AND role true. Tiering on status alone would send cold outreach
    to info@."""
    result = map_response({"status": "valid", "role": True, "disposable": False})
    assert result.status == EmailStatus.ROLE_BASED


def test_disposable_true_beats_status_valid():
    result = map_response({"status": "valid", "role": False, "disposable": True})
    assert result.status == EmailStatus.DISPOSABLE


def test_accept_all_true_beats_status_valid():
    result = map_response({"status": "valid", "accept_all": True})
    assert result.status == EmailStatus.CATCH_ALL


def test_role_is_checked_before_disposable_and_accept_all():
    result = map_response({"status": "valid", "role": True, "disposable": True, "accept_all": True})
    assert result.status == EmailStatus.ROLE_BASED


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("valid", EmailStatus.VALID),
        ("invalid", EmailStatus.INVALID),
        ("unknown", EmailStatus.RISKY),
    ],
)
def test_status_map_when_no_flag_is_set(status: str, expected: EmailStatus):
    result = map_response({"status": status, "role": False, "disposable": False})
    assert result.status == expected


@pytest.mark.protected
def test_an_unrecognised_status_word_fails_closed_to_unverified():
    """A vocabulary this adapter has never seen must never become sendable."""
    for word in ("wat", "", "ok", "accept_all", "ok_for_all", "role"):
        result = map_response({"status": word})
        assert result.status == EmailStatus.UNVERIFIED, word


def test_score_and_free_are_stored_but_never_tiered_on():
    """A founder at a 40-person agency on gmail.com is in the ICP, and score
    is a vendor confidence number — neither changes the tier."""
    result = map_response({"status": "valid", "score": 42, "free": True})
    assert result.status == EmailStatus.VALID
    assert result.attributes == {
        "email_verification_score": 42,
        "email_is_free_provider": True,
    }


def test_string_flags_are_not_read_as_false():
    assert map_response({"status": "valid", "role": "true"}).status == EmailStatus.ROLE_BASED


def test_legacy_map_has_both_catch_all_spellings_and_all_invalid_forms():
    assert LEGACY_STATUS_MAP["accept_all"] == EmailStatus.CATCH_ALL
    assert LEGACY_STATUS_MAP["ok_for_all"] == EmailStatus.CATCH_ALL
    for word in ("invalid", "invalid_mx", "email_disabled", "dead_server"):
        assert LEGACY_STATUS_MAP[word] == EmailStatus.INVALID
    assert LEGACY_STATUS_MAP["ok"] == EmailStatus.VALID


# ---------------------------------------------------------------------------
# The adapter fails closed
# ---------------------------------------------------------------------------


class _NeverCalled:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("the real API must never be called in tests")


async def test_malformed_address_is_invalid_without_spending_a_credit(
    monkeypatch: pytest.MonkeyPatch, unguarded_verifier: None
):
    never = _NeverCalled()
    monkeypatch.setattr(httpx.AsyncClient, "get", never)
    provider = EmailListVerifyProvider(api_key="test-key")

    result = await provider.verify("not-an-email")

    assert result.status == EmailStatus.INVALID
    assert never.calls == 0


@pytest.mark.protected
@pytest.mark.parametrize(
    "failure",
    ["http_error", "exception", "malformed"],
)
async def test_verification_failure_never_yields_valid(
    monkeypatch: pytest.MonkeyPatch, failure: str, unguarded_verifier: None
):
    """API down, out of credits, or answering nonsense: the contact stays
    unverified (never-send), never valid."""

    async def _get(self: object, url: str, **kwargs: object) -> httpx.Response:
        if failure == "exception":
            raise httpx.ConnectTimeout("no route to host")
        if failure == "http_error":
            return httpx.Response(402, json={"error": "out of credits"})
        return httpx.Response(200, json=["not", "a", "dict"])

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    provider = EmailListVerifyProvider(api_key="test-key")

    result = await provider.verify("pat@acme.example")

    assert result.status == EmailStatus.UNVERIFIED


async def test_successful_lookup_maps_through_map_response(
    monkeypatch: pytest.MonkeyPatch, unguarded_verifier: None
):
    async def _get(self: object, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json={"email": "pat@acme.example", "status": "valid", "accept_all": True}
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    provider = EmailListVerifyProvider(api_key="test-key")

    assert (await provider.verify("pat@acme.example")).status == EmailStatus.CATCH_ALL


def test_default_provider_is_none_without_an_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EMAILLISTVERIFY_API_KEY", raising=False)
    assert default_provider() is None
