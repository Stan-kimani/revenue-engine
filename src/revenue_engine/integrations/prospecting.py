"""Prospecting provider interface (discovery-addendum.md §3) and its v1
implementation, ManualCsvProvider.

Discovery itself (the `discovery.requested` event flow) is out of scope until
the ManualCsvProvider pilot clears its threshold (discovery-addendum.md §8) —
`discover_companies`/`find_contacts`/`verify_email` exist here so
scripts/import_leads.py has one real implementation of the interface to
import through, per this milestone's CSV contract, not because the discovery
flow is being wired up. No business logic beyond CSV parsing lives here
(CLAUDE.md §2: agents/ may import integrations/ interfaces; integrations/
contains zero business logic) — row validation is mechanical (are the two
required columns present?), not a judgment call.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..db.models import EmailStatus


@dataclass(frozen=True)
class DiscoveryFilters:
    """Built from an industry pack's icp.* sections (discovery-addendum.md
    §3) once a real vendor implementation exists. ManualCsvProvider accepts
    this for Protocol conformance but ignores it — a human already curated
    the CSV; there is nothing left to filter."""

    business_models: tuple[str, ...] = ()
    employee_bands: tuple[str, ...] = ()
    geographies: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompanyStub:
    name: str
    domain: str | None
    linkedin_url: str | None = None


@dataclass(frozen=True)
class ContactStub:
    email: str
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    source_note: str | None = None


class ProspectingProvider(Protocol):
    """discovery-addendum.md §3. Three operations, one interface, because a
    single vendor typically provides all three; kept separate from a future
    integrations/enrichment.py only if a second vendor is ever used for one.
    """

    async def discover_companies(
        self, filters: DiscoveryFilters, limit: int
    ) -> list[CompanyStub]: ...

    async def find_contacts(
        self, company: CompanyStub, target_titles: list[str], limit: int
    ) -> list[ContactStub]: ...

    async def verify_email(self, email: str) -> EmailStatus: ...


@dataclass(frozen=True)
class CsvRowError:
    """One rejected row from ManualCsvProvider's parse. The importer reports
    these and continues — a single bad row never fails the whole file (M1.1
    CSV contract, docs/decisions.md)."""

    row_number: int  # 1-indexed, header excluded — matches how a spreadsheet counts data rows
    reason: str


_REQUIRED_COLUMNS = frozenset({"domain", "contact_email"})

# Syntax-only — no real verification vendor exists in v1
# (discovery-addendum.md §3: "V1 implementation: ManualCsvProvider ...
# Nothing downstream knows which provider ran"). Deliberately conservative
# rather than RFC 5322-complete; this only needs to catch obviously
# malformed input, not replace a real verification vendor.
_EMAIL_SYNTAX_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ManualCsvProvider:
    """Reads the M1.1 CSV contract (docs/decisions.md):

        company_name, domain, contact_first_name, contact_last_name,
        contact_email, contact_title, linkedin_url, source_note

    `domain` and `contact_email` are required (the dedup natural keys); a row
    missing either is collected in `.errors`, not raised — the importer
    continues past it and reports a summary. All other columns are optional.
    `linkedin_url` is read as the CONTACT's personal profile — the one
    genuinely ambiguous column in the contract; docs/decisions.md logs this
    choice.

    Parses once, eagerly, at construction: the whole point of a v1
    "provider" over a human-curated list is that there is no cost-metered
    vendor call to defer, so `discover_companies`/`find_contacts` just read
    back what's already parsed.
    """

    def __init__(self, csv_path: Path | None = None) -> None:
        """`csv_path=None` constructs an empty provider — no companies/
        contacts to discover, `.errors` empty. Used when a caller only wants
        `verify_email()` (a pure, instance-state-independent check) without
        importing a file — e.g. agents/leadgen.py re-verifying a contact's
        email at enrichment time, well after scripts/import_leads.py's own
        provider instance (and its CSV) is gone."""
        self._companies: dict[str, CompanyStub] = {}  # keyed by lowercased domain
        self._contacts_by_domain: dict[str, list[ContactStub]] = {}
        self.errors: list[CsvRowError] = []
        if csv_path is not None:
            self._parse(csv_path)

    def _parse(self, csv_path: Path) -> None:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing_columns = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing_columns:
                raise ValueError(
                    f"{csv_path}: missing required column(s): {sorted(missing_columns)}"
                )
            for row_number, row in enumerate(reader, start=1):
                self._parse_row(row_number, row)

    def _parse_row(self, row_number: int, row: dict[str, str | None]) -> None:
        domain = (row.get("domain") or "").strip().lower()
        email = (row.get("contact_email") or "").strip().lower()
        if not domain or not email:
            missing = [
                name for name, value in (("domain", domain), ("contact_email", email)) if not value
            ]
            self.errors.append(
                CsvRowError(row_number=row_number, reason=f"missing required field(s): {missing}")
            )
            return

        company_name = (row.get("company_name") or "").strip() or domain
        company = self._companies.setdefault(domain, CompanyStub(name=company_name, domain=domain))
        contact = ContactStub(
            email=email,
            first_name=(row.get("contact_first_name") or "").strip() or None,
            last_name=(row.get("contact_last_name") or "").strip() or None,
            title=(row.get("contact_title") or "").strip() or None,
            linkedin_url=(row.get("linkedin_url") or "").strip() or None,
            source_note=(row.get("source_note") or "").strip() or None,
        )
        self._contacts_by_domain.setdefault(company.domain or domain, []).append(contact)

    async def discover_companies(self, filters: DiscoveryFilters, limit: int) -> list[CompanyStub]:
        """`filters` is accepted for Protocol conformance and ignored — a
        human already curated this CSV; discovery-addendum.md §3's
        DiscoveryFilters exists for a real vendor's query, not a fixed
        list."""
        return list(self._companies.values())[:limit]

    async def find_contacts(
        self, company: CompanyStub, target_titles: list[str], limit: int
    ) -> list[ContactStub]:
        """`target_titles` is accepted for Protocol conformance and ignored,
        same reasoning as `discover_companies`'s `filters`."""
        if company.domain is None:
            return []
        return self._contacts_by_domain.get(company.domain, [])[:limit]

    async def verify_email(self, email: str) -> EmailStatus:
        """Syntax-only (docs/decisions.md, M1.1 judgment call) — no real
        verification vendor exists in v1. Never returns VALID: a syntax
        check alone cannot confirm deliverability, and returning VALID from
        this would be a guess wearing a confident label."""
        if not _EMAIL_SYNTAX_RE.match(email):
            return EmailStatus.INVALID
        return EmailStatus.UNVERIFIED
