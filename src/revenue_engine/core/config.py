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
from decimal import Decimal
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import jsonschema
import yaml

from ..db.models import Tier
from .errors import ConfigError

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_BASE_CONFIG_PATH = _REPO_ROOT / "config" / "base.yaml"
_DEFAULT_INDUSTRIES_DIR = _REPO_ROOT / "config" / "industries"
_DEFAULT_PACK_SCHEMA_PATH = _REPO_ROOT / "schemas" / "entities" / "industry_pack.json"
_REPLY_CLASSIFICATION_SCHEMA_PATH = _REPO_ROOT / "schemas" / "outputs" / "reply_classification.json"
_OBJECTION_RESPONSE_SCHEMA_PATH = _REPO_ROOT / "schemas" / "outputs" / "objection_response.json"

_WEIGHT_SUM_TOLERANCE = 0.001
_REQUIRED_TIERS = frozenset(t.value for t in Tier)


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

    @property
    def is_draft(self) -> bool:
        """draft packs must not be usable for campaign launch — this exposes
        the state; enforcement lands with campaigns (a later milestone)."""
        return self.status == "draft"


@dataclass(frozen=True)
class Config:
    queue: QueueConfig
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
    models_cfg = _parse_models(raw_base, base_config_path)

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
    _assert_objection_categories_match(
        raw_pack, pack_path, reply_classification_schema_path, objection_response_schema_path
    )

    pack = _parse_pack(raw_pack)
    return Config(queue=queue_cfg, models=models_cfg, pack=pack)


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
    )
