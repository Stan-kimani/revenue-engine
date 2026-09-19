"""Slack Socket Mode integration (M1.3 plan deliverable 3). Notification and
INPUT channel for approvals — never the source of truth. `approvals` (via
core/approvals.py) is the source of truth; this module only posts,
listens for button clicks, and calls `core/approvals.py::resolve()`.

No business logic beyond rendering and dispatch lives here (CLAUDE.md §2:
`integrations/` translates our types to API calls and back, nothing more).
`slack_sdk` and `aiohttp` are used for Socket Mode transport ONLY and must
never appear in `core/` or `db/` — the approval gate itself
(`core/approvals.py`) has zero Slack awareness, which is exactly what makes
it fail closed (this module's docstring, and core/approvals.py's own).

Credentials: `SLACK_APP_TOKEN` (Socket Mode, `xapp-...`), `SLACK_BOT_TOKEN`
(Web API, `xoxb-...`), `SLACK_APPROVAL_CHANNEL` (channel ID to post
approval requests to). All three read from the environment; none has a
default — a missing credential fails the Slack-facing call, never the
approval row itself.
"""

from __future__ import annotations

import json
import logging
import os
from functools import cache
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from ..core import approvals
from ..core.errors import ApprovalAlreadyDecidedError, ApprovalNotFoundError
from ..db import repositories as repo
from ..db.models import Approval, ApprovalStatus, Job

log = logging.getLogger("revenue_engine.integrations.slack")

_APPROVE_ACTION_ID = "approval_approve"
_REJECT_ACTION_ID = "approval_reject"

# Slack's own section-block text limit is ~3000 chars; this is a conservative
# cut below that, leaving room for the surrounding header/context text in the
# same message. A payload this large is a real M1.4-era design question
# (chunking? a truncated preview + a link to the full draft?) — not solved
# here; this module refuses to silently truncate what a human is deciding on
# (see `_render_payload_block`).
_MAX_RENDERED_PAYLOAD_CHARS = 2500


class SlackWebClientProtocol(Protocol):
    """Only the two calls this module needs — duck-typed against
    `slack_sdk.web.async_client.AsyncWebClient`, so tests inject a plain stub
    with no real Slack SDK object involved (same pattern as
    core/llm.py::AnthropicClientProtocol)."""

    async def chat_postMessage(
        self, *, channel: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> Any: ...

    async def chat_update(
        self, *, channel: str, ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> Any: ...


@cache
def _default_web_client() -> SlackWebClientProtocol:
    """Reads SLACK_BOT_TOKEN from the environment. Only ever called when no
    `client` is injected — every test injects a stub and never reaches this
    path (same convention as core/llm.py::_default_anthropic_client)."""
    from slack_sdk.web.async_client import AsyncWebClient

    return AsyncWebClient(token=os.environ.get("SLACK_BOT_TOKEN"))


def _approval_channel() -> str:
    channel = os.environ.get("SLACK_APPROVAL_CHANNEL")
    if not channel:
        raise RuntimeError("SLACK_APPROVAL_CHANNEL is not set")
    return channel


# ============================================================================
# Rendering — Block Kit. The human must see exactly what would execute, not a
# summary (M1.3 plan deliverable 3).
# ============================================================================


def _render_payload_block(payload: dict[str, Any]) -> str:
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if len(rendered) > _MAX_RENDERED_PAYLOAD_CHARS:
        # Deliberately not a silent truncation of the content a human is
        # about to approve — surfaces the limitation instead of hiding it.
        return (
            f"```{rendered[:_MAX_RENDERED_PAYLOAD_CHARS]}```\n"
            f"⚠️ *Content truncated* ({len(rendered)} chars total) — this exceeds what Slack "
            "can render safely in one message. Do not approve without reviewing the full "
            "content elsewhere."
        )
    return f"```{rendered}```"


def build_approval_request_blocks(approval: Approval) -> tuple[str, list[dict[str, Any]]]:
    """Returns (fallback text, Block Kit blocks) for a fresh approval
    request. `text` is Slack's required plain-text fallback (notifications,
    screen readers); `blocks` is what's actually rendered, showing the FULL
    `approval.payload` — the exact content that would execute if approved."""
    fallback_text = f"Approval needed: {approval.action_type.value} ({approval.id})"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Approval needed: {approval.action_type.value}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"`{approval.id}` · requested by "
                        f"`{approval.requested_by_agent or 'unknown'}`"
                        + (
                            f" · expires {approval.expires_at.isoformat()}"
                            if approval.expires_at
                            else ""
                        )
                    ),
                }
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _render_payload_block(approval.payload)},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": _APPROVE_ACTION_ID,
                    "value": str(approval.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": _REJECT_ACTION_ID,
                    "value": str(approval.id),
                },
            ],
        },
    ]
    return fallback_text, blocks


