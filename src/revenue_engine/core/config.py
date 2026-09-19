"""Typed, validated config loader (CLAUDE.md §1 non-negotiable 9: no
industry/vertical/ICP/brand voice is hardcoded — it comes from here).

Loads `config/base.yaml` plus exactly one industry pack from
`config/industries/*.yaml`, validates the pack against
`schemas/entities/industry_pack.json`, and refuses to boot (raises
`ConfigError`, never a warning) if:

- the pack fails that schema,
- `scoring.weights` does not sum to 1.0 (+/- 0.001), or
- the pack's `objections.categories` keys drift from the `objection_category`
  enum in either `schemas/outputs/reply_classification.json` or
  `schemas/outputs/objection_response.json`.

`load_config()` is the pure, uncached, injectable-paths entry point — tests
construct arbitrary base/pack content with it directly. `get_config()` is the
process-wide cached singleton every other module should call.

This replaces the temporary `QueueConfig`/`_load_config()` that lived in
core/queue.py from M0.3 (docs/decisions.md, M0.3 entry: "to be replaced by
core/config.py's typed, validated loader at M0.4").
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
import yaml

from ..db.models import ActionType, AutonomyLevel, EmailStatus, Tier
from .disqualifiers import parse_rule
from .errors import ConfigError, DisqualifierRuleError

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_BASE_CONFIG_PATH = _REPO_ROOT / "config" / "base.yaml"
_DEFAULT_THRESHOLDS_CONFIG_PATH = _REPO_ROOT / "config" / "thresholds.yaml"
_DEFAULT_INDUSTRIES_DIR = _REPO_ROOT / "config" / "industries"
_DEFAULT_PACK_SCHEMA_PATH = _REPO_ROOT / "schemas" / "entities" / "industry_pack.json"
_REPLY_CLASSIFICATION_SCHEMA_PATH = _REPO_ROOT / "schemas" / "outputs" / "reply_classification.json"
_OBJECTION_RESPONSE_SCHEMA_PATH = _REPO_ROOT / "schemas" / "outputs" / "objection_response.json"

_WEIGHT_SUM_TOLERANCE = 0.001
_REQUIRED_TIERS = frozenset(t.value for t in Tier)
_REQUIRED_ACTION_TYPES = frozenset(a.value for a in ActionType)
_VALID_ON_EXPIRY = frozenset({"cancel", "escalate"})
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_REQUIRED_RATE_METRICS = frozenset({"bounce", "spam_complaint", "unsubscribe"})
_VALID_BELOW_FLOOR_REASONS = frozenset({"hard_bounce", "spam_complaint"})


# ============================================================================
# Typed accessors
# ============================================================================


@dataclass(frozen=True)
class QueueConfig:
    max_attempts: int
    backoff_base_s: float
    backoff_cap_s: float
    visibility_timeout_s: int


@dataclass(frozen=True)
class LLMConfig:
    timeout_s: float
    max_client_retries: int


@dataclass(frozen=True)
class ApprovalExpiryPolicy:
    """One `action_type`'s row in event-catalog.md §7.1's expiry table.
    `on_expiry`: 'cancel' (terminal — approval moves to `expired`) or
    'escalate' (never cancels — stays `pending`, `approval.expired` fires
    once as a notification signal only). `ttl_hours=None` means never
    expires (`record_delete`: never autonomous, at any confidence, under any
    config — CLAUDE.md §1 non-negotiable 8 / agent-contracts.md §5)."""

    on_expiry: str
    ttl_hours: float | None


@dataclass(frozen=True)
class ThresholdsConfig:
    """`config/thresholds.yaml` — build-spec §2: "Autonomy thresholds (HITL
    triggers)". Read by core/approvals.py, never hardcoded per call site
    (CLAUDE.md §3)."""

    autonomy_requires_approval: frozenset[AutonomyLevel]
    """Which agent-contracts.md §0.4 levels create a BLOCKING pending
    approval when passed to `request_approval()`. Config-driven per M1.2's
    plan-approval Correction: A0 has no external side effects to gate, A1's
    side effects are autonomous within config caps — approval_gates.py must
    never hardcode "A2 and up" as a Python literal, so tightening this later
    (e.g. requiring approval at A1 too) is a one-line config change."""
    expiry: MappingProxyType[ActionType, ApprovalExpiryPolicy]


@dataclass(frozen=True)
class RateThreshold:
    warn: float
    pause: float


@dataclass(frozen=True)
class HealthConfig:
    """docs/deliverability.md §6, with the sample floor added at M1.4a: below
    `sample_floor_sends` sends in the window, rates are not evaluated and the
    absolute `below_floor_pause_counts` apply instead."""

    window_days: int
    sample_floor_sends: int
    below_floor_pause_counts: MappingProxyType[str, int]
    """Keyed by suppression reason ('hard_bounce', 'spam_complaint')."""
    rates: MappingProxyType[str, RateThreshold]
    """Keyed by metric ('bounce', 'spam_complaint', 'unsubscribe')."""


@dataclass(frozen=True)
class EmailStatusTiers:
    """docs/deliverability.md §5. Every EmailStatus member belongs to exactly
    one tier — asserted at boot, so a status added later fails the boot rather
    than defaulting to sendable."""

    send: frozenset[EmailStatus]
    restricted: frozenset[EmailStatus]
    """Sendable, but under a sub-cap and with bounces weighted double."""
    never: frozenset[EmailStatus]

    def tier_of(self, status: EmailStatus) -> str:
        if status in self.send:
            return "send"
        if status in self.restricted:
            return "restricted"
        return "never"


@dataclass(frozen=True)
class DeliverabilityConfig:
    """docs/deliverability.md §4 and §7 — every number the send path uses."""

    sending_domain: str
    from_address: str
    daily_cap: int
    hourly_cap: int
    min_gap_seconds: int
    jitter_seconds: int
    send_window_start: time
    send_window_end: time
    send_days: frozenset[int]
    """Weekday numbers, Monday=0 (datetime.weekday())."""
    timezone_source: str
    default_recipient_timezone: str
    country_timezones: MappingProxyType[str, str]
    email_status_tiers: EmailStatusTiers
    catch_all_share: float
    """Fraction of `daily_cap` that may go to catch-all recipients. Expressed
    as a share, not an absolute, so it tracks daily_cap across the warmup ramp
    (5 -> 40) instead of silently becoming absurdly restrictive."""
    bounce_weight_by_status: MappingProxyType[str, float]
    role_based_local_parts: frozenset[str]
    max_links: int
    opt_out_sentence: str
    physical_address: str
    """Empty is a valid config value (not yet decided); the content gate
    refuses every send while it is empty."""
    authorization_ttl_seconds: int
    gmail_timeout_seconds: int
    health: HealthConfig

    @property
    def catch_all_daily_cap(self) -> int:
        """`catch_all_share` of daily_cap, floored, minimum 1 — one restricted
        send is always permitted while the cap is non-zero, so a small warmup
        cap doesn't silently forbid the category outright."""
        if self.daily_cap <= 0:
            return 0
        return max(1, int(self.daily_cap * self.catch_all_share))


