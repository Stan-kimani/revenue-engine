"""Unit tests for core/approvals.py's pure functions — no I/O, no database
(M1.3 plan: "Autonomy levels... decide WHETHER approval is required. That
decision is config-driven from the pack, not hardcoded per call site").
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from revenue_engine.core.approvals import compute_expires_at, requires_approval
from revenue_engine.core.config import ApprovalExpiryPolicy, ThresholdsConfig
from revenue_engine.db.models import ActionType, AutonomyLevel


def _thresholds(
    *,
    autonomy_requires_approval: frozenset[AutonomyLevel] = frozenset(
        {AutonomyLevel.A2, AutonomyLevel.A3}
    ),
    expiry: dict[ActionType, ApprovalExpiryPolicy] | None = None,
) -> ThresholdsConfig:
    default_expiry = {
        a: ApprovalExpiryPolicy(on_expiry="escalate", ttl_hours=24.0) for a in ActionType
    }
    return ThresholdsConfig(
        autonomy_requires_approval=autonomy_requires_approval,
        expiry=MappingProxyType(expiry if expiry is not None else default_expiry),
    )


# ---------------------------------------------------------------------------
# requires_approval — config-driven, not a hardcoded level comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (AutonomyLevel.A0, False),
        (AutonomyLevel.A1, False),
        (AutonomyLevel.A2, True),
        (AutonomyLevel.A3, True),
    ],
)
def test_requires_approval_matches_default_config(level: AutonomyLevel, expected: bool):
    assert requires_approval(level, _thresholds()) is expected


def test_requires_approval_is_driven_entirely_by_config_not_a_fixed_rule():
    """The same AutonomyLevel must be able to go either way purely by
    changing config — proves this isn't secretly hardcoded anywhere."""
    permissive = _thresholds(autonomy_requires_approval=frozenset())
    strict = _thresholds(autonomy_requires_approval=frozenset(AutonomyLevel))

    assert requires_approval(AutonomyLevel.A2, permissive) is False
    assert requires_approval(AutonomyLevel.A0, strict) is True


# ---------------------------------------------------------------------------
# compute_expires_at — event-catalog.md §7.1's per-action-type TTL
# ---------------------------------------------------------------------------


def test_compute_expires_at_adds_configured_ttl_hours():
    thresholds = _thresholds(
        expiry={ActionType.OUTREACH_DRAFT: ApprovalExpiryPolicy(on_expiry="cancel", ttl_hours=72.0)}
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)

    expires_at = compute_expires_at(ActionType.OUTREACH_DRAFT, thresholds, now=now)

    assert expires_at == datetime(2026, 1, 4, tzinfo=UTC)


def test_compute_expires_at_null_ttl_never_expires():
    """record_delete: 'never autonomous, at any confidence, under any
    config' — ttl_hours=None means no expiry, ever."""
    thresholds = _thresholds(
        expiry={
            ActionType.RECORD_DELETE: ApprovalExpiryPolicy(on_expiry="escalate", ttl_hours=None)
        }
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert compute_expires_at(ActionType.RECORD_DELETE, thresholds, now=now) is None
