"""Every file under schemas/ must be a valid JSON Schema (build-spec §8)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
SCHEMA_FILES = sorted(SCHEMAS_DIR.glob("**/*.json"))


@pytest.mark.parametrize("schema_path", SCHEMA_FILES, ids=lambda p: str(p.relative_to(SCHEMAS_DIR)))
def test_schema_file_is_valid_json_schema(schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text())
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)


def test_at_least_one_schema_file_was_found() -> None:
    assert SCHEMA_FILES, "No schema files found under schemas/ — the glob may be wrong"
