"""The only way anything calls a model (CLAUDE.md §1 non-negotiable 2).

`complete_json()`: reads a prompt file, renders its `{{variables}}`, resolves
its tier to a model via core/config.py, calls the model with the output
schema passed through the Anthropic API's native structured-output mechanism
(`output_config.format` — constrained generation, not just a request phrased
nicely), validates the response against that JSON Schema plus the V1-V9
cross-field checks (docs/phase1-llm-boundary.md §4), retries once on any
failure feeding both the error *and* the schema back into the request, and on
a second failure raises `LLMValidationError` and emits `llm.validation_failed`
with nothing partial written. Every call — success or failure — is recorded
to `agent_runs` and traced through core/observability.py.

Structured outputs (docs/decisions.md, 2026-08-13/2026-08-14 entries): the
model is never shown only prose describing the schema — it is constrained to
it directly, so the class of bug where a model invents a plausible-but-wrong
enum value or field name is prevented at generation time, not just caught
after the fact. `schemas/outputs/*.json` remains the single source of truth;
prompt bodies must never duplicate the schema in prose (prompts/_conventions.md).
Structured outputs enforces shape and enum membership *only* — the API
itself rejects numeric/length/pattern bounds if present in the request
schema (`_STRUCTURED_OUTPUT_UNSUPPORTED_KEYWORDS`), so this module's own
post-call jsonschema validation is still the only thing enforcing `minimum`/
`maximum`/`maxLength`/`pattern`/etc., and the V1-V9 + retry path is very much
still live, not dead code superseded by this feature.

No prompt text lives here (CLAUDE.md §1 non-negotiable 3) — this module only
parses and renders prompt *files*; the words themselves are all under
`prompts/`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import anthropic
import asyncpg
import jsonschema
import yaml

from ..db import repositories as repo
from ..db.models import AgentRunStatus, Tier
from . import events as core_events
from . import observability
from .config import get_config
from .errors import LLMValidationError, PromptRenderError, RevenueEngineError
from .observability import TraceContext

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"
_SCHEMAS_DIR = _REPO_ROOT / "schemas"

_MAX_ATTEMPTS = 2  # one call, one retry — never more (docs/phase1-llm-boundary.md §4)

__all__ = ["complete_json", "TraceContext", "PromptTemplate"]


# ============================================================================
# Anthropic client — injectable for tests (docs/decisions.md: same pattern
# as core/events.py::emit()'s expanded signature)
# ============================================================================


class AnthropicClientProtocol(Protocol):
    """Only `.messages` is used. Typed as `Any` deliberately — this is an
    external SDK boundary; a stub test double only needs a `.messages.create`
    coroutine, not the real client's full structural shape. Declared as a
    read-only property (not a plain attribute) because the real SDK's
    `AsyncAnthropic.messages` is itself read-only — mypy --strict treats a
    plain attribute as requiring read/write access, which the real client
    doesn't structurally satisfy."""

    @property
    def messages(self) -> Any: ...


@cache
def _default_anthropic_client() -> AnthropicClientProtocol:
    """Reads ANTHROPIC_API_KEY from the environment (the SDK's own default).
    Applies llm.timeout_s and llm.max_client_retries from config/base.yaml so
    requests time out cleanly instead of hanging, and transient transport
    failures are retried without burning a validation-retry slot in agent_runs.
    Only ever called when no `client` is injected — every unit/contract test
    injects a stub and never reaches this path."""
    cfg = get_config()
    return anthropic.AsyncAnthropic(
        timeout=cfg.llm.timeout_s,
        max_retries=cfg.llm.max_client_retries,
    )


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts)


def _extract_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
    )


# ============================================================================
# Prompt file parsing (prompts/_conventions.md)
# ============================================================================


@dataclass(frozen=True)
class PromptTemplate:
    id: str
    tier: str
    output_schema: str
    max_tokens: int
    variables: tuple[str, ...]
    version: int
    body: str


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


