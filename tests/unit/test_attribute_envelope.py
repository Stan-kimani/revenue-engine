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
                "source": "llm:enrich_company",
                "confidence": 0.82,
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
                "source": "llm:enrich_company",
                "confidence": 0.1,
                "run_id": "a4f1c2d3-1234-4567-89ab-cdef01234567",
                "observed_at": "2026-07-23T09:14:00Z",
            }
        }
    )


def test_provider_source_with_null_run_id() -> None:
    _validate_attributes(
        {
            "employee_count": {
                "value": 12,
                "source": "provider:apollo",
                "confidence": 1.0,
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
                    "source": "llm:enrich_company",
                    "confidence": 0.82,
                }
            }
        )


def test_invalid_source_kind_rejected() -> None:
    with pytest.raises(InvalidAttributeEnvelopeError):
        _validate_attributes(
            {
                "industry": {
                    "value": "B2B SaaS",
                    "source": "guess:whatever",
                    "confidence": 0.82,
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
                    "source": "llm:enrich_company",
                    "confidence": 1.5,
                    "run_id": None,
                    "observed_at": "2026-07-23T09:14:00Z",
                }
            }
        )
