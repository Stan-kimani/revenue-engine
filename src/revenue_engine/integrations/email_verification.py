"""Email verification (M1.4a). Interface plus one concrete adapter, the same
shape as integrations/prospecting.py: a Protocol for what we do not implement,
an adapter for what we do.

Verification happens ONCE, at import (scripts/import_leads.py), and the verdict
is stored permanently on the contact. Credits are consumed per address and a
mailbox's status does not change often enough to justify paying again, so a
contact that already carries a real verdict is never re-verified — only an
`unverified` contact (no verdict yet) is looked up. `contacts.email_status` is
write-once from here (repositories.upsert_contact preserves it; only
repositories.set_email_status changes it).

FAILING CLOSED: an API that is down, out of credits, rate-limited, slow, or
that answers with a word this adapter does not recognise leaves the contact
`unverified` — which is in the never-send tier. A verification failure can
never produce `valid`.

Provider vocabulary never escapes this module: callers see `EmailStatus`.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..db.models import EmailStatus

log = logging.getLogger("revenue_engine.integrations.email_verification")

_API_URL = "https://api.emaillistverify.com/api/verifyEmail"

# Obviously malformed input is refused without spending a credit.
_EMAIL_SYNTAX_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# The JSON endpoint's `status` field. Flags (role/disposable/accept_all) are
# evaluated BEFORE this map — see `map_response` — because they are orthogonal
# to status: an address can be status "valid" AND role true, and tiering on
# status alone would send cold outreach to info@.
_STATUS_MAP = {
    "valid": EmailStatus.VALID,
    "invalid": EmailStatus.INVALID,
    "unknown": EmailStatus.RISKY,
}

# Legacy string endpoint (apps.emaillistverify.com/api/verifyEmail), kept so a
# fallback to it cannot silently mis-tier: `ok_for_all` is a SECOND catch-all
# spelling alongside `accept_all`, and the three mailbox-level failures all
# mean invalid. Not used by the adapter below, which calls the JSON endpoint.
LEGACY_STATUS_MAP = {
    "ok": EmailStatus.VALID,
    "invalid": EmailStatus.INVALID,
    "invalid_mx": EmailStatus.INVALID,
    "email_disabled": EmailStatus.INVALID,
    "dead_server": EmailStatus.INVALID,
    "accept_all": EmailStatus.CATCH_ALL,
    "ok_for_all": EmailStatus.CATCH_ALL,
    "disposable": EmailStatus.DISPOSABLE,
    "role": EmailStatus.ROLE_BASED,
    "unknown": EmailStatus.RISKY,
}


@dataclass(frozen=True)
class VerificationResult:
    status: EmailStatus
    attributes: dict[str, Any] = field(default_factory=dict)
    """Vendor metadata stored as ordinary contact attributes — `score` and
    `free`. Neither is tiered on: score is a vendor-specific confidence
    number, and a founder at a 40-person agency legitimately using a
    gmail.com address is squarely in the ICP."""
    raw_status: str | None = None
    """What the provider actually said, for debugging a mis-map. Never used
    for a decision."""


class EmailVerificationProvider(Protocol):
    async def verify(self, email: str) -> VerificationResult: ...


def map_response(payload: dict[str, Any]) -> VerificationResult:
    """Flags first, then status. Pure: no I/O, exhaustively unit-tested.

    `role`/`disposable`/`accept_all` are booleans independent of `status`, so
    an address that is status "valid" and role true must land in the
    never-send tier, not the send tier.
    """
    raw_status = str(payload.get("status", "")).strip().lower()

    if _flag(payload, "role"):
        status = EmailStatus.ROLE_BASED
    elif _flag(payload, "disposable"):
        status = EmailStatus.DISPOSABLE
    elif _flag(payload, "accept_all"):
        status = EmailStatus.CATCH_ALL
    else:
        # An unrecognised status word fails closed to `unverified`: never-send,
        # and still eligible for a later verification once the map is fixed.
        status = _STATUS_MAP.get(raw_status, EmailStatus.UNVERIFIED)

    attributes: dict[str, Any] = {}
    if (score := payload.get("score")) is not None:
        attributes["email_verification_score"] = score
    if (free := payload.get("free")) is not None:
        attributes["email_is_free_provider"] = bool(free)
    return VerificationResult(status=status, attributes=attributes, raw_status=raw_status or None)


def _flag(payload: dict[str, Any], key: str) -> bool:
    """The provider sends real JSON booleans, but a string "true"/"1" from a
    future response shape must not read as False."""
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


class EmailListVerifyProvider:
    """The JSON endpoint, chosen over the legacy string one precisely because
    it exposes role/disposable/accept_all as separate booleans."""

    def __init__(self, *, api_key: str, timeout_seconds: float = 30) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def verify(self, email: str) -> VerificationResult:
        if not _EMAIL_SYNTAX_RE.match(email):
            # No credit spent on something that cannot be an address.
            return VerificationResult(status=EmailStatus.INVALID, raw_status="syntax")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _API_URL, params={"secret": self._api_key, "email": email}
                )
            if response.status_code != 200:
                log.warning(
                    "email verification returned %s; leaving contact unverified",
                    response.status_code,
                )
                return VerificationResult(status=EmailStatus.UNVERIFIED, raw_status="http_error")
            payload = response.json()
        except Exception:  # noqa: BLE001 - down, timing out, or out of credits: unverified
            log.exception("email verification failed; leaving contact unverified")
            return VerificationResult(status=EmailStatus.UNVERIFIED, raw_status="exception")
        if not isinstance(payload, dict):
            return VerificationResult(status=EmailStatus.UNVERIFIED, raw_status="malformed")
        return map_response(payload)


def default_provider(timeout_seconds: float = 30) -> EmailVerificationProvider | None:
    """None when EMAILLISTVERIFY_API_KEY is unset — the import then leaves
    every contact `unverified` (never-send) instead of failing, so an import
    still works before the key is supplied."""
    api_key = os.environ.get("EMAILLISTVERIFY_API_KEY")
    if not api_key:
        return None
    return EmailListVerifyProvider(api_key=api_key, timeout_seconds=timeout_seconds)
