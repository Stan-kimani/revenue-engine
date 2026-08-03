"""Unit tests for the attribute-provenance envelope validation
(entity-model.md §2) that gates every write to an `attributes` JSONB column.

No network, no LLM, no database (build-spec §8.1).
"""

from __future__ import annotations

import pytest

from revenue_engine.core.errors import InvalidAttributeEnvelopeError
from revenue_engine.db.repositories import _validate_attributes


def test_valid_envelope_passes() -> None:
    _validate_attributes(
        {
            "industry": {
                "value": "B2B SaaS",
                "confidence": 0.82,
                "evidence": "Homepage copy: 'the operating system for B2B SaaS finance teams'",
                "source": "llm:enrich_company",
                "run_id": "a4f1c2d3-1234-4567-89ab-cdef01234567",
                "observed_at": "2026-07-23T09:14:00Z",
            }
        }
    )


def test_null_value_is_explicitly_allowed() -> None:
    """phase1-llm-boundary.md §2: null is the correct answer when evidence is absent."""
    _validate_attributes(
        {
            "industry": {
                "value": None,
                "confidence": 0.1,
                "evidence": None,
                "source": "llm:enrich_company",
                "run_id": "a4f1c2d3-1234-4567-89ab-cdef01234567",
                "observed_at": "2026-07-23T09:14:00Z",
            }
        }
    )


def test_provider_source_with_null_run_id_and_null_evidence() -> None:
    """Non-LLM sources have no natural text evidence — null is allowed there too."""
    _validate_attributes(
        {
            "employee_count": {
                "value": 12,
                "confidence": 1.0,
                "evidence": None,
                "source": "provider:apollo",
                "run_id": None,
                "observed_at": "2026-07-23T09:14:00Z",
            }
        }
    )


def test_bare_scalar_rejected() -> None:
    with pytest.raises(InvalidAttributeEnvelopeError):
        _validate_attributes({"industry": "B2B SaaS"})


def test_missing_required_field_rejected() -> None:
    with pytest.raises(InvalidAttributeEnvelopeError):
        _validate_attributes(
            {
                "industry": {
                    "value": "B2B SaaS",
                    "confidence": 0.82,
                    "evidence": "some snippet",
                }
            }
        )


def test_missing_evidence_rejected() -> None:
    """evidence is required (corrected 2026-08-04) — omitting it, not just
    setting it null, must fail validation."""
    with pytest.raises(InvalidAttributeEnvelopeError):
        _validate_attributes(
            {
                "industry": {
                    "value": "B2B SaaS",
                    "confidence": 0.82,
                    "source": "llm:enrich_company",
                    "run_id": None,
                    "observed_at": "2026-07-23T09:14:00Z",
                }
            }
        )


def test_invalid_source_kind_rejected() -> None:
    with pytest.raises(InvalidAttributeEnvelopeError):
        _validate_attributes(
            {
                "industry": {
                    "value": "B2B SaaS",
                    "confidence": 0.82,
                    "evidence": "some snippet",
                    "source": "guess:whatever",
                    "run_id": None,
                    "observed_at": "2026-07-23T09:14:00Z",
                }
            }
        )


def test_confidence_out_of_range_rejected() -> None:
    with pytest.raises(InvalidAttributeEnvelopeError):
        _validate_attributes(
            {
                "industry": {
                    "value": "B2B SaaS",
                    "confidence": 1.5,
                    "evidence": "some snippet",
                    "source": "llm:enrich_company",
                    "run_id": None,
                    "observed_at": "2026-07-23T09:14:00Z",
                }
            }
        )