@dataclass(frozen=True)
class ModelTierConfig:
    model: str
    input_cost_per_mtok: Decimal
    output_cost_per_mtok: Decimal


@dataclass(frozen=True)
class ScoringConfig:
    weights: MappingProxyType[str, float]
    budget_fit_map: MappingProxyType[str, float]
    bands: MappingProxyType[str, float]
    min_confidence_to_store: float
    guidance: str
    """Rendered verbatim as {{scoring_guidance}} in qualification/score_lead.md."""
    llm_subscore_weights: MappingProxyType[str, float]
    """How agents/qualification.py combines score_lead.md's three fuzzy
    sub-scores (buying_intent, seniority_fit, narrative_fit) into the single
    `intent` component `weights` above defines — a scoring decision, so it
    lives in the pack like every other one (M1.2 Correction 3, docs/decisions.md).
    Must sum to 1.0 +/- _WEIGHT_SUM_TOLERANCE, asserted at boot below, same as
    `weights` itself."""
    engagement_points: MappingProxyType[str, float]
    """Raw points per counted engagement signal (currently `reply`, `meeting`)
    — CLAUDE.md §3: tunable numbers live in config, never as literals in
    agents/qualification.py. `open`/`click` are deliberately absent, not
    present at 0: no tracking columns exist yet (messages has no
    opened_at/clicked_at), so there is nothing to count, not a weighting
    choice."""
    engagement_saturation: float
    """Raw points at which the engagement component saturates at 1.0."""


