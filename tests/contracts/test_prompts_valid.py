"""Contract tests for prompts/*.md (build-spec §8.2, prompts/_conventions.md).

Pure file-reading — no database, no network, no API key. These are the
collection-time checks that catch a broken prompt file before a worker ever
tries to render it: a typo'd output_schema path, an invalid tier, an id that
doesn't match its own file path, or a {{variable}} nothing can ever supply.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from revenue_engine.core.llm import _FRONTMATTER_RE, _VARIABLE_RE, PromptTemplate

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPTS_DIR = _REPO_ROOT / "prompts"
_SCHEMAS_DIR = _REPO_ROOT / "schemas"
_VALID_TIERS = {"fast", "standard", "deep"}

_PROMPT_FILES = sorted(
    p for p in _PROMPTS_DIR.glob("**/*.md") if not p.name.startswith("_")
)  # excludes prompts/_conventions.md — documentation, not a renderable prompt

# ---------------------------------------------------------------------------
# Variable provenance allowlist (prompts/_conventions.md §4). Every
# {{variable}} across every prompt file must appear in exactly one of these
# two sets — a name in neither is a defect (typo, or a genuinely new variable
# nobody wired up yet); a name that could arguably belong in either is a
# design question, not something this test silently guesses at.
# ---------------------------------------------------------------------------

# Config-sourced: resolvable today from config/base.yaml + the loaded
# industry pack. The comment names the accessor a caller would use.
CONFIG_RESOLVABLE_VARIABLES = {
    "sender_persona": "pack.voice.sender_persona",
    "voice_rules": "pack.voice.as_prompt_text() (tone_rules + vocabulary say/avoid)",
    "icp_definition": "pack.icp",
    "objection_vocabulary": "pack.objection_categories",
    "commercial_boundaries": "pack.commercial_boundaries",
    "service_catalogue": "pack.service_catalogue",
    "scoring_guidance": "pack.scoring.guidance",
    "constraints": "pack.commercial_boundaries (never_state_autonomously + on_pricing_question)",
    "industry_pack_vocabulary": "pack.voice.vocabulary + pack.icp.firmographics.business_models",
    "step_intent": "pack.sequences.default.steps[N].intent, looked up by day/step",
}

# Runtime-supplied: per-call data an agent assembles from the database at
# call time. No agent code exists yet (M0.4 builds only complete_json() and
# its machinery) — this documents the expected source, it isn't backed by
# anything today. The comment names which future agent supplies it and from
# where, so this can't quietly become a dumping ground for typos.
RUNTIME_VARIABLES = {
    "company_enrichment": "leadgen.build_prospect_profile <- output of leadgen.enrich_company",
    "contact_enrichment": (
        "leadgen.build_prospect_profile <- output of leadgen.enrich_decision_maker"
    ),
    "raw_research": (
        "leadgen.enrich_company / enrich_decision_maker / build_prospect_profile "
        "<- scraped research text"
    ),
    "company_name": "leadgen.enrich_company <- companies.name",
    "domain": "leadgen.enrich_company <- companies.domain",
    "full_name": "leadgen.enrich_decision_maker <- contacts.full_name",
    "title": "leadgen.enrich_decision_maker <- contacts.title",
    "company_summary": "leadgen.enrich_decision_maker <- company_enrichment.positioning_summary",
    "prospect_profile": "qualification/sales prompts <- output of leadgen.build_prospect_profile",
    "engagement_summary": "qualification.score_lead <- aggregated activities/events for the lead",
    "reply_text": "sales.classify_reply <- inbound messages.body",
    "thread_history": "sales.classify_reply / draft_followup <- messages for the lead's thread",
    "account_brief": (
        "sales.draft_followup / draft_initial_outreach <- output of sales.research_account"
    ),
    "sequence_step": "sales.draft_followup <- sequence_runs.step",
    "conversation_history": "sales.draft_proposal <- messages for the deal",
    "meeting_insights": "sales.draft_proposal <- meeting_insights rows",
    "objection_text": "sales.handle_objection <- inbound messages.body",
    "retrieved_precedents": (
        "sales.handle_objection <- embeddings retrieval (kind=objection_precedent)"
    ),
    "similar_wins": "sales.research_account <- embeddings retrieval (kind=proof)",
    "campaign_angle": "sales.research_account <- campaigns/campaign_assets",
}


def _parse(path: Path) -> PromptTemplate:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    assert match is not None, f"{path}: missing YAML frontmatter"
    frontmatter = yaml.safe_load(match.group(1))
    return PromptTemplate(
        id=frontmatter["id"],
        tier=frontmatter["tier"],
        output_schema=frontmatter["output_schema"],
        max_tokens=frontmatter["max_tokens"],
        variables=tuple(frontmatter["variables"]),
        version=frontmatter["version"],
        body=match.group(2),
    )


def test_at_least_one_prompt_file_was_found():
    assert _PROMPT_FILES, "No prompt files found under prompts/ — the glob may be wrong"


@pytest.mark.parametrize(
    "prompt_path", _PROMPT_FILES, ids=lambda p: str(p.relative_to(_PROMPTS_DIR))
)
def test_frontmatter_id_matches_own_path(prompt_path: Path):
    template = _parse(prompt_path)
    expected_id = str(prompt_path.relative_to(_PROMPTS_DIR).with_suffix("")).replace("\\", "/")
    assert template.id == expected_id


@pytest.mark.parametrize(
    "prompt_path", _PROMPT_FILES, ids=lambda p: str(p.relative_to(_PROMPTS_DIR))
)
def test_frontmatter_tier_is_valid(prompt_path: Path):
    template = _parse(prompt_path)
    assert template.tier in _VALID_TIERS, (
        f"{prompt_path}: tier {template.tier!r} not one of {sorted(_VALID_TIERS)}"
    )


@pytest.mark.parametrize(
    "prompt_path", _PROMPT_FILES, ids=lambda p: str(p.relative_to(_PROMPTS_DIR))
)
def test_frontmatter_output_schema_exists(prompt_path: Path):
    template = _parse(prompt_path)
    schema_path = _SCHEMAS_DIR / template.output_schema
    assert schema_path.is_file(), (
        f"{prompt_path}: output_schema {template.output_schema!r} not found"
    )


@pytest.mark.parametrize(
    "prompt_path", _PROMPT_FILES, ids=lambda p: str(p.relative_to(_PROMPTS_DIR))
)
def test_frontmatter_tier_never_names_a_literal_model(prompt_path: Path):
    """Belt-and-braces: tier must be the bare word, never something that
    looks like a model id smuggled into the tier field."""
    template = _parse(prompt_path)
    assert not re.search(r"claude-", template.tier), (
        f"{prompt_path}: tier {template.tier!r} looks like a hardcoded model name"
    )


def test_pack_supplies_all_variables():
    """Every {{variable}} referenced in every prompt body is either
    config-resolvable or a documented runtime variable — never neither. This
    is the CI-time check for a variable nobody wired up, per
    prompts/_conventions.md §4."""
    known = set(CONFIG_RESOLVABLE_VARIABLES) | set(RUNTIME_VARIABLES)
    overlap = set(CONFIG_RESOLVABLE_VARIABLES) & set(RUNTIME_VARIABLES)
    assert not overlap, f"variables listed in both buckets: {overlap}"

    unaccounted: dict[str, set[str]] = {}
    for prompt_path in _PROMPT_FILES:
        template = _parse(prompt_path)
        used = set(_VARIABLE_RE.findall(template.body))
        missing = used - known
        if missing:
            unaccounted[str(prompt_path.relative_to(_PROMPTS_DIR))] = missing

    assert not unaccounted, f"variables not in either bucket: {unaccounted}"


def test_frontmatter_declared_variables_are_all_accounted_for():
    """The frontmatter `variables:` list itself (not just what the body
    happens to reference) must also be fully covered — catches a declared
    variable that was renamed in the body but not in the frontmatter list."""
    known = set(CONFIG_RESOLVABLE_VARIABLES) | set(RUNTIME_VARIABLES)
    for prompt_path in _PROMPT_FILES:
        template = _parse(prompt_path)
        missing = set(template.variables) - known
        assert not missing, f"{prompt_path}: declared variables not in either bucket: {missing}"