def build_decided_blocks(approval: Approval) -> tuple[str, list[dict[str, Any]]]:
    """Replaces the Approve/Reject buttons with a plain statement of who
    decided and when — deliverable 3: 'updates the message to show who
    decided and when.'"""
    verb = "Approved" if approval.status == ApprovalStatus.GRANTED else "Rejected"
    decided_at = approval.decided_at.isoformat() if approval.decided_at else "unknown time"
    fallback_text = f"{verb} by {approval.decided_by} at {decided_at}"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{verb}: {approval.action_type.value}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _render_payload_block(approval.payload)},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*{verb}* by `{approval.decided_by}` at {decided_at}"}
            ],
        },
    ]
    if approval.decision_reason:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Reason:* {approval.decision_reason}"},
            }
        )
    return fallback_text, blocks


# ============================================================================
# Job handler — wired via router.py's approval.requested -> ROUTES entry and
# scripts/run_worker.py's HANDLERS. Posting is best-effort: if this raises
# (Slack down, misconfigured, whatever), the job retries/dead-letters per the
# normal queue mechanics (core/queue.py) — the approval row itself, already
# committed by core/approvals.py::request_approval() before this job even
# existed, is completely unaffected either way.
# ============================================================================


async def handle_notify_approval_request(
    conn: asyncpg.Connection, job: Job, *, client: SlackWebClientProtocol | None = None
) -> None:
    resolved_client = client if client is not None else _default_web_client()
    approval_id = UUID(job.payload["approval_id"])

    approval = await repo.get_approval(conn, approval_id)
    if approval is None:
        # Should be unreachable (request_approval() just inserted this row
        # before emitting the event this job was routed from) — but a
        # missing row is a reason to skip posting, not to crash the worker.
        log.warning(
            "approval.requested job for missing approval", extra={"approval_id": str(approval_id)}
        )
        return
    if approval.status != ApprovalStatus.PENDING:
        # Already decided or expired before this job ran (e.g. a retried
        # dispatch after a crash, or a very short-TTL action) — posting an
        # Approve/Reject prompt for something no longer actionable would be
        # actively misleading.
        log.info(
            "skipping Slack post for non-pending approval",
            extra={"approval_id": str(approval_id), "status": approval.status.value},
        )
        return

    text, blocks = build_approval_request_blocks(approval)
    await resolved_client.chat_postMessage(channel=_approval_channel(), text=text, blocks=blocks)


# ============================================================================
# Interaction callback — Socket Mode delivers this as a `block_actions`
# payload. Kept separate from the raw Socket Mode transport code below so it
# is fully testable with a plain dict and a stub client (deliverable: "Stub
# the Slack client in tests. Do not hit the real API.").
# ============================================================================


async def handle_interaction_payload(
    conn: asyncpg.Connection,
    raw_payload: dict[str, Any],
    *,
    client: SlackWebClientProtocol | None = None,
) -> None:
    if raw_payload.get("type") != "block_actions":
        return
    actions = raw_payload.get("actions") or []
    if not actions:
        return
    action = actions[0]
    action_id = action.get("action_id")
    approval_id_raw = action.get("value")
    if action_id not in (_APPROVE_ACTION_ID, _REJECT_ACTION_ID) or not approval_id_raw:
        return

    approval_id = UUID(approval_id_raw)
    decision = ApprovalStatus.GRANTED if action_id == _APPROVE_ACTION_ID else ApprovalStatus.DENIED
    user = raw_payload.get("user") or {}
    decided_by = f"human:{user.get('username') or user.get('id') or 'unknown'}"

    try:
        resolved = await approvals.resolve(
            conn, approval_id, decision=decision, decided_by=decided_by
        )
    except (ApprovalNotFoundError, ApprovalAlreadyDecidedError) as exc:
        # An expected race (double-click, or someone already decided via a
        # different path) — surfaced to the channel, not raised into the
        # Socket Mode listener loop.
        channel = (raw_payload.get("channel") or {}).get("id")
        ts = (raw_payload.get("message") or {}).get("ts")
        if channel and ts:
            resolved_client = client if client is not None else _default_web_client()
            await resolved_client.chat_update(channel=channel, ts=ts, text=f"⚠️ {exc}")
        return

    channel = (raw_payload.get("channel") or {}).get("id")
    ts = (raw_payload.get("message") or {}).get("ts")
    if channel and ts:
        resolved_client = client if client is not None else _default_web_client()
        text, blocks = build_decided_blocks(resolved)
        await resolved_client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)