@dataclass(frozen=True)
class VoiceConfig:
    sender_persona: str
    """Rendered as {{sender_persona}}."""
    tone_rules: tuple[str, ...]
    vocabulary_say: tuple[str, ...]
    vocabulary_avoid: tuple[str, ...]

    def as_prompt_text(self) -> str:
        """Rendered as {{voice_rules}} — tone rules plus the say/avoid
        vocabulary, flattened to plain text for direct prompt injection."""
        lines = list(self.tone_rules)
        if self.vocabulary_say:
            lines.append("Use: " + ", ".join(self.vocabulary_say))
        if self.vocabulary_avoid:
            lines.append("Avoid: " + ", ".join(self.vocabulary_avoid))
        return "\n".join(f"- {line}" for line in lines)


@dataclass(frozen=True)
class IndustryPack:
    """Typed accessors for the sections other M0.4 code touches directly
    (status, scoring, voice, objection categories). The remaining sections —
    icp, qualification, commercial_boundaries, sequences, service_catalogue,
    discovery, channels, account_limits — are exposed as read-only mappings
    rather than hand-modelled dataclasses: nothing before the milestones that
    actually consume them (scoring/campaigns/sequencing) needs field-level
    typing, and modelling them now would be speculative (CLAUDE.md §4,
    "minimum abstraction"). Every section is still reached through a named
    attribute, never a raw dict key lookup on the loaded YAML.
    """

    name: str
    version: int
    status: str
    icp: MappingProxyType[str, Any]
    scoring: ScoringConfig
    qualification: MappingProxyType[str, Any]
    voice: VoiceConfig
    commercial_boundaries: MappingProxyType[str, Any]
    objection_categories: MappingProxyType[str, str]
    sequences: MappingProxyType[str, Any]
    service_catalogue: MappingProxyType[str, Any]
    discovery: MappingProxyType[str, Any]
    channels: MappingProxyType[str, bool]
    account_limits: MappingProxyType[str, Any]
    outreach_draft_bands: frozenset[str]
    """`outreach.draft_bands` — which qualification bands Sales drafts
    first-touch outreach for (M1.4a). Default in the shipped pack is [sql],
    matching agent-contracts.md §2/§3; adding mql is a deliberate pack edit."""

    @property
    def is_draft(self) -> bool:
        """draft packs must not be usable for campaign launch — this exposes
        the state; enforcement lands with campaigns (a later milestone)."""
        return self.status == "draft"


@dataclass(frozen=True)
class Config:
    queue: QueueConfig
    llm: LLMConfig
    thresholds: ThresholdsConfig
    deliverability: DeliverabilityConfig
    models: MappingProxyType[Tier, ModelTierConfig]
    pack: IndustryPack

    def model_for(self, tier: Tier) -> str:
        return self.models[tier].model

    def cost_rates_for(self, tier: Tier) -> ModelTierConfig:
        return self.models[tier]

    def compute_cost(self, tier: Tier, *, input_tokens: int, output_tokens: int) -> Decimal:
        """Money is numeric/Decimal throughout (CLAUDE.md §1 non-negotiable
        12) — never float, even for a per-call cost this small."""
        rates = self.cost_rates_for(tier)
        million = Decimal(1_000_000)
        return (Decimal(input_tokens) / million) * rates.input_cost_per_mtok + (
            Decimal(output_tokens) / million
        ) * rates.output_cost_per_mtok


# ============================================================================
# Loading
# ============================================================================


