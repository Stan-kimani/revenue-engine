"""Gmail API send (M1.4a). Send only — no polling, no history.list (M1.4b).

Transport only: every business rule lives in core/sending.py. `send()` accepts
nothing but a `SendAuthorization`, which only core.sending.authorize_send() can
create after every gate passed and the message was reserved. Two safety rails
live HERE, in the send function itself, deliberately:

  - CLAUDE.md §6: when ENV != "production", ALL outbound mail is delivered to
    DEV_SANDBOX_EMAIL instead of the real recipient. An unset ENV counts as
    not production.
  - deliverability.md §1: the authenticated Gmail account must be the
    authorization's from address. Gmail would otherwise send as whichever
    account the OAuth token belongs to.

Plain text only: no HTML part, no tracking pixel, no template (§7).

Uses httpx directly (an existing dependency) rather than google-api-python-client,
whose synchronous client would block the event loop (CLAUDE.md §4). OAuth:
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN (gmail.send and
gmail.readonly-profile scopes). Tests inject a stub transport and never reach
the network.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol

import httpx

from ..core.errors import (
    GmailSenderMismatchError,
    GmailSendRejectedError,
    SendNotAuthorizedError,
)
from ..core.sending import SendAuthorization

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass(frozen=True)
class GmailSendResult:
    provider_message_id: str
    thread_id: str | None
    delivered_to: str
    dev_sandbox_redirect: bool


class GmailTransportProtocol(Protocol):
    async def authenticated_address(self) -> str: ...

    async def send_raw(self, raw: str) -> tuple[str, str | None]:
        """Returns (provider_message_id, thread_id). Raises
        GmailSendRejectedError for a definitive rejection (nothing
        delivered); any other exception means the outcome is unknown."""
        ...


class HttpxGmailTransport:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        timeout_seconds: float,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._timeout = timeout_seconds
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._address: str | None = None

    async def _token(self, client: httpx.AsyncClient) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        response = await client.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code != 200:
            # Nothing has been sent yet — a definitive failure, not an unknown.
            raise GmailSendRejectedError(response.status_code, "OAuth token refresh failed")
        body = response.json()
        self._access_token = str(body["access_token"])
        self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
        return self._access_token

    async def authenticated_address(self) -> str:
        if self._address is not None:
            return self._address
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            token = await self._token(client)
            response = await client.get(
                f"{_GMAIL_API}/profile", headers={"Authorization": f"Bearer {token}"}
            )
        if response.status_code != 200:
            raise GmailSendRejectedError(response.status_code, "Gmail profile lookup failed")
        self._address = str(response.json()["emailAddress"]).lower()
        return self._address

    async def send_raw(self, raw: str) -> tuple[str, str | None]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            token = await self._token(client)
            response = await client.post(
                f"{_GMAIL_API}/messages/send",
                headers={"Authorization": f"Bearer {token}"},
                json={"raw": raw},
            )
        if 400 <= response.status_code < 500:
            raise GmailSendRejectedError(response.status_code, response.text[:500])
        # 5xx: Gmail may or may not have accepted it — surfaces as unknown.
        response.raise_for_status()
        body = response.json()
        return str(body["id"]), body.get("threadId")


def _default_transport(timeout_seconds: float) -> GmailTransportProtocol:
    return HttpxGmailTransport(
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        timeout_seconds=timeout_seconds,
    )


def build_raw_message(*, from_address: str, to_address: str, subject: str, body: str) -> str:
    """RFC 2822, text/plain UTF-8, base64url-encoded as Gmail's `raw` expects."""
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body, subtype="plain", charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


async def send(
    authorization: SendAuthorization,
    *,
    transport: GmailTransportProtocol | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    timeout_seconds: float = 30,
) -> GmailSendResult:
    if not isinstance(authorization, SendAuthorization):
        raise SendNotAuthorizedError("gmail.send requires a core.sending.SendAuthorization")
    authorization.consume(now)

    env: Mapping[str, str] = environ if environ is not None else os.environ
    recipient = authorization.to_address
    redirected = False
    if env.get("ENV") != "production":
        sandbox = env.get("DEV_SANDBOX_EMAIL")
        if not sandbox:
            raise SendNotAuthorizedError(
                "ENV is not 'production' and DEV_SANDBOX_EMAIL is unset — refusing to send"
            )
        recipient = sandbox
        redirected = True

    resolved = transport if transport is not None else _default_transport(timeout_seconds)
    authenticated = (await resolved.authenticated_address()).lower()
    if authenticated != authorization.from_address.lower():
        raise GmailSenderMismatchError(
            f"authenticated Gmail account {authenticated!r} is not the configured from "
            f"address {authorization.from_address!r}"
        )

    raw = build_raw_message(
        from_address=authorization.from_address,
        to_address=recipient,
        subject=authorization.subject,
        body=authorization.body,
    )
    provider_message_id, thread_id = await resolved.send_raw(raw)
    return GmailSendResult(
        provider_message_id=provider_message_id,
        thread_id=thread_id,
        delivered_to=recipient,
        dev_sandbox_redirect=redirected,
    )
