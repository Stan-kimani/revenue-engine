"""Unit tests for core/config.py. Pure — every path is injected, so these
never touch the developer's real .env-selected database and don't need one;
the only I/O is reading fixture YAML this test writes itself into tmp_path,
plus the real schemas/ files (which validate, not mutate)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from revenue_engine.core.config import Config, get_config, load_config
from revenue_engine.core.errors import ConfigError
from revenue_engine.db.models import Tier

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_BASE_CONFIG = _REPO_ROOT / "config" / "base.yaml"
_REAL_THRESHOLDS_CONFIG = _REPO_ROOT / "config" / "thresholds.yaml"
_REAL_PACK_PATH = _REPO_ROOT / "config" / "industries" / "b2b-service-firms.yaml"
_PACK_SCHEMA = _REPO_ROOT / "schemas" / "entities" / "industry_pack.json"
_REPLY_SCHEMA = _REPO_ROOT / "schemas" / "outputs" / "reply_classification.json"
_OBJECTION_SCHEMA = _REPO_ROOT / "schemas" / "outputs" / "objection_response.json"


def _real_pack_data() -> dict:
    data = yaml.safe_load(_REAL_PACK_PATH.read_text())
    assert isinstance(data, dict)
    return data


def _real_thresholds_data() -> dict:
    data = yaml.safe_load(_REAL_THRESHOLDS_CONFIG.read_text())
    assert isinstance(data, dict)
    return data


def _load(
    industries_dir: Path,
    *,
    industry_pack: str | None = None,
    thresholds_config_path: Path = _REAL_THRESHOLDS_CONFIG,
) -> Config:
    return load_config(
        base_config_path=_REAL_BASE_CONFIG,
        thresholds_config_path=thresholds_config_path,
        industries_dir=industries_dir,
        pack_schema_path=_PACK_SCHEMA,
        reply_classification_schema_path=_REPLY_SCHEMA,
        objection_response_schema_path=_OBJECTION_SCHEMA,
        industry_pack=industry_pack,
    )


def _write_pack(industries_dir: Path, data: dict, name: str) -> None:
    industries_dir.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["name"] = name
    (industries_dir / f"{name}.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


# ---------------------------------------------------------------------------
# The real pack, as shipped
# ---------------------------------------------------------------------------


def test_real_pack_loads_and_exposes_typed_accessors():
    config = _load(_REAL_PACK_PATH.parent, industry_pack="b2b-service-firms")
    assert config.pack.name == "b2b-service-firms"
    assert config.pack.status == "draft"
    assert config.pack.is_draft is True
    assert abs(sum(config.pack.scoring.weights.values()) - 1.0) < 0.001
    assert config.model_for(Tier.FAST) == "claude-haiku-4-5-20251001"
    assert config.model_for(Tier.STANDARD) == "claude-sonnet-5"
    assert config.model_for(Tier.DEEP) == "claude-opus-5"
    assert "price" in config.pack.objection_categories
    assert config.pack.voice.sender_persona


def test_get_config_singleton_uses_real_files():
    config = get_config()
    assert config.pack.name == "b2b-service-firms"
    assert config is get_config()  # cached singleton, not re-parsed


def test_compute_cost_is_decimal_and_scales_with_tokens():
    config = _load(_REAL_PACK_PATH.parent, industry_pack="b2b-service-firms")
    cost = config.compute_cost(Tier.FAST, input_tokens=1_000_000, output_tokens=1_000_000)
    assert (
        cost
        == config.cost_rates_for(Tier.FAST).input_cost_per_mtok
        + config.cost_rates_for(Tier.FAST).output_cost_per_mtok
    )


# ---------------------------------------------------------------------------
# Hard boot failures — protected: these encode the explicit instruction that
# a bad weight sum or objection-category drift must refuse to boot, not warn.
# ---------------------------------------------------------------------------


@pytest.mark.protected
def test_refuses_to_boot_when_weights_do_not_sum_to_one(tmp_path: Path):
    data = _real_pack_data()
    data["scoring"] = dict(data["scoring"])
    data["scoring"]["weights"] = {"icp_match": 0.3, "intent": 0.2}  # sums to 0.5
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    with pytest.raises(ConfigError, match="sum to 1.0"):
        _load(industries_dir, industry_pack="test-pack")


@pytest.mark.protected
def test_refuses_to_boot_when_weights_sum_is_juuust_outside_tolerance(tmp_path: Path):
    data = _real_pack_data()
    data["scoring"] = dict(data["scoring"])
    # 1.0 + 0.002 — outside the +/- 0.001 tolerance
    data["scoring"]["weights"] = {"icp_match": 0.502, "intent": 0.5}
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    with pytest.raises(ConfigError, match="sum to 1.0"):
        _load(industries_dir, industry_pack="test-pack")


def test_boots_when_weights_sum_is_within_tolerance(tmp_path: Path):
    data = _real_pack_data()
    data["scoring"] = dict(data["scoring"])
    data["scoring"]["weights"] = {"icp_match": 0.5005, "intent": 0.5}  # within +/- 0.001
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    config = _load(industries_dir, industry_pack="test-pack")
    assert config.pack.name == "test-pack"


@pytest.mark.protected
def test_refuses_to_boot_when_objection_categories_drift(tmp_path: Path):
    data = _real_pack_data()
    categories = dict(data["objections"]["categories"])
    del categories["handoff"]  # now drifts from both output schema enums
    data["objections"] = {"categories": categories}
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    with pytest.raises(ConfigError, match="[Dd]rift"):
        _load(industries_dir, industry_pack="test-pack")


@pytest.mark.protected
def test_refuses_to_boot_when_pack_fails_json_schema(tmp_path: Path):
    data = _real_pack_data()
    del data["voice"]  # required by schemas/entities/industry_pack.json
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    with pytest.raises(ConfigError):
        _load(industries_dir, industry_pack="test-pack")


# ---------------------------------------------------------------------------
# M1.2 Correction 3 — llm_subscore_weights, same rigor as scoring.weights
# ---------------------------------------------------------------------------


@pytest.mark.protected
def test_refuses_to_boot_when_llm_subscore_weights_do_not_sum_to_one(tmp_path: Path):
    data = _real_pack_data()
    data["scoring"] = dict(data["scoring"])
    data["scoring"]["llm_subscore_weights"] = {"buying_intent": 0.3, "seniority_fit": 0.3}  # 0.6
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    with pytest.raises(ConfigError, match="llm_subscore_weights must sum to 1.0"):
        _load(industries_dir, industry_pack="test-pack")


def test_boots_when_llm_subscore_weights_sum_is_within_tolerance(tmp_path: Path):
    data = _real_pack_data()
    data["scoring"] = dict(data["scoring"])
    data["scoring"]["llm_subscore_weights"] = {
        "buying_intent": 0.3334,
        "seniority_fit": 0.3333,
        "narrative_fit": 0.3333,
    }
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    config = _load(industries_dir, industry_pack="test-pack")
    assert abs(sum(config.pack.scoring.llm_subscore_weights.values()) - 1.0) < 0.001


# ---------------------------------------------------------------------------
# M1.2 Correction 1 — a disqualifier the scorer cannot evaluate must fail the
# boot unless explicitly marked enforcement: manual.
# ---------------------------------------------------------------------------


@pytest.mark.protected
def test_refuses_to_boot_on_unparseable_disqualifier_rule(tmp_path: Path):
    data = _real_pack_data()
    data["icp"] = dict(data["icp"])
    data["icp"]["disqualifiers"] = [
        {
            "id": "fuzzy_rule",
            "rule": 'business_model == "creative_studio" OR positioning matches something',
            "reason": "unparseable — uses OR and `matches`, not marked manual",
        }
    ]
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    with pytest.raises(ConfigError, match="cannot evaluate"):
        _load(industries_dir, industry_pack="test-pack")


@pytest.mark.protected
def test_refuses_to_boot_on_disqualifier_rule_with_unknown_field(tmp_path: Path):
    data = _real_pack_data()
    data["icp"] = dict(data["icp"])
    data["icp"]["disqualifiers"] = [
        {
            "id": "unknown_field_rule",
            "rule": 'headcount == "1-10"',  # not employee_band, business_model, or revenue_signal
            "reason": "field the scorer cannot read",
        }
    ]
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    with pytest.raises(ConfigError, match="cannot evaluate"):
        _load(industries_dir, industry_pack="test-pack")


def test_boots_when_unparseable_disqualifier_is_marked_enforcement_manual(tmp_path: Path):
    data = _real_pack_data()
    data["icp"] = dict(data["icp"])
    data["icp"]["disqualifiers"] = [
        {
            "id": "fuzzy_rule",
            "rule": 'business_model == "creative_studio" OR positioning matches something',
            "reason": "unparseable, but explicitly marked manual",
            "enforcement": "manual",
        }
    ]
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, data, "test-pack")

    config = _load(industries_dir, industry_pack="test-pack")
    assert config.pack.name == "test-pack"


def test_boots_when_all_disqualifier_rules_parse(tmp_path: Path):
    # The real pack's structured disqualifiers (too_small, competitor,
    # enterprise) must parse; bespoke_creative/regulated_health are marked
    # enforcement: manual. This is really a regression guard on the shipped
    # pack, not a synthetic fixture.
    config = _load(_REAL_PACK_PATH.parent, industry_pack="b2b-service-firms")
    assert config.pack.name == "b2b-service-firms"


# ---------------------------------------------------------------------------
# Pack selection
# ---------------------------------------------------------------------------


def test_autodetects_single_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # .env sets a real INDUSTRY_PACK default for developer convenience — clear
    # it here so this test actually exercises autodetection, not the env var.
    monkeypatch.delenv("INDUSTRY_PACK", raising=False)
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, _real_pack_data(), "only-pack")

    config = _load(industries_dir)  # no industry_pack given, no env var
    assert config.pack.name == "only-pack"


def test_multiple_packs_without_selection_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INDUSTRY_PACK", raising=False)
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, _real_pack_data(), "pack-a")
    _write_pack(industries_dir, _real_pack_data(), "pack-b")

    with pytest.raises(ConfigError, match="Multiple industry packs"):
        _load(industries_dir)


def test_no_packs_found_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INDUSTRY_PACK", raising=False)
    industries_dir = tmp_path / "industries"
    industries_dir.mkdir()

    with pytest.raises(ConfigError, match="No industry packs"):
        _load(industries_dir)


def test_template_file_is_excluded_from_autodetection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("INDUSTRY_PACK", raising=False)
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, _real_pack_data(), "only-pack")
    (industries_dir / "_template.yaml").write_text("")

    config = _load(industries_dir)
    assert config.pack.name == "only-pack"


def test_industry_pack_env_var_selects_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, _real_pack_data(), "pack-a")
    _write_pack(industries_dir, _real_pack_data(), "pack-b")
    monkeypatch.setenv("INDUSTRY_PACK", "pack-b")

    config = _load(industries_dir)
    assert config.pack.name == "pack-b"


def test_explicit_argument_overrides_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, _real_pack_data(), "pack-a")
    _write_pack(industries_dir, _real_pack_data(), "pack-b")
    monkeypatch.setenv("INDUSTRY_PACK", "pack-b")

    config = _load(industries_dir, industry_pack="pack-a")
    assert config.pack.name == "pack-a"


def test_unknown_named_pack_raises(tmp_path: Path):
    industries_dir = tmp_path / "industries"
    _write_pack(industries_dir, _real_pack_data(), "pack-a")

    with pytest.raises(ConfigError, match="not found"):
        _load(industries_dir, industry_pack="does-not-exist")


def test_missing_base_config_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(
            base_config_path=tmp_path / "missing.yaml",
            industries_dir=_REAL_PACK_PATH.parent,
            pack_schema_path=_PACK_SCHEMA,
            reply_classification_schema_path=_REPLY_SCHEMA,
            objection_response_schema_path=_OBJECTION_SCHEMA,
            industry_pack="b2b-service-firms",
        )


# ---------------------------------------------------------------------------
# M1.3 — config/thresholds.yaml (approvals.autonomy_requires_approval,
# approvals.expiry — event-catalog.md §7.1)
# ---------------------------------------------------------------------------


def _write_thresholds(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "thresholds.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_real_thresholds_config_loads_and_exposes_typed_accessors():
    config = _load(_REAL_PACK_PATH.parent, industry_pack="b2b-service-firms")
    from revenue_engine.db.models import ActionType, AutonomyLevel

    assert config.thresholds.autonomy_requires_approval == frozenset(
        {AutonomyLevel.A2, AutonomyLevel.A3}
    )
    assert set(config.thresholds.expiry.keys()) == set(ActionType)
    assert config.thresholds.expiry[ActionType.RECORD_DELETE].ttl_hours is None
    assert config.thresholds.expiry[ActionType.RECORD_DELETE].on_expiry == "escalate"
    assert config.thresholds.expiry[ActionType.OUTREACH_DRAFT].ttl_hours == 72.0
    assert config.thresholds.expiry[ActionType.OUTREACH_DRAFT].on_expiry == "cancel"


def test_missing_thresholds_config_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        _load(
            _REAL_PACK_PATH.parent,
            industry_pack="b2b-service-firms",
            thresholds_config_path=tmp_path / "missing.yaml",
        )


@pytest.mark.protected
def test_refuses_to_boot_when_thresholds_expiry_is_missing_an_action_type(tmp_path: Path):
    data = _real_thresholds_data()
    del data["approvals"]["expiry"]["record_delete"]
    path = _write_thresholds(tmp_path, data)

    with pytest.raises(ConfigError, match="record_delete"):
        _load(
            _REAL_PACK_PATH.parent, industry_pack="b2b-service-firms", thresholds_config_path=path
        )


@pytest.mark.protected
def test_refuses_to_boot_on_invalid_on_expiry_value(tmp_path: Path):
    data = _real_thresholds_data()
    data["approvals"]["expiry"]["outreach_draft"]["on_expiry"] = "ignore"
    path = _write_thresholds(tmp_path, data)

    with pytest.raises(ConfigError, match="on_expiry"):
        _load(
            _REAL_PACK_PATH.parent, industry_pack="b2b-service-firms", thresholds_config_path=path
        )


def test_refuses_to_boot_on_negative_ttl_hours(tmp_path: Path):
    data = _real_thresholds_data()
    data["approvals"]["expiry"]["outreach_draft"]["ttl_hours"] = -5
    path = _write_thresholds(tmp_path, data)

    with pytest.raises(ConfigError, match="ttl_hours"):
        _load(
            _REAL_PACK_PATH.parent, industry_pack="b2b-service-firms", thresholds_config_path=path
        )


def test_refuses_to_boot_on_invalid_autonomy_level(tmp_path: Path):
    data = _real_thresholds_data()
    data["approvals"]["autonomy_requires_approval"] = ["A2", "A9"]
    path = _write_thresholds(tmp_path, data)

    with pytest.raises(ConfigError, match="invalid autonomy level"):
        _load(
            _REAL_PACK_PATH.parent, industry_pack="b2b-service-firms", thresholds_config_path=path
        )


def test_null_ttl_hours_is_accepted_as_never_expires(tmp_path: Path):
    data = _real_thresholds_data()
    data["approvals"]["expiry"]["crm_merge"]["ttl_hours"] = None
    path = _write_thresholds(tmp_path, data)

    config = _load(
        _REAL_PACK_PATH.parent, industry_pack="b2b-service-firms", thresholds_config_path=path
    )

    from revenue_engine.db.models import ActionType

    assert config.thresholds.expiry[ActionType.CRM_MERGE].ttl_hours is None


# ---------------------------------------------------------------------------
# M1.4a — config/base.yaml deliverability block (docs/deliverability.md §4/§6)
# ---------------------------------------------------------------------------


def _load_with_base(tmp_path: Path, base: dict) -> Config:
    base_path = tmp_path / "base.yaml"
    base_path.write_text(yaml.safe_dump(base, sort_keys=False))
    return load_config(
        base_config_path=base_path,
        thresholds_config_path=_REAL_THRESHOLDS_CONFIG,
        industries_dir=_REAL_PACK_PATH.parent,
        pack_schema_path=_PACK_SCHEMA,
        reply_classification_schema_path=_REPLY_SCHEMA,
        objection_response_schema_path=_OBJECTION_SCHEMA,
        industry_pack="b2b-service-firms",
    )


def _real_base_data() -> dict:
    data = yaml.safe_load(_REAL_BASE_CONFIG.read_text())
    assert isinstance(data, dict)
    return data


def test_real_deliverability_block_matches_the_warmup_week_and_the_icp_clock():
    config = _load(_REAL_PACK_PATH.parent, industry_pack="b2b-service-firms")
    d = config.deliverability
    assert d.daily_cap == 5
    assert d.sending_domain == "getkimani.com"
    assert d.default_recipient_timezone == "America/New_York"
    assert {s.value for s in d.allowed_email_statuses} == {"valid"}
    assert d.health.sample_floor_sends == 50
    assert dict(d.health.below_floor_pause_counts) == {"hard_bounce": 2, "spam_complaint": 1}


@pytest.mark.protected
def test_refuses_to_boot_without_a_deliverability_block(tmp_path: Path):
    base = _real_base_data()
    del base["deliverability"]
    with pytest.raises(ConfigError, match="deliverability"):
        _load_with_base(tmp_path, base)


def test_refuses_to_boot_on_an_unknown_recipient_timezone(tmp_path: Path):
    base = _real_base_data()
    base["deliverability"]["default_recipient_timezone"] = "Mars/Olympus_Mons"
    with pytest.raises(ConfigError, match="timezone"):
        _load_with_base(tmp_path, base)


def test_refuses_to_boot_on_an_inverted_send_window(tmp_path: Path):
    base = _real_base_data()
    base["deliverability"]["send_window_start"] = "17:00"
    base["deliverability"]["send_window_end"] = "08:00"
    with pytest.raises(ConfigError, match="send_window_start"):
        _load_with_base(tmp_path, base)


def test_refuses_to_boot_when_warn_exceeds_pause(tmp_path: Path):
    base = _real_base_data()
    base["deliverability"]["health"]["rates"]["bounce"] = {"warn": 0.05, "pause": 0.03}
    with pytest.raises(ConfigError, match="warn <= pause"):
        _load_with_base(tmp_path, base)
