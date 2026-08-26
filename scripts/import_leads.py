"""Manual lead import (build-spec M1.1). Reads a CSV via
integrations/prospecting.py::ManualCsvProvider, creates companies/contacts/
leads through db/repositories.py, and emits `lead.captured` (or
`lead.deferred`) per surviving row — the normal Phase 1 pipeline picks up
from there via `orchestrator/router.py` + scripts/run_worker.py.

CSV contract (docs/decisions.md, M1.1):
    company_name, domain, contact_first_name, contact_last_name,
    contact_email, contact_title, linkedin_url, source_note

`domain` and `contact_email` are required; a row missing either is rejected
with a per-row error and the import continues — never fails the whole file
over one bad row.

Idempotent: re-importing the same CSV creates no new rows and no duplicate
events. Two guarantees combine to make this true under concurrency, not just
in the common single-process case:
  1. A pre-check (repositories.get_active_or_deferred_lead_by_contact) skips
     rows already in the pipeline — an optimisation, not the real guard.
  2. The real guard is structural: migrations/0001's single-thread indexes
     for the active case, migrations/0004's `one_deferred_lead_per_contact`
     for the deferred case. A caller that loses a race against (1) still
     gets a typed, idempotent outcome from repositories.create_lead()
     (DuplicateActiveLeadError, or a re-read deferred row) instead of a
     duplicate — handled below, not left to crash the row.

Usage: uv run python scripts/import_leads.py path/to/leads.csv
Env: DATABASE_URL (required).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from dotenv import load_dotenv

from revenue_engine.core import events as core_events
from revenue_engine.core.config import get_config
from revenue_engine.core.errors import DuplicateActiveLeadError
from revenue_engine.db import repositories as repo
from revenue_engine.db.models import Lead, LeadSource, LeadStatus
from revenue_engine.integrations.prospecting import (
    CompanyStub,
    ContactStub,
    DiscoveryFilters,
    ManualCsvProvider,
)

ACTOR = "script:import_leads"


@dataclass(frozen=True)
class RowResult:
    outcome: str  # imported | deferred | already_imported | already_deferred | rejected | failed
    detail: str | None = None


async def _import_row(
    conn: asyncpg.Connection,
    *,
    company_stub: CompanyStub,
    contact_stub: ContactStub,
    industry_pack: str,
) -> RowResult:
    company = await repo.upsert_company(conn, name=company_stub.name, domain=company_stub.domain)

    contact_attributes = None
    if contact_stub.source_note:
        contact_attributes = {
            "import_note": {
                "value": contact_stub.source_note,
                "confidence": 1.0,
                # Empty string, not null: schemas/entities/attribute.json
                # permits either for a human-entered note with no natural
                # quoted "snippet", but "" reads more honestly here than
                # null — null is reserved for "we don't know", not "there
                # is deliberately nothing to quote". Verified against the
                # live validator both ways (docs/decisions.md, M1.1).
                "evidence": "",
                "source": "human:manual_import",
                "run_id": None,
                "observed_at": datetime.now(UTC).isoformat(),
            }
        }
    contact = await repo.upsert_contact(
        conn,
        email=contact_stub.email,
        first_name=contact_stub.first_name,
        last_name=contact_stub.last_name,
        title=contact_stub.title,
        linkedin_url=contact_stub.linkedin_url,
        company_id=company.id,
        attributes=contact_attributes,
    )

    existing = await repo.get_active_or_deferred_lead_by_contact(conn, contact.id)
    if existing is not None:
        return await _reemit_existing(
            conn, existing, company_id=company.id, industry_pack=industry_pack
        )

    try:
        result = await repo.create_lead(
            conn,
            contact_id=contact.id,
            company_id=company.id,
            industry_pack=industry_pack,
            source=LeadSource.MANUAL_IMPORT,
        )
    except DuplicateActiveLeadError:
        # Lost a race: another concurrent import of this same CSV created
        # the contact's active lead between our pre-check and this call.
        # migrations/0001's one_active_lead_per_contact enforced this at the
        # DB level; re-fetch what the winner created and treat it the same
        # as an idempotent replay.
        winner = await repo.get_active_or_deferred_lead_by_contact(conn, contact.id)
        if winner is None:
            return RowResult(
                outcome="failed", detail="DuplicateActiveLeadError but no lead found on re-read"
            )
        return await _reemit_existing(
            conn, winner, company_id=company.id, industry_pack=industry_pack
        )

    if result.failed:
        return RowResult(outcome="failed", detail=result.error)

    if result.deferred:
        assert result.lead is not None
        await core_events.emit(
            conn,
            type="lead.deferred",
            payload={
                "lead_id": str(result.lead.id),
                "company_id": str(company.id),
                "blocked_by_lead_id": str(result.blocked_by_lead_id)
                if result.blocked_by_lead_id
                else None,
                "reason": "company_single_thread",
            },
            correlation_id=uuid4(),
            actor=ACTOR,
            idempotency_key=f"lead:{contact.id}:manual:deferred",
        )
        return RowResult(outcome="deferred")

    assert result.lead is not None
    await core_events.emit(
        conn,
        type="lead.captured",
        payload={
            "lead_id": str(result.lead.id),
            "contact_id": str(contact.id),
            "company_id": str(company.id),
            "campaign_id": None,
            "source": "manual_import",
            "industry_pack": industry_pack,
        },
        correlation_id=uuid4(),
        actor=ACTOR,
        idempotency_key=f"lead:{contact.id}:manual:captured",
    )
    return RowResult(outcome="imported")


async def _reemit_existing(
    conn: asyncpg.Connection, lead: Lead, *, company_id: UUID, industry_pack: str
) -> RowResult:
    """Re-import of a row already in the pipeline. Re-emits the event that
    row would have produced, with the SAME idempotency_key as the original —
    core/events.py::emit()'s own dedup (event-catalog.md §R3) makes this a
    true no-op: whatever payload is passed here is discarded in favour of the
    row that already exists, so approximating `blocked_by_lead_id` as null
    for the deferred case is harmless, not a data-quality compromise."""
    if lead.status == LeadStatus.DEFERRED:
        await core_events.emit(
            conn,
            type="lead.deferred",
            payload={
                "lead_id": str(lead.id),
                "company_id": str(company_id),
                "blocked_by_lead_id": None,
                "reason": "company_single_thread",
            },
            correlation_id=uuid4(),
            actor=ACTOR,
            idempotency_key=f"lead:{lead.contact_id}:manual:deferred",
        )
        return RowResult(outcome="already_deferred")

    await core_events.emit(
        conn,
        type="lead.captured",
        payload={
            "lead_id": str(lead.id),
            "contact_id": str(lead.contact_id),
            "company_id": str(company_id),
            "campaign_id": None,
            "source": "manual_import",
            "industry_pack": industry_pack,
        },
        correlation_id=uuid4(),
        actor=ACTOR,
        idempotency_key=f"lead:{lead.contact_id}:manual:captured",
    )
    return RowResult(outcome="already_imported")


def _print_summary(rejected_count: int, results: list[RowResult]) -> None:
    counts: dict[str, int] = {"rejected": rejected_count}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    total = rejected_count + len(results)
    print(f"{total} row(s) read")
    for outcome in (
        "imported",
        "deferred",
        "already_imported",
        "already_deferred",
        "rejected",
        "failed",
    ):
        if counts.get(outcome):
            print(f"  {outcome}: {counts[outcome]}")
    for r in results:
        if r.outcome == "failed":
            print(f"  FAILED: {r.detail}", file=sys.stderr)


async def run(csv_path: Path, *, industry_pack: str | None) -> int:
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    resolved_pack = industry_pack or get_config().pack.name

    try:
        provider = ManualCsvProvider(csv_path)
    except (OSError, ValueError) as exc:
        print(f"Could not read {csv_path}: {exc}", file=sys.stderr)
        return 1

    for error in provider.errors:
        print(f"  row {error.row_number} rejected: {error.reason}", file=sys.stderr)

    conn = await asyncpg.connect(database_url)
    results: list[RowResult] = []
    try:
        companies = await provider.discover_companies(DiscoveryFilters(), limit=1_000_000)
        for company_stub in companies:
            contacts = await provider.find_contacts(company_stub, target_titles=[], limit=1_000_000)
            for contact_stub in contacts:
                # Deliberately NOT wrapped in an outer `async with
                # conn.transaction()` here (found by actually running this
                # against Postgres, not by reading the code — see
                # docs/decisions.md): repositories.create_lead()'s first
                # insert attempt runs as a bare top-level statement,
                # expecting a UniqueViolationError there to abort only
                # itself, not an enclosing transaction. Wrapping the whole
                # row in one outer transaction meant that first violation
                # poisoned the entire transaction — every following
                # statement, including create_lead()'s own internal
                # recovery savepoint, failed with "current transaction is
                # aborted". Each repository call below already manages its
                # own internal atomicity (upsert_company, upsert_contact,
                # create_lead all do); nothing here needs a wider one, and
                # every write below is idempotent on retry regardless (a
                # re-run of this row finds what a partially-completed
                # earlier attempt already created via the pre-check /
                # create_lead's own typed outcomes, and re-emits the same
                # idempotency-keyed event as a no-op).
                result = await _import_row(
                    conn,
                    company_stub=company_stub,
                    contact_stub=contact_stub,
                    industry_pack=resolved_pack,
                )
                results.append(result)
    finally:
        await conn.close()

    _print_summary(len(provider.errors), results)
    return 1 if any(r.outcome == "failed" for r in results) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument(
        "--industry-pack",
        default=None,
        help="Overrides INDUSTRY_PACK / the auto-detected single pack (core/config.py).",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.csv_path, industry_pack=args.industry_pack))


if __name__ == "__main__":
    sys.exit(main())