@cache
def _load_prompt(prompt_path: str) -> PromptTemplate:
    """`prompt_path` is relative to `prompts/`, e.g. `"sales/classify_reply.md"`.
    Cached: prompt files don't change at runtime, and every complete_json()
    call would otherwise re-read and re-parse the same file from disk."""
    path = _PROMPTS_DIR / prompt_path
    if not path.is_file():
        raise RevenueEngineError(f"Prompt file not found: {path}")

    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise RevenueEngineError(
            f"{path}: missing YAML frontmatter (expected a leading --- ... --- block)"
        )
    raw_frontmatter, body = match.group(1), match.group(2)
    frontmatter = yaml.safe_load(raw_frontmatter)

    try:
        template = PromptTemplate(
            id=frontmatter["id"],
            tier=frontmatter["tier"],
            output_schema=frontmatter["output_schema"],
            max_tokens=frontmatter["max_tokens"],
            variables=tuple(frontmatter["variables"]),
            version=frontmatter["version"],
            body=body.strip(),
        )
    except (KeyError, TypeError) as exc:
        raise RevenueEngineError(f"{path}: missing required frontmatter key: {exc}") from exc

    expected_id = prompt_path[: -len(".md")] if prompt_path.endswith(".md") else prompt_path
    if template.id != expected_id:
        raise RevenueEngineError(
            f"{path}: frontmatter id {template.id!r} does not match its own path "
            f"(expected {expected_id!r})"
        )
    return template


# ============================================================================
# Template rendering — fails loudly on any unsupplied {{variable}}
# ============================================================================

_VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _render(body: str, variables: dict[str, Any], prompt_id: str) -> str:
    used = set(_VARIABLE_RE.findall(body))
    missing = sorted(used - variables.keys())
    if missing:
        raise PromptRenderError(prompt_id, missing)

    def _sub(match: re.Match[str]) -> str:
        value = variables[match.group(1)]
        return value if isinstance(value, str) else json.dumps(value, indent=2)

    return _VARIABLE_RE.sub(_sub, body)


# ============================================================================
# Response parsing + JSON Schema validation
# ============================================================================