def load_config(
    *,
    base_config_path: Path = _DEFAULT_BASE_CONFIG_PATH,
    thresholds_config_path: Path = _DEFAULT_THRESHOLDS_CONFIG_PATH,
    industries_dir: Path = _DEFAULT_INDUSTRIES_DIR,
    pack_schema_path: Path = _DEFAULT_PACK_SCHEMA_PATH,
    reply_classification_schema_path: Path = _REPLY_CLASSIFICATION_SCHEMA_PATH,
    objection_response_schema_path: Path = _OBJECTION_RESPONSE_SCHEMA_PATH,
    industry_pack: str | None = None,
) -> Config:
    """Uncached. Every path is injectable so tests can point this at
    fixture files without touching the real config/ or schemas/ trees —
    including the protected boot-failure tests, which need to construct
    invalid content on purpose. `get_config()` below is what production code
    calls; this is the function it (and tests) call underneath.
    """
    if not base_config_path.is_file():
        raise ConfigError(f"Base config not found: {base_config_path}")
    raw_base = yaml.safe_load(base_config_path.read_text())

    queue_cfg = _parse_queue(raw_base, base_config_path)
    llm_cfg = _parse_llm(raw_base, base_config_path)
    models_cfg = _parse_models(raw_base, base_config_path)
    deliverability_cfg = _parse_deliverability(raw_base, base_config_path)
    thresholds_cfg = _parse_thresholds(thresholds_config_path)

    pack_name = (
        industry_pack or os.environ.get("INDUSTRY_PACK") or _autodetect_pack_name(industries_dir)
    )
    pack_path = industries_dir / f"{pack_name}.yaml"
    if not pack_path.is_file():
        raise ConfigError(f"Industry pack not found: {pack_path}")
    raw_pack = yaml.safe_load(pack_path.read_text())
    if not isinstance(raw_pack, dict):
        raise ConfigError(f"{pack_path}: expected a YAML mapping at the top level")

    _validate_pack_schema(raw_pack, pack_schema_path, pack_path)
    _assert_weights_sum_to_one(raw_pack, pack_path)
    _assert_llm_subscore_weights_sum_to_one(raw_pack, pack_path)
    _assert_disqualifiers_evaluable(raw_pack, pack_path)
    _assert_objection_categories_match(
        raw_pack, pack_path, reply_classification_schema_path, objection_response_schema_path
    )

    pack = _parse_pack(raw_pack)
    return Config(
        queue=queue_cfg,
        llm=llm_cfg,
        thresholds=thresholds_cfg,
        deliverability=deliverability_cfg,
        models=models_cfg,
        pack=pack,
    )


@cache
def get_config() -> Config:
    """Process-wide singleton. Production code (core/llm.py, core/queue.py,
    ...) calls this, never load_config() directly, so config is read and
    validated exactly once per process."""
    return load_config()


# ============================================================================
# Parsing helpers
# ============================================================================


def _parse_queue(raw_base: dict[str, Any], base_config_path: Path) -> QueueConfig:
    try:
        q = raw_base["queue"]
        return QueueConfig(
            max_attempts=q["max_attempts"],
            backoff_base_s=q["backoff_base_s"],
            backoff_cap_s=q["backoff_cap_s"],
            visibility_timeout_s=q["visibility_timeout_s"],
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"{base_config_path}: missing or malformed 'queue' block: {exc}") from exc


def _parse_llm(raw_base: dict[str, Any], base_config_path: Path) -> LLMConfig:
    try:
        block = raw_base["llm"]
        return LLMConfig(
            timeout_s=float(block["timeout_s"]),
            max_client_retries=int(block["max_client_retries"]),
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"{base_config_path}: missing or malformed 'llm' block: {exc}") from exc


