"""Unit tests for core/sending.py's pure rules and integrations/gmail.py's
message building — no database, no network (M1.4a)."""

from __future__ import annotations

import base64
import email
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from revenue_engine.core.config import load_config
from revenue_engine.core.errors import SendNotAuthorizedError
from revenue_engine.core.sending import (
    SendAuthorization,
    check_content,
    compose_outbound_body,
    evaluate_health,
    is_role_based_address,
    next_window_open,
    resolve_recipient_timezone,
)
from revenue_engine.db.models import EmailStatus, Message, SendState
from revenue_engine.integrations.gmail import build_raw_message

_CFG = replace(
    load_config().deliverability, physical_address="123 Main St, Springfield, IL 62701, USA"
)


# ---------------------------------------------------------------------------
# §7 content
# ---------------------------------------------------------------------------


def test_composed_body_passes_the_content_gate():
    body = compose_outbound_body("Saw you are hiring an ops coordinator. Worth a chat?", _CFG)
    assert (
        check_content(subject="ops coordinator hire", body=body, cfg=_CFG, first_touch=True) is None
    )


def test_missing_physical_address_config_fails_content():
    cfg = replace(_CFG, physical_address="")
    body = compose_outbound_body("Short honest note about intake.", cfg)
    assert "physical_address" in (
        check_content(subject="intake", body=body, cfg=cfg, first_touch=True) or ""
    )


def test_missing_opt_out_fails_content():
    body = f"Short note.\n\n{_CFG.physical_address}"
    assert "opt-out" in (check_content(subject="hi", body=body, cfg=_CFG, first_touch=True) or "")


def test_html_is_refused():
    body = compose_outbound_body("<p>Hello</p>", _CFG)
    assert "HTML" in (check_content(subject="hi", body=body, cfg=_CFG, first_touch=True) or "")


def test_more_links_than_max_links_is_refused():
    body = compose_outbound_body("See https://a.example and https://b.example", _CFG)
    assert "links" in (check_content(subject="hi", body=body, cfg=_CFG, first_touch=True) or "")


def test_re_prefix_on_first_touch_is_refused_but_allowed_on_follow_up():
    body = compose_outbound_body("Short note.", _CFG)
    assert check_content(subject="Re: intake", body=body, cfg=_CFG, first_touch=True)
    assert check_content(subject="Re: intake", body=body, cfg=_CFG, first_touch=False) is None


# ---------------------------------------------------------------------------
# Timezone and window
# ---------------------------------------------------------------------------


def _envelope(value: str) -> dict[str, object]:
    return {"timezone": {"value": value, "confidence": 0.9, "evidence": "x", "source": "llm:x"}}


def test_contact_timezone_takes_precedence_over_company_country():
    zone, source = resolve_recipient_timezone(
        contact_attributes=_envelope("Europe/London"), company_country="US", cfg=_CFG
    )
    assert str(zone) == "Europe/London" and source == "contact_timezone"


def test_company_country_used_when_contact_timezone_absent():
    zone, source = resolve_recipient_timezone(contact_attributes={}, company_country="uk", cfg=_CFG)
    assert str(zone) == "Europe/London" and source == "company_country:UK"


def test_unknown_placement_falls_back_to_default_recipient_timezone_not_refusal():
    zone, source = resolve_recipient_timezone(
        contact_attributes=_envelope("Not/AZone"), company_country=None, cfg=_CFG
    )
    assert str(zone) == "America/New_York" and source == "default_recipient_timezone"


def test_inside_window_returns_none():
    ny = ZoneInfo("America/New_York")
    wednesday_10am = datetime(2026, 9, 16, 10, 0, tzinfo=ny)
    assert next_window_open(wednesday_10am.astimezone(UTC), ny, _CFG) is None


def test_window_is_evaluated_in_recipient_time_not_utc():
    """14:00 UTC is 10:00 in New York (inside) but 17:00 in Nairobi (outside)."""
    now = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
    assert next_window_open(now, ZoneInfo("America/New_York"), _CFG) is None
    assert next_window_open(now, ZoneInfo("Africa/Nairobi"), _CFG) is not None


def test_end_of_window_is_exclusive_and_next_open_is_next_business_morning():
    ny = ZoneInfo("America/New_York")
    wednesday_5pm = datetime(2026, 9, 16, 17, 0, tzinfo=ny)
    opens = next_window_open(wednesday_5pm.astimezone(UTC), ny, _CFG)
    assert opens == datetime(2026, 9, 17, 8, 0, tzinfo=ny).astimezone(UTC)


def test_friday_evening_next_open_is_monday_morning():
    ny = ZoneInfo("America/New_York")
    friday_6pm = datetime(2026, 9, 18, 18, 0, tzinfo=ny)
    opens = next_window_open(friday_6pm.astimezone(UTC), ny, _CFG)
    assert opens == datetime(2026, 9, 21, 8, 0, tzinfo=ny).astimezone(UTC)


# ---------------------------------------------------------------------------
# §6 health with the sample floor
# ---------------------------------------------------------------------------


def _health(**kw: float):
    base: dict[str, float] = {
        "sends": 0,
        "hard_bounces": 0,
        "spam_complaints": 0,
        "unsubscribes": 0,
    }
    base.update(kw)
    return evaluate_health(health=_CFG.health, **base)


def test_zero_sends_is_below_floor_and_does_not_divide_by_zero():
    result = _health()
    assert result.pause_reasons == () and result.metrics["mode"] == "below_sample_floor"


