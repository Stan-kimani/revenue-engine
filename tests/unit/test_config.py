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
_REAL_PACK_PATH = _REPO_ROOT / "config" / "industries" / "b2b-service-firms.yaml"
_PACK_SCHEMA = _REPO_ROOT / "schemas" / "entities" / "industry_pack.json"
_REPLY_SCHEMA = _REPO_ROOT / "schemas" / "outputs" / "reply_classification.json"
_OBJECTION_SCHEMA = _REPO_ROOT / "schemas" / "outputs" / "objection_response.json"


def _real_pack_data() -> dict:
    data = yaml.safe_load(_REAL_PACK_PATH.read_text())
    assert isinstance(data, dict)
    return data


def _load(industries_dir: Path, *, industry_pack: str | None = None) -> Config:
    return load_config(
        base_config_path=_REAL_BASE_CONFIG,
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