def _parse_deliverability(raw_base: dict[str, Any], base_config_path: Path) -> DeliverabilityConfig:
    """docs/deliverability.md §4/§6/§7. Refuses to boot on anything missing or
    malformed — a send path running on a guessed cap or window is exactly the
    failure this block exists to prevent."""

    def fail(detail: str) -> ConfigError:
        return ConfigError(f"{base_config_path}: 'deliverability' block: {detail}")

    try:
        d = raw_base["deliverability"]
        health_raw = d["health"]
        rates_raw = health_raw["rates"]

        sending_domain = str(d["sending_domain"]).strip().lower()
        from_address = str(d["from_address"]).strip().lower()
        if not sending_domain or "@" in sending_domain:
            raise fail(f"sending_domain must be a bare domain, got {sending_domain!r}")
        if from_address.count("@") != 1:
            raise fail(f"from_address must be an email address, got {from_address!r}")

        ints = {
            name: int(d[name])
            for name in (
                "daily_cap",
                "hourly_cap",
                "min_gap_seconds",
                "jitter_seconds",
                "max_links",
                "authorization_ttl_seconds",
                "gmail_timeout_seconds",
            )
        }
        for name, value in ints.items():
            if value < 0:
                raise fail(f"{name} must be >= 0, got {value}")

        start = time.fromisoformat(str(d["send_window_start"]))
        end = time.fromisoformat(str(d["send_window_end"]))
        if start >= end:
            raise fail(f"send_window_start {start} must be before send_window_end {end}")

        days_raw = d["send_days"]
        unknown_days = [x for x in days_raw if str(x).lower() not in _WEEKDAYS]
        if unknown_days or not days_raw:
            raise fail(f"send_days must be a non-empty subset of {sorted(_WEEKDAYS)}")
        send_days = frozenset(_WEEKDAYS[str(x).lower()] for x in days_raw)

        timezone_source = str(d["timezone_source"])
        if timezone_source != "recipient":
            raise fail(f"timezone_source must be 'recipient', got {timezone_source!r}")

        default_tz = str(d["default_recipient_timezone"])
        country_timezones = {str(k).upper(): str(v) for k, v in d["country_timezones"].items()}
        for tz_name in [default_tz, *country_timezones.values()]:
            try:
                ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise fail(f"unknown IANA timezone {tz_name!r}") from exc

        tiers_raw = d["email_status_tiers"]
        if set(tiers_raw) != {"send", "restricted", "never"}:
            raise fail(
                "email_status_tiers must define exactly ['never', 'restricted', 'send'], "
                f"found {sorted(tiers_raw)}"
            )
        tiers = {name: [EmailStatus(v) for v in values] for name, values in tiers_raw.items()}
        classified = [status for values in tiers.values() for status in values]
        duplicates = sorted({s.value for s in classified if classified.count(s) > 1})
        unclassified = sorted(s.value for s in EmailStatus if s not in classified)
        if duplicates or unclassified:
            raise fail(
                "every EmailStatus must appear in exactly one email_status_tiers tier — "
                f"unclassified: {unclassified}, in more than one: {duplicates}"
            )
        email_status_tiers = EmailStatusTiers(
            send=frozenset(tiers["send"]),
            restricted=frozenset(tiers["restricted"]),
            never=frozenset(tiers["never"]),
        )

        catch_all_share = float(d["catch_all_share"])
        if not 0 <= catch_all_share <= 1:
            raise fail(f"catch_all_share must be between 0 and 1, got {catch_all_share}")
        bounce_weights = {str(k): float(v) for k, v in d["bounce_weight_by_status"].items()}
        unknown_weighted = sorted(set(bounce_weights) - {s.value for s in EmailStatus})
        if unknown_weighted:
            raise fail(f"bounce_weight_by_status names unknown statuses: {unknown_weighted}")
        if any(w < 0 for w in bounce_weights.values()):
            raise fail("bounce_weight_by_status values must be >= 0")
        role_local_parts = frozenset(str(p).strip().lower() for p in d["role_based_local_parts"])

        below_floor = {str(k): int(v) for k, v in health_raw["below_floor_pause_counts"].items()}
        if set(below_floor) - _VALID_BELOW_FLOOR_REASONS or not below_floor:
            raise fail(
                "health.below_floor_pause_counts keys must be a non-empty subset of "
                f"{sorted(_VALID_BELOW_FLOOR_REASONS)}"
            )
        if set(rates_raw) != _REQUIRED_RATE_METRICS:
            raise fail(f"health.rates must define exactly {sorted(_REQUIRED_RATE_METRICS)}")
        rates: dict[str, RateThreshold] = {}
        for metric, pair in rates_raw.items():
            threshold = RateThreshold(warn=float(pair["warn"]), pause=float(pair["pause"]))
            if not 0 <= threshold.warn <= threshold.pause <= 1:
                raise fail(f"health.rates.{metric} must satisfy 0 <= warn <= pause <= 1")
            rates[metric] = threshold

        window_days = int(health_raw["window_days"])
        sample_floor = int(health_raw["sample_floor_sends"])
        if window_days < 1 or sample_floor < 0:
            raise fail("health.window_days must be >= 1 and sample_floor_sends >= 0")

        return DeliverabilityConfig(
            sending_domain=sending_domain,
            from_address=from_address,
            daily_cap=ints["daily_cap"],
            hourly_cap=ints["hourly_cap"],
            min_gap_seconds=ints["min_gap_seconds"],
            jitter_seconds=ints["jitter_seconds"],
            send_window_start=start,
            send_window_end=end,
            send_days=send_days,
            timezone_source=timezone_source,
            default_recipient_timezone=default_tz,
            country_timezones=MappingProxyType(country_timezones),
            email_status_tiers=email_status_tiers,
            catch_all_share=catch_all_share,
            bounce_weight_by_status=MappingProxyType(bounce_weights),
            role_based_local_parts=role_local_parts,
            max_links=ints["max_links"],
            opt_out_sentence=str(d["opt_out_sentence"]).strip(),
            physical_address=str(d["physical_address"] or "").strip(),
            authorization_ttl_seconds=ints["authorization_ttl_seconds"],
            gmail_timeout_seconds=ints["gmail_timeout_seconds"],
            health=HealthConfig(
                window_days=window_days,
                sample_floor_sends=sample_floor,
                below_floor_pause_counts=MappingProxyType(below_floor),
                rates=MappingProxyType(rates),
            ),
        )
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise fail(f"missing or malformed: {exc!r}") from exc


