"""Golden test for the leadgen enrichment chain on genuinely sparse,
CSV-only input (M1.1 NOT VERIFIED gap, docs/verification-loop.md).

Every other golden test in this directory hand-writes a fixture JSON of
prompt variables. None of them start from an actual CSV row parsed by
ManualCsvProvider, and no test anywhere had run build_prospect_profile
against a real model with only what a hand-built CSV row actually contains.
build_prospect_profile is the call that produces `personalization_anchors` —
the only facts sales/draft_initial_outreach.md is permitted to assert
(prompts/leadgen/build_prospect_profile.md's own "Treat them as a contract").
If it fabricates an anchor from thin input, every downstream email inherits
an invented fact.

This drives the real chain: leadgen_sparse.csv -> ManualCsvProvider ->
enrich_company -> enrich_decision_maker -> build_prospect_profile, three
real complete_json() calls, using the exact variable-building helpers
agents/leadgen.py::handle_enrich uses in production (_raw_research_text,
_vocabulary_text, _icp_definition_text, _contact_display_name — imported
directly, same pattern as tests/golden/test_contact_enrichment.py importing
core/llm.py's private `_EMAIL_RE`). handle_enrich itself is not called: it
writes enrichment through repo.upsert_company/upsert_contact, which drop any
field below min_confidence_to_store — exactly the null, low-confidence
fields this test needs to inspect. Calling complete_json() directly, as
every sibling golden test does, is what makes the model's actual output
visible to assert on.

@pytest.mark.golden — excluded by default, run via `make golden`. Calls the
real Anthropic API (ANTHROPIC_API_KEY required) and writes to
TEST_DATABASE_URL through the real complete_json() path.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from revenue_engine.agents.leadgen import (
    _contact_display_name,
    _icp_definition_text,
    _raw_research_text,
    _vocabulary_text,
)
from revenue_engine.core import llm
from revenue_engine.core.config import get_config
from revenue_engine.core.observability import TraceContext
from revenue_engine.db.models import Company, Contact, EmailStatus
from revenue_engine.integrations.prospecting import DiscoveryFilters, ManualCsvProvider

pytestmark = pytest.mark.golden

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
_CSV_PATH = _FIXTURES_DIR / "leadgen_sparse.csv"


async def _company_and_contact_from_csv() -> tuple[Company, Contact]:
    """Parses leadgen_sparse.csv through the real ManualCsvProvider, then
    wraps the resulting stubs in db/models.py's Company/Contact so
    agents/leadgen.py's own text-building helpers (which take those types,
    not CompanyStub/ContactStub) can be called unmodified. Only the
    id/timestamp fields are fabricated here — those aren't in the CSV and
    aren't read by anything this test exercises."""
    provider = ManualCsvProvider(_CSV_PATH)
    assert provider.errors == [], (
        f"fixture CSV should parse with no rejected rows: {provider.errors}"
    )

    companies = await provider.discover_companies(DiscoveryFilters(), limit=10)
    assert len(companies) == 1, "fixture CSV should yield exactly one company"
    company_stub = companies[0]

    contacts = await provider.find_contacts(company_stub, target_titles=[], limit=10)
    assert len(contacts) == 1, "fixture CSV should yield exactly one contact"
    contact_stub = contacts[0]

    assert contact_stub.title is None, "fixture must leave contact_title genuinely blank"

    now = datetime.now(UTC)
    company = Company(
        id=uuid.uuid4(),
        name=company_stub.name,
        domain=company_stub.domain,
        linkedin_url=company_stub.linkedin_url,
        country=None,
        employee_band=None,
        attributes={},
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )

    # Mirrors scripts/import_leads.py::_import_row's own construction of
    # contact_attributes["import_note"] from contact_stub.source_note.
    contact_attributes: dict = {}
    if contact_stub.source_note:
        contact_attributes = {
            "import_note": {
                "value": contact_stub.source_note,
                "confidence": 1.0,
                "evidence": "",
                "source": "human:manual_import",
                "run_id": None,
                "observed_at": now.isoformat(),
            }
        }

    contact = Contact(
        id=uuid.uuid4(),
        email=contact_stub.email,
        email_status=EmailStatus.UNVERIFIED,
        full_name=None,
        first_name=contact_stub.first_name,
        last_name=contact_stub.last_name,
        title=contact_stub.title,
        linkedin_url=contact_stub.linkedin_url,
        company_id=company.id,
        attributes=contact_attributes,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    return company, contact


async def test_sparse_csv_row_yields_no_fabricated_facts(conn: asyncpg.Connection):
    company, contact = await _company_and_contact_from_csv()
    pack = get_config().pack
    raw_research = _raw_research_text(company=company, contact=contact)
    correlation_id = uuid.uuid4()

    company_enrichment = await llm.complete_json(
        "leadgen/enrich_company.md",
        {
            "company_name": company.name,
            "domain": company.domain or "",
            "raw_research": raw_research,
            "industry_pack_vocabulary": _vocabulary_text(pack),
        },
        "outputs/company_enrichment.json",
        TraceContext(trace_id="golden-leadgen-sparse-company", correlation_id=correlation_id),
        conn=conn,
        correlation_id=correlation_id,
        actor="golden-test",
    )

    # A CSV row alone (company name + domain, no research) is the sparse
    # case docs/phase1-llm-boundary.md §2 exists for: nulls, not an invented
    # industry or employee band.
    assert company_enrichment["insufficient_context"] is True
    for field in ("industry", "sub_industry", "business_model", "employee_band"):
        assert company_enrichment[field]["value"] is None, (
            f"company_enrichment.{field} should be null on a bare CSV row, not invented"
        )

    contact_enrichment = await llm.complete_json(
        "leadgen/enrich_decision_maker.md",
        {
            "full_name": _contact_display_name(contact),
            "title": contact.title or "",
            "company_summary": company.name,
            "raw_research": raw_research,
        },
        "outputs/contact_enrichment.json",
        TraceContext(trace_id="golden-leadgen-sparse-contact", correlation_id=correlation_id),
        conn=conn,
        correlation_id=correlation_id,
        actor="golden-test",
    )

    # No title was supplied (fixture leaves contact_title blank) — seniority
    # must not be forced into a confident guess from name/email alone.
    # Either insufficient_context is true, or the returned confidence is
    # visibly low; a confident seniority label here would be fabricated.
    if not contact_enrichment["insufficient_context"]:
        assert contact_enrichment["seniority"]["confidence"] < 0.5, (
            "seniority should not be confidently inferred with no title and no research"
        )

    # V9, exercised end-to-end against a real model response.
    assert not llm._EMAIL_RE.search(json.dumps(contact_enrichment))

    profile = await llm.complete_json(
        "leadgen/build_prospect_profile.md",
        {
            "company_enrichment": company_enrichment,
            "contact_enrichment": contact_enrichment,
            "icp_definition": _icp_definition_text(pack),
            "raw_research": raw_research,
        },
        "outputs/prospect_profile.json",
        TraceContext(trace_id="golden-leadgen-sparse-profile", correlation_id=correlation_id),
        conn=conn,
        correlation_id=correlation_id,
        actor="golden-test",
    )

    # The point of this test: a CSV row contains no verifiable public fact
    # to anchor on. An empty personalization_anchors array is the correct,
    # honest output (prompts/leadgen/build_prospect_profile.md rule 3) and
    # routes the account to manual research. Any anchor here is fabricated.
    assert profile["personalization_anchors"] == []
    assert profile["insufficient_context"] is True