_CODE_FENCE_RE = re.compile(r"\A```(?:json)?\s*\n(.*?)\n```\s*\Z", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Defensive: prompts instruct "no prose outside JSON", but a fenced
    ```json ... ``` block is a common enough model habit that stripping one
    costs nothing and avoids a spurious schema failure over formatting."""
    match = _CODE_FENCE_RE.match(text.strip())
    return match.group(1) if match else text


@cache
def _load_output_schema(output_schema: str) -> dict[str, Any]:
    path = _SCHEMAS_DIR / output_schema
    if not path.is_file():
        raise RevenueEngineError(f"Unknown output schema: {output_schema} (no {path})")
    schema = json.loads(path.read_text())
    if not isinstance(schema, dict):
        raise RevenueEngineError(f"{path}: expected a JSON object at the top level")
    return schema


@cache
def _load_output_validator(output_schema: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_load_output_schema(output_schema))


# Keywords the Anthropic structured-output API rejects outright with a 400 if
# present anywhere in output_config.format.schema. Confirmed by a real 400
# response, not inferred from docs alone — an earlier version of this
# function trusted the docs' claim that the SDK "automatically strips
# unsupported constraints" and shipped with none of these stripped; the
# actual API call returned:
#   "output_config.format.schema: For 'number' type, properties maximum,
#    minimum are not supported"
#   "output_config.format.schema: For 'array' type, property 'maxItems' is
#    not supported"
# — proving that claim doesn't hold for a raw dict passed via output_config
# (whatever automatic transformation the docs describe evidently applies to
# some other call path). See docs/decisions.md, 2026-08-14 correction.
# Deliberately conservative: every numeric/string/array bounding keyword
# encountered in this codebase's schemas, plus documented neighbours not
# currently in use (exclusiveMinimum/Maximum, multipleOf, uniqueItems) —
# a keyword this module doesn't yet use but a future schema adds is still
# covered without needing to remember to extend this list.
_STRUCTURED_OUTPUT_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "uniqueItems",
    }
)


def _to_structured_output_schema(node: Any) -> Any:
    """Transform for the Anthropic structured-output API
    (`output_config.format.schema`, https://platform.claude.com/docs/en/
    build-with-claude/structured-outputs, verified 2026-08-13, corrected
    2026-08-14 after a real 400 response). Two transforms, applied
    recursively:

    1. Every key in `_STRUCTURED_OUTPUT_UNSUPPORTED_KEYWORDS` is dropped —
       the API rejects the request outright if present.
    2. A JSON Schema `enum` that includes `null` is rewritten as `anyOf`
       with a `{"type": "null"}` branch — also explicitly unsupported
       ("use anyOf with {"type": "null"} instead"), and several of
       schemas/outputs/*.json use exactly that pattern for inferred-value
       fields (e.g. company_enrichment.json's `business_model`,
       contact_enrichment.json's `seniority`, reply_classification.json's
       `objection_category`).

    CRITICAL: `schemas/outputs/*.json` is never touched by this function —
    it always operates on a derived, in-memory copy. `_load_output_schema`'s
    return value (and therefore `_load_output_validator` and
    `_validate_json_response`) keeps every constraint this function strips.
    Structured outputs guarantees shape and enum membership at generation
    time; every bound (`confidence` in [0, 1], `maxItems` on
    `personalization_anchors`, `maxLength` on an outreach body, `pattern` on
    an `anchor_id`, ...) is enforced *only* by this module's own post-call
    jsonschema validation, same as before this feature existed — the retry
    path is very much still live for those failures, not dead code.
    """
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in _STRUCTURED_OUTPUT_UNSUPPORTED_KEYWORDS:
                continue
            if key == "enum" and isinstance(value, list) and None in value:
                result["anyOf"] = [
                    {"enum": [v for v in value if v is not None]},
                    {"type": "null"},
                ]
            else:
                result[key] = _to_structured_output_schema(value)
        return result
    if isinstance(node, list):
        return [_to_structured_output_schema(item) for item in node]
    return node


@cache
def _load_output_config(output_schema: str) -> dict[str, Any]:
    """The full `output_config` request parameter for one output schema —
    `{"format": {"type": "json_schema", "schema": ...}}`, per
    OutputConfigParam/JSONOutputFormatParam in the installed `anthropic` SDK
    (introspected via `inspect`, not assumed — see docs/decisions.md,
    2026-08-13). Cached like every other schemas/prompts/ read in this
    module, since the transform is pure and the schema files don't change at
    runtime."""
    return {
        "format": {
            "type": "json_schema",
            "schema": _to_structured_output_schema(_load_output_schema(output_schema)),
        }
    }


def _validate_json_response(
    raw_text: str, validator: jsonschema.Draft202012Validator, output_schema: str
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        candidate = json.loads(_strip_code_fence(raw_text))
    except json.JSONDecodeError as exc:
        return None, [f"response is not valid JSON: {exc}"]
    if not isinstance(candidate, dict):
        return None, ["response is valid JSON but not a JSON object"]
    _apply_injecting_transforms(output_schema, candidate)
    errors = [e.message for e in validator.iter_errors(candidate)]
    return (candidate if not errors else None), errors


# ============================================================================
# V1-V9 cross-field validators (docs/phase1-llm-boundary.md §4)
#
# Two kinds, applied in this order once JSON Schema validation passes:
#   - correcting: mutate `output` in place, log a warning, never fail the call
#     (V4, V5, V8)
#   - rejecting: return error strings; any error is treated exactly like a
#     schema failure — fed into the same retry-once-then-fail path
#     (V1, V2, V3, V6, V7, V9)
# ============================================================================

logger = logging.getLogger(__name__)


def _anchor_ids_from_variables(variables: dict[str, Any]) -> set[str]:
    """V1 and V3 both validate against the anchors on the `prospect_profile`
    variable — the schema's own description: "personalization_anchors are
    the ONLY facts downstream outreach may assert" (prospect_profile.json).
    Requires prospect_profile to be passed as a structured dict, not a
    pre-stringified value."""
    profile = variables.get("prospect_profile")
    if not isinstance(profile, dict):
        return set()
    anchors = profile.get("personalization_anchors", [])
    if not isinstance(anchors, list):
        return set()
    return {a["anchor_id"] for a in anchors if isinstance(a, dict) and "anchor_id" in a}


def _v1_facts_asserted_anchors_exist(
    output: dict[str, Any], variables: dict[str, Any]
) -> list[str]:
    known = _anchor_ids_from_variables(variables)
    errors = []
    for fact in output.get("facts_asserted", []) or []:
        anchor_id = fact.get("anchor_id") if isinstance(fact, dict) else None
        if anchor_id not in known:
            errors.append(
                f"facts_asserted anchor_id {anchor_id!r} not found in "
                "prospect_profile.personalization_anchors"
            )
    return errors


def _word_count(text: str) -> int:
    """Tokenisation rule, applied identically wherever a word count matters
    (word-count injection, V2, and their tests): split on whitespace, then
    keep only tokens containing at least one alphanumeric character (drops
    pure-punctuation tokens like a lone em-dash). A hyphenated word
    ("follow-up") or a contraction ("don't") is one token — the
    hyphen/apostrophe is interior, not a separator — and is counted as one
    word."""
    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


# Token-based models cannot reliably count their own words (docs/decisions.md,
# 2026-08-14: golden run — a genuinely fine draft was rejected over a 6-word
# self-report discrepancy, burning a retry on a non-defect). word_count is no
# longer part of what the model is asked for at all (dropped from
# schemas/outputs/outreach_draft.json's `required`, and from
# draft_initial_outreach.md's rules) — it is a derivable fact computed from
# `body`, injected by complete_json() before schema validation ever runs
# (agent-contracts.md §0.2: derivable facts are computed, not asked for).
def _inject_word_count(candidate: dict[str, Any]) -> None:
    body = candidate.get("body")
    candidate["word_count"] = _word_count(body) if isinstance(body, str) else 0


InjectingTransform = Callable[[dict[str, Any]], None]

_INJECTING_TRANSFORMS: dict[str, list[InjectingTransform]] = {
    "outputs/outreach_draft.json": [_inject_word_count],
}


def _apply_injecting_transforms(output_schema: str, candidate: dict[str, Any]) -> None:
    """Runs BEFORE schema validation — unlike the correcting/rejecting
    V-hooks below, which only ever see a candidate that already passed the
    JSON Schema (word_count wouldn't exist yet for validation to check if it
    weren't injected first)."""
    for transform in _INJECTING_TRANSFORMS.get(output_schema, []):
        transform(candidate)


# draft_initial_outreach.md Rule 3: "Under 120 words in the body." One
# deliberate, explicitly-flagged duplication of a number that also lives in
# that prompt's prose (docs/decisions.md, 2026-08-14) — CLAUDE.md §1
# non-negotiable 3 is about prompt WORDING never living in Python, not about
# a schema-adjacent numeric limit also being asserted in code; unlike the
# enum/field-name duplication the structured-output fix eliminated, there is
# no single machine-readable source this number could instead be read from
# without inventing one. schemas/outputs/outreach_draft.json's own
# word_count bounds (20-220) stay a generic, shared sanity range on purpose
# — this constant is the real, tighter business rule. draft_followup.md has
# no fixed number of its own ("shorter than the previous message" is
# relative), so this same ceiling applies there too: a followup can never
# legitimately need to exceed what the initial outreach was already capped
# at.
_OUTREACH_MAX_WORDS = 120


def _v2_word_count_within_limit(output: dict[str, Any], variables: dict[str, Any]) -> list[str]:
    """word_count is now always code-injected from body's real word count
    (_inject_word_count, applied before schema validation) — it can no
    longer diverge from the body by construction, so there is nothing left
    to "match" against a self-report. V2's job changes to enforcing the
    actual constraint the self-report was only ever a proxy for: is the
    draft short enough. Schema validation alone can't own this number
    (word_count's own bounds are deliberately generic, see
    _OUTREACH_MAX_WORDS above), so it stays a cross-field check here."""
    word_count = output.get("word_count")
    if isinstance(word_count, int) and word_count > _OUTREACH_MAX_WORDS:
        return [f"word_count={word_count} exceeds the {_OUTREACH_MAX_WORDS}-word limit"]
    return []


def _v3_supporting_anchors_exist(output: dict[str, Any], variables: dict[str, Any]) -> list[str]:
    known = _anchor_ids_from_variables(variables)
    errors = []
    for anchor_id in output.get("supporting_anchors", []) or []:
        if anchor_id not in known:
            errors.append(
                f"supporting_anchors id {anchor_id!r} not found in "
                "prospect_profile.personalization_anchors"
            )
    return errors


# Matches an explicit currency symbol/code next to a number, or a bare number
# immediately followed by a currency word/abbreviation — deliberately not a
# bare "\d+" (that would flag "3-6 weeks" or "50 people"). Money is numeric
# in this codebase (CLAUDE.md §1 non-negotiable 12); this regex is the one
# place a *model's own text* is scanned for it, precisely because that text
# is not trusted (V4's whole point).
_CURRENCY_RE = re.compile(
    r"(?:[$£€]\s?\d[\d,]*(?:\.\d+)?)"
    r"|(?:\b\d[\d,]*(?:\.\d+)?\s?(?:usd|eur|gbp|dollars|k\b))",
    re.IGNORECASE,
)


def _proposal_draft_text(output: dict[str, Any]) -> str:
    parts = [
        str(output.get("title", "")),
        str(output.get("problem_statement", "")),
    ]
    for scope_item in output.get("proposed_scope", []) or []:
        if isinstance(scope_item, dict):
            parts.append(f"{scope_item.get('item', '')} {scope_item.get('outcome', '')}")
    parts.extend(str(s) for s in output.get("out_of_scope", []) or [])
    parts.extend(str(s) for s in output.get("assumptions", []) or [])
    for phase in output.get("timeline", []) or []:
        if isinstance(phase, dict):
            parts.append(f"{phase.get('phase', '')} {phase.get('duration', '')}")
    parts.extend(str(s) for s in output.get("pricing_placeholders", []) or [])
    return " ".join(parts)


def _v4_pricing_present_honest(output: dict[str, Any], variables: dict[str, Any]) -> None:
    """proposal_draft.json has no `requires_approval` field (additionalProperties:
    false) — every proposal is "Always approval-gated (A2)" per the schema's
    own description, and `pricing_present` is the flag that gate actually
    reads. "force requires_approval" (docs/phase1-llm-boundary.md §4) is
    therefore implemented as forcing `pricing_present: true`, not as adding a
    field that would break schema validation — see docs/decisions.md."""
    if output.get("pricing_present"):
        return
    if _CURRENCY_RE.search(_proposal_draft_text(output)):
        logger.warning(
            "V4: model reported pricing_present=false but a currency/number pattern "
            "was found in the draft text; forcing pricing_present=true (honesty violation)"
        )
        output["pricing_present"] = True


def _extract_precedent_ids(retrieved: Any) -> set[str]:
    if not isinstance(retrieved, list):
        return set()
    ids: set[str] = set()
    for item in retrieved:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.add(item["id"])
    return ids


def _v5_precedents_used_subset(output: dict[str, Any], variables: dict[str, Any]) -> None:
    retrieved_ids = _extract_precedent_ids(variables.get("retrieved_precedents"))
    used = output.get("precedents_used", []) or []
    stripped = [p for p in used if p not in retrieved_ids]
    if stripped:
        logger.warning(
            "V5: stripping precedents_used ids not in retrieved_precedents: %s", stripped
        )
        output["precedents_used"] = [p for p in used if p in retrieved_ids]


def _v6_subscore_evidence_nonempty(output: dict[str, Any], variables: dict[str, Any]) -> list[str]:
    errors = []
    for field in ("buying_intent", "seniority_fit", "narrative_fit"):
        sub = output.get(field)
        if not isinstance(sub, dict):
            continue
        score = sub.get("score")
        evidence = sub.get("evidence") or []
        if isinstance(score, int | float) and score > 0.2 and not evidence:
            errors.append(f"{field}.score={score} > 0.2 requires non-empty evidence")
    return errors


def _v7_objection_category_only_if_objection(
    output: dict[str, Any], variables: dict[str, Any]
) -> list[str]:
    category = output.get("objection_category")
    intent = output.get("intent")
    if category is not None and intent != "objection":
        return [
            f"objection_category={category!r} set but intent={intent!r} "
            "(must be null unless intent == 'objection')"
        ]
    return []


def _v8_resume_date_only_if_not_now(output: dict[str, Any], variables: dict[str, Any]) -> None:
    intent = output.get("intent")
    if output.get("requested_resume_date") is not None and intent != "not_now":
        logger.warning(
            "V8: requested_resume_date set but intent=%r (!= 'not_now'); nulling it", intent
        )
        output["requested_resume_date"] = None


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _v9_no_email_regex(output: dict[str, Any], variables: dict[str, Any]) -> list[str]:
    if _EMAIL_RE.search(json.dumps(output)):
        return [
            "output contains an email-address-shaped string; contact_enrichment "
            "must never generate a contact identifier"
        ]
    return []


RejectingValidator = Callable[[dict[str, Any], dict[str, Any]], list[str]]
CorrectingValidator = Callable[[dict[str, Any], dict[str, Any]], None]

_REJECTING_VALIDATORS: dict[str, list[RejectingValidator]] = {
    "outputs/outreach_draft.json": [_v1_facts_asserted_anchors_exist, _v2_word_count_within_limit],
    "outputs/account_brief.json": [_v3_supporting_anchors_exist],
    "outputs/lead_subscores.json": [_v6_subscore_evidence_nonempty],
    "outputs/reply_classification.json": [_v7_objection_category_only_if_objection],
    "outputs/contact_enrichment.json": [_v9_no_email_regex],
}

_CORRECTING_VALIDATORS: dict[str, list[CorrectingValidator]] = {
    "outputs/proposal_draft.json": [_v4_pricing_present_honest],
    "outputs/objection_response.json": [_v5_precedents_used_subset],
    "outputs/reply_classification.json": [_v8_resume_date_only_if_not_now],
}


def _apply_correcting_validators(
    output_schema: str, output: dict[str, Any], variables: dict[str, Any]
) -> None:
    for validator in _CORRECTING_VALIDATORS.get(output_schema, []):
        validator(output, variables)


def _apply_rejecting_validators(
    output_schema: str, output: dict[str, Any], variables: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    for validator in _REJECTING_VALIDATORS.get(output_schema, []):
        errors.extend(validator(output, variables))
    return errors


# ============================================================================
# complete_json()
# ============================================================================


async def complete_json(
    prompt_path: str,
    variables: dict[str, Any],
    output_schema: str,
    trace: TraceContext | None,
    *,
    tier: Tier | None = None,
    conn: asyncpg.Connection,
    correlation_id: UUID,
    actor: str,
    causation_id: UUID | None = None,
    client: AnthropicClientProtocol | None = None,
) -> dict[str, Any]:
    """Signature expands on the literal one you gave (`conn`, `correlation_id`,
    `actor`, `causation_id`, `client` added) — same pattern as
    core/events.py::emit(), logged in docs/decisions.md. `conn`/`correlation_id`/
    `actor` are required keyword-only, not defaulted: agent_runs and a possible
    llm.validation_failed emission both need them, and getting either wrong
    silently breaks traceability the same way an unpropagated correlation_id
    does in emit().

    - `tier=None` uses the prompt frontmatter's own tier.
    - `conn`/`causation_id`: `causation_id` doubles as `agent_runs.trigger_event`
      — both express "the event that caused this call", so one caller-supplied
      value backs both.
    - `client=None` builds a real `anthropic.AsyncAnthropic()` from
      ANTHROPIC_API_KEY. Tests inject a stub — this path needs no API key.

    Control flow: render -> call -> validate (schema, then V-hooks) -> on any
    failure, retry once with the error fed back as a corrective user turn ->
    on a second failure, write the failed agent_runs row, emit
    llm.validation_failed, raise LLMValidationError. Nothing partial is ever
    returned or written on failure.
    """
    template = _load_prompt(prompt_path)
    if template.output_schema != output_schema:
        raise RevenueEngineError(
            f"{prompt_path}: frontmatter output_schema {template.output_schema!r} does not "
            f"match the output_schema argument {output_schema!r}"
        )

    try:
        resolved_tier = tier or Tier(template.tier)
    except ValueError as exc:
        raise RevenueEngineError(f"{prompt_path}: invalid tier {template.tier!r}") from exc

    config = get_config()
    model = config.model_for(resolved_tier)
    rendered_body = _render(template.body, variables, template.id)
    resolved_client = client if client is not None else _default_anthropic_client()
    raw_schema = _load_output_schema(output_schema)
    validator = _load_output_validator(output_schema)
    output_config = _load_output_config(output_schema)

    messages: list[dict[str, str]] = [{"role": "user", "content": rendered_body}]

    start = time.monotonic()
    parsed: dict[str, Any] | None = None
    last_errors: list[str] = []
    input_tokens = 0
    output_tokens = 0
    attempt = 0

    while attempt < _MAX_ATTEMPTS:
        attempt += 1
        response = await resolved_client.messages.create(
            model=model,
            max_tokens=template.max_tokens,
            messages=messages,
            output_config=output_config,
        )
        raw_text = _extract_text(response)
        in_tok, out_tok = _extract_usage(response)
        input_tokens += in_tok
        output_tokens += out_tok

        candidate, errors = _validate_json_response(raw_text, validator, output_schema)
        if candidate is not None:
            _apply_correcting_validators(output_schema, candidate, variables)
            rejecting_errors = _apply_rejecting_validators(output_schema, candidate, variables)
            if not rejecting_errors:
                parsed = candidate
                break
            errors = rejecting_errors

        last_errors = errors
        if attempt < _MAX_ATTEMPTS:
            messages.append({"role": "assistant", "content": raw_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous response failed validation:\n"
                        + "\n".join(f"- {e}" for e in errors)
                        + "\n\nThe response must match this JSON Schema exactly — field "
                        "names and enum values must match precisely, never an invented "
                        "alternative:\n"
                        + json.dumps(raw_schema, indent=2)
                        + "\n\nReturn corrected JSON matching the schema. No prose outside JSON."
                    ),
                }
            )

    latency_ms = int((time.monotonic() - start) * 1000)
    cost = config.compute_cost(
        resolved_tier, input_tokens=input_tokens, output_tokens=output_tokens
    )
    retry_count = attempt - 1
    trace_id = trace.trace_id if trace is not None else None

    if parsed is None:
        run = await repo.insert_agent_run(
            conn,
            agent=actor,
            trigger_event=causation_id,
            trace_id=trace_id,
            prompt_id=template.id,
            prompt_version=template.version,
            tier=resolved_tier,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            latency_ms=latency_ms,
            status=AgentRunStatus.FAILED,
            error="; ".join(last_errors),
            retry_count=retry_count,
        )
        observability.record_span(
            trace,
            name=template.id,
            prompt_id=template.id,
            prompt_version=template.version,
            tier=resolved_tier.value,
            model=model,
            cost=cost,
            latency_ms=latency_ms,
            status="failed",
            retry_count=retry_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error="; ".join(last_errors),
        )
        await core_events.emit(
            conn,
            type="llm.validation_failed",
            payload={
                "prompt_id": template.id,
                "prompt_version": template.version,
                "run_id": str(run.id),
                "errors": last_errors,
            },
            correlation_id=correlation_id,
            actor=actor,
            idempotency_key=f"agent_run:{run.id}:validation_failed",
            causation_id=causation_id,
        )
        raise LLMValidationError(template.id, template.version, last_errors)

    await repo.insert_agent_run(
        conn,
        agent=actor,
        trigger_event=causation_id,
        trace_id=trace_id,
        prompt_id=template.id,
        prompt_version=template.version,
        tier=resolved_tier,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=cost,
        latency_ms=latency_ms,
        status=AgentRunStatus.SUCCESS,
        error=None,
        retry_count=retry_count,
    )
    observability.record_span(
        trace,
        name=template.id,
        prompt_id=template.id,
        prompt_version=template.version,
        tier=resolved_tier.value,
        model=model,
        cost=cost,
        latency_ms=latency_ms,
        status="success",
        retry_count=retry_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=None,
    )
    return parsed