# ============================================================================
# Socket Mode transport — thin wiring, not directly unit tested (same as
# scripts/run_worker.py's own main()/signal handling). Registered as a 4th
# long-running loop from scripts/run_worker.py's main(), matching its other
# three loops' shape.
# ============================================================================


async def run_socket_mode_listener(pool: asyncpg.Pool, shutdown: Any) -> None:
    """Connects via Socket Mode and dispatches every `block_actions`
    interaction to `handle_interaction_payload`, each on its own pooled
    connection. Runs until `shutdown` (an `asyncio.Event`) is set.

    A Slack/network failure here can only affect THIS loop — it cannot
    reach into `approvals` (this loop never writes to `approvals` directly;
    `handle_interaction_payload` does, through the exact same
    `core/approvals.py::resolve()` any other caller would use)."""
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient

    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        log.warning("SLACK_APP_TOKEN not set — Socket Mode listener not starting")
        return

    web_client = AsyncWebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
    client = SocketModeClient(app_token=app_token, web_client=web_client)

    async def _on_request(client: AsyncBaseSocketModeClient, req: SocketModeRequest) -> None:
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "interactive":
            return
        raw_payload = req.payload
        if isinstance(raw_payload, str):
            raw_payload = json.loads(raw_payload)
        async with pool.acquire() as conn:
            try:
                await handle_interaction_payload(conn, raw_payload)
            except Exception:  # noqa: BLE001 - one bad interaction must not kill the listener
                log.exception("failed to handle Slack interaction payload")

    client.socket_mode_request_listeners.append(_on_request)
    await client.connect()
    try:
        await shutdown.wait()
    finally:
        await client.disconnect()


# ============================================================================
# Sending health alerts (M1.4a) — sending.paused / sending.resumed /
# sending.health_warning. Notification only: the pause itself is the
# sending_pauses row core/sending.py already committed; a Slack failure here
# cannot resume or un-pause anything.
# ============================================================================


def render_sending_alert(event_type: str, payload: dict[str, Any]) -> str:
    domain = payload.get("sending_domain", "?")
    if event_type == "sending.paused":
        return (
            f":octagonal_sign: *Sending PAUSED on {domain}.* Nothing will send until a human "
            f"resumes with a reason (scripts/resume_sending.py).\n*Why:* {payload.get('reason')}\n"
            f"*Metrics:* ```{json.dumps(payload.get('metrics', {}), indent=2, sort_keys=True)}```"
        )
    if event_type == "sending.resumed":
        return (
            f":arrow_forward: *Sending resumed on {domain}* by `{payload.get('resumed_by')}`.\n"
            f"*Reason:* {payload.get('reason')}\n"
            f"Re-enqueued held drafts: {payload.get('requeued_count')}"
        )
    if event_type == "sending.health_warning":
        return (
            f":warning: *Deliverability warning on {domain}:* {payload.get('metric')} rate "
            f"{float(payload.get('value', 0)):.4f} >= warn {payload.get('warn_threshold')} "
            f"({payload.get('sends_in_window')} sends in {payload.get('window_days')} days). "
            "Not paused yet."
        )
    raise ValueError(f"not a sending alert event type: {event_type}")


async def handle_notify_sending_alert(
    conn: asyncpg.Connection, job: Job, *, client: SlackWebClientProtocol | None = None
) -> None:
    resolved_client = client if client is not None else _default_web_client()
    event = await repo.get_event(conn, UUID(job.payload["source_event_id"]))
    if event is None:
        raise RuntimeError(f"source event {job.payload['source_event_id']} not found")
    text = render_sending_alert(event.type, event.payload)
    await resolved_client.chat_postMessage(channel=_approval_channel(), text=text)