def test_below_floor_one_hard_bounce_does_not_pause_but_two_do():
    assert _health(sends=35, hard_bounces=1).pause_reasons == ()
    assert _health(sends=35, hard_bounces=2).pause_reasons


def test_below_floor_one_spam_complaint_pauses():
    assert _health(sends=10, spam_complaints=1).pause_reasons


def test_below_floor_rates_do_not_evaluate():
    """3 unsubscribes in 10 sends is 30% — below the floor, unsubscribes
    have no absolute count, so this does not pause."""
    assert _health(sends=10, unsubscribes=3).pause_reasons == ()


def test_at_floor_rates_apply_as_written():
    at_floor = _CFG.health.sample_floor_sends
    assert _health(sends=at_floor, hard_bounces=1).pause_reasons == ()  # 2% = warn
    assert _health(sends=at_floor, hard_bounces=1).warnings[0][0] == "bounce"
    assert _health(sends=at_floor, hard_bounces=2).pause_reasons  # 4% >= 3%


def test_health_thresholds_come_from_config():
    strict = replace(_CFG.health, below_floor_pause_counts=MappingProxyType({"hard_bounce": 1}))
    result = evaluate_health(
        sends=5, hard_bounces=1, spam_complaints=0, unsubscribes=0, health=strict
    )
    assert result.pause_reasons


# ---------------------------------------------------------------------------
# SendAuthorization and the Gmail message
# ---------------------------------------------------------------------------


def _message() -> Message:
    now = datetime.now(UTC)
    return Message(
        id=uuid4(),
        lead_id=uuid4(),
        contact_id=uuid4(),
        campaign_id=None,
        direction="outbound",
        channel="email",
        provider_message_id=None,
        thread_id=None,
        subject="s",
        body_text="b",
        sequence_step=0,
        prompt_version=1,
        approval_id=uuid4(),
        from_address="kimani@getkimani.com",
        to_address="pat@acme.example",
        send_state=SendState.SENDING,
        send_started_at=now,
        send_block_reason=None,
        recipient_email_status=None,
        sent_at=None,
        created_at=now,
        updated_at=now,
    )


def test_authorization_cannot_be_minted_outside_core_sending():
    with pytest.raises(SendNotAuthorizedError):
        SendAuthorization(
            message=_message(), authorized_at=datetime.now(UTC), ttl_seconds=60, _mint=object()
        )


def test_build_raw_message_is_plain_text_only():
    raw = build_raw_message(
        from_address="kimani@getkimani.com",
        to_address="pat@acme.example",
        subject="intake",
        body="Plain body.",
    )
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert parsed.get_content_type() == "text/plain"
    assert not parsed.is_multipart()
    assert parsed["To"] == "pat@acme.example"
    assert "<img" not in parsed.get_payload()


# ---------------------------------------------------------------------------
# M1.4a tiers: catch-all sub-cap arithmetic, role detection, weighted bounces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("daily_cap", "share", "expected"),
    [
        (5, 0.4, 2),  # today's warmup week
        (10, 0.4, 4),
        (40, 0.4, 16),  # the share tracks the ramp; an absolute 2 would not
        (5, 0.1, 1),  # floors to 0, raised to the minimum of 1
        (1, 0.4, 1),
        (0, 0.4, 0),  # a zero cap forbids everything, sub-cap included
    ],
)
def test_catch_all_sub_cap_is_a_share_of_daily_cap(daily_cap: int, share: float, expected: int):
    cfg = replace(_CFG, daily_cap=daily_cap, catch_all_share=share)
    assert cfg.catch_all_daily_cap == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("info@acme.example", True),
        ("SALES@Acme.Example", True),
        ("support@acme.example", True),
        ("pat@acme.example", False),
        ("info.patel@acme.example", False),  # a person, not the info@ mailbox
    ],
)
def test_role_based_local_parts_are_detected_without_a_verifier(address: str, expected: bool):
    assert is_role_based_address(address, _CFG) is expected


def test_every_email_status_is_classified_in_exactly_one_tier():
    tiers = _CFG.email_status_tiers
    for status in EmailStatus:
        assert tiers.tier_of(status) in {"send", "restricted", "never"}
    assert not (tiers.send & tiers.restricted)
    assert not (tiers.send & tiers.never)
    assert not (tiers.restricted & tiers.never)


def test_unverified_and_risky_are_never_send():
    tiers = _CFG.email_status_tiers
    for status in (
        EmailStatus.UNVERIFIED,
        EmailStatus.RISKY,
        EmailStatus.ROLE_BASED,
        EmailStatus.DISPOSABLE,
        EmailStatus.INVALID,
        EmailStatus.BOUNCED,
        EmailStatus.SUPPRESSED,
    ):
        assert tiers.tier_of(status) == "never", status


def test_catch_all_is_restricted_not_send():
    assert _CFG.email_status_tiers.tier_of(EmailStatus.CATCH_ALL) == "restricted"


def test_a_catch_all_bounce_counts_double_toward_the_pause_threshold():
    """Below the sample floor 2 hard bounces pause. One catch-all bounce is
    weighted 2, so it pauses on its own — accepted deliberately: a bounce in
    the first fifty sends on a new domain is genuinely alarming."""
    unweighted = _health(sends=10, hard_bounces=1)
    weighted = _health(sends=10, hard_bounces=2.0)  # one catch-all bounce, weight 2

    assert unweighted.pause_reasons == ()
    assert weighted.pause_reasons