def _parse_thresholds(thresholds_config_path: Path) -> ThresholdsConfig:
    """`config/thresholds.yaml` is a top-level file, not nested in
    `base.yaml`, so it gets its own read + fail-loud-if-missing, same
    discipline as the pack itself."""
    if not thresholds_config_path.is_file():
        raise ConfigError(f"Thresholds config not found: {thresholds_config_path}")
    raw = yaml.safe_load(thresholds_config_path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError(f"{thresholds_config_path}: expected a YAML mapping at the top level")

    try:
        approvals_raw = raw["approvals"]
        autonomy_raw = approvals_raw["autonomy_requires_approval"]
        expiry_raw = approvals_raw["expiry"]
    except (KeyError, TypeError) as exc:
        raise ConfigError(
            f"{thresholds_config_path}: missing or malformed 'approvals' block: {exc}"
        ) from exc

    try:
        autonomy_levels = frozenset(AutonomyLevel(v) for v in autonomy_raw)
    except ValueError as exc:
        raise ConfigError(
            f"{thresholds_config_path}: 'approvals.autonomy_requires_approval' contains an "
            f"invalid autonomy level: {exc}"
        ) from exc

    found_action_types = set(expiry_raw.keys()) if isinstance(expiry_raw, dict) else set()
    if found_action_types != _REQUIRED_ACTION_TYPES:
        raise ConfigError(
            f"{thresholds_config_path}: 'approvals.expiry' must define exactly the action types "
            f"{sorted(_REQUIRED_ACTION_TYPES)} (event-catalog.md §7.1), found "
            f"{sorted(found_action_types)}"
        )

    expiry: dict[ActionType, ApprovalExpiryPolicy] = {}
    for action_type_str, policy_raw in expiry_raw.items():
        try:
            on_expiry = policy_raw["on_expiry"]
            ttl_hours = policy_raw["ttl_hours"]
        except (KeyError, TypeError) as exc:
            raise ConfigError(
                f"{thresholds_config_path}: 'approvals.expiry.{action_type_str}' missing "
                f"'on_expiry' or 'ttl_hours': {exc}"
            ) from exc
        if on_expiry not in _VALID_ON_EXPIRY:
            raise ConfigError(
                f"{thresholds_config_path}: 'approvals.expiry.{action_type_str}.on_expiry' must "
                f"be one of {sorted(_VALID_ON_EXPIRY)}, got {on_expiry!r}"
            )
        if ttl_hours is not None and (not isinstance(ttl_hours, int | float) or ttl_hours <= 0):
            raise ConfigError(
                f"{thresholds_config_path}: 'approvals.expiry.{action_type_str}.ttl_hours' must "
                f"be null or a positive number, got {ttl_hours!r}"
            )
        expiry[ActionType(action_type_str)] = ApprovalExpiryPolicy(
            on_expiry=on_expiry, ttl_hours=float(ttl_hours) if ttl_hours is not None else None
        )

    return ThresholdsConfig(
        autonomy_requires_approval=autonomy_levels, expiry=MappingProxyType(expiry)
    )


def _parse_models(
    raw_base: dict[str, Any], base_config_path: Path
) -> MappingProxyType[Tier, ModelTierConfig]:
    try:
        raw_models = raw_base["models"]
    except KeyError as exc:
        raise ConfigError(f"{base_config_path}: missing 'models' block") from exc

    found = set(raw_models.keys())
    if found != _REQUIRED_TIERS:
        raise ConfigError(
            f"{base_config_path}: 'models' must define exactly the tiers "
            f"{sorted(_REQUIRED_TIERS)}, found {sorted(found)}"
        )

    parsed: dict[Tier, ModelTierConfig] = {}
    for tier_name, entry in raw_models.items():
        try:
            parsed[Tier(tier_name)] = ModelTierConfig(
                model=entry["model"],
                input_cost_per_mtok=Decimal(str(entry["input_cost_per_mtok"])),
                output_cost_per_mtok=Decimal(str(entry["output_cost_per_mtok"])),
            )
        except (KeyError, TypeError) as exc:
            raise ConfigError(
                f"{base_config_path}: malformed 'models.{tier_name}' entry: {exc}"
            ) from exc
    return MappingProxyType(parsed)


def _autodetect_pack_name(industries_dir: Path) -> str:
    candidates = sorted(p.stem for p in industries_dir.glob("*.yaml") if not p.stem.startswith("_"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ConfigError(f"No industry packs found under {industries_dir}")
    raise ConfigError(
        f"Multiple industry packs found under {industries_dir} ({candidates}) and "
        "INDUSTRY_PACK is unset — set INDUSTRY_PACK to select one."
    )


def _validate_pack_schema(raw_pack: dict[str, Any], schema_path: Path, pack_path: Path) -> None:
    if not schema_path.is_file():
        raise ConfigError(f"Industry pack schema not found: {schema_path}")
    schema = json.loads(schema_path.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = [e.message for e in validator.iter_errors(raw_pack)]
    if errors:
        raise ConfigError(f"{pack_path} failed schema validation: {'; '.join(errors)}")


def _assert_weights_sum_to_one(raw_pack: dict[str, Any], pack_path: Path) -> None:
    weights = raw_pack["scoring"]["weights"]
    total = sum(float(w) for w in weights.values())
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise ConfigError(
            f"{pack_path}: scoring.weights must sum to 1.0 (+/- {_WEIGHT_SUM_TOLERANCE}), "
            f"got {total} from {dict(weights)}"
        )


def _assert_llm_subscore_weights_sum_to_one(raw_pack: dict[str, Any], pack_path: Path) -> None:
    weights = raw_pack["scoring"]["llm_subscore_weights"]
    total = sum(float(w) for w in weights.values())
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise ConfigError(
            f"{pack_path}: scoring.llm_subscore_weights must sum to 1.0 "
            f"(+/- {_WEIGHT_SUM_TOLERANCE}), got {total} from {dict(weights)}"
        )


def _assert_disqualifiers_evaluable(raw_pack: dict[str, Any], pack_path: Path) -> None:
    """Fails the boot on any icp.disqualifiers[] entry the deterministic
    scorer cannot evaluate, unless it is explicitly marked
    `enforcement: manual` (M1.2 Correction 1, docs/decisions.md). A
    disqualifier that silently can't be checked is worse than none — it
    reads as protection that isn't there."""
    for entry in raw_pack.get("icp", {}).get("disqualifiers", []):
        if entry.get("enforcement") == "manual":
            continue
        try:
            parse_rule(entry["rule"])
        except DisqualifierRuleError as exc:
            raise ConfigError(
                f"{pack_path}: disqualifier '{entry['id']}' has a rule the deterministic "
                f"scorer cannot evaluate ({exc.reason}). Either rewrite it in the supported "
                "grammar (core/disqualifiers.py), or mark it `enforcement: manual` and make "
                "sure qualification.discovery_checklist covers it on the human call."
            ) from exc


def _assert_objection_categories_match(
    raw_pack: dict[str, Any],
    pack_path: Path,
    reply_classification_schema_path: Path,
    objection_response_schema_path: Path,
) -> None:
    pack_categories = set(raw_pack["objections"]["categories"].keys())

    reply_schema = json.loads(reply_classification_schema_path.read_text())
    reply_enum = {
        v for v in reply_schema["properties"]["objection_category"]["enum"] if v is not None
    }

    objection_schema = json.loads(objection_response_schema_path.read_text())
    objection_enum = set(objection_schema["properties"]["objection_category"]["enum"])

    if not (pack_categories == reply_enum == objection_enum):
        raise ConfigError(
            "Objection category drift: "
            f"{pack_path} has {sorted(pack_categories)}, "
            f"{reply_classification_schema_path.name} has {sorted(reply_enum)}, "
            f"{objection_response_schema_path.name} has {sorted(objection_enum)} — "
            "all three must match exactly."
        )


def _parse_pack(raw_pack: dict[str, Any]) -> IndustryPack:
    scoring_raw = raw_pack["scoring"]
    scoring = ScoringConfig(
        weights=MappingProxyType(dict(scoring_raw["weights"])),
        budget_fit_map=MappingProxyType(dict(scoring_raw["budget_fit_map"])),
        bands=MappingProxyType(dict(scoring_raw["bands"])),
        min_confidence_to_store=scoring_raw["min_confidence_to_store"],
        guidance=scoring_raw["guidance"],
        llm_subscore_weights=MappingProxyType(dict(scoring_raw["llm_subscore_weights"])),
        engagement_points=MappingProxyType(
            {k: float(v) for k, v in scoring_raw["engagement_points"].items()}
        ),
        engagement_saturation=float(scoring_raw["engagement_saturation"]),
    )

    voice_raw = raw_pack["voice"]
    voice = VoiceConfig(
        sender_persona=voice_raw["sender_persona"],
        tone_rules=tuple(voice_raw["tone_rules"]),
        vocabulary_say=tuple(voice_raw["vocabulary"].get("say", [])),
        vocabulary_avoid=tuple(voice_raw["vocabulary"].get("avoid", [])),
    )

    return IndustryPack(
        name=raw_pack["name"],
        version=raw_pack["version"],
        status=raw_pack["status"],
        icp=MappingProxyType(dict(raw_pack["icp"])),
        scoring=scoring,
        qualification=MappingProxyType(dict(raw_pack["qualification"])),
        voice=voice,
        commercial_boundaries=MappingProxyType(dict(raw_pack["commercial_boundaries"])),
        objection_categories=MappingProxyType(dict(raw_pack["objections"]["categories"])),
        sequences=MappingProxyType(dict(raw_pack["sequences"])),
        service_catalogue=MappingProxyType(dict(raw_pack["service_catalogue"])),
        discovery=MappingProxyType(dict(raw_pack["discovery"])),
        channels=MappingProxyType(dict(raw_pack["channels"])),
        account_limits=MappingProxyType(dict(raw_pack["account_limits"])),
        outreach_draft_bands=frozenset(raw_pack["outreach"]["draft_bands"]),
    )
