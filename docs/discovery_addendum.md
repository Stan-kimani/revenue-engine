# Discovery — Design Addendum

**Status:** architecture addendum to `docs/agent-contracts.md` §1 (Lead Generation).
Adds autonomous prospect discovery. Phase 1 scope.

---

## 1. What changed and why

The build spec listed "schedule: daily discovery run" as a leadgen trigger without
specifying the mechanism. The intuitive implementation — LLM plus web search — does not
work:

- Search returns listicles and directory pages, not company records. Stale, SEO-ranked,
  not fit-ranked.
- No firmographic data, so the scorer has nothing deterministic to work with.
- **No contact emails.** This is the actual blocker and search never solves it.
- Scraping directories to compensate creates ToS exposure and a dependency that breaks
  silently when a page structure changes.

**Discovery is a structured query against a prospect database.** Web search is retained
for signal detection on already-identified companies, which is a different job.

---

## 2. Trigger: demand-driven, not scheduled

A fixed daily discovery run over-produces. Send capacity during warmup is 20/day; a
queue of thousands of unworked leads buries the good ones and burns vendor credits for
nothing.

```
schedule: hourly check
  if count(leads WHERE status IN ('new','enriching','scored','qualified')) < replenish_floor:
      emit discovery.requested(target_count = replenish_ceiling - current)
```

Config in the industry pack:
```yaml
discovery:
  replenish_floor: 40          # trigger below this many active leads
  replenish_ceiling: 80        # top up to this
  max_per_run: 50              # hard cap regardless of gap
  credit_budget_per_week: 500  # vendor credits; refuse to exceed
  enabled: false               # per-pack kill switch
```

`credit_budget_per_week` is a real spend control, enforced in code before the vendor
call. A bug that loops discovery must cost a bounded amount of money.

---

## 3. New interface: `integrations/prospecting.py`

Three operations, one interface, because a single vendor typically provides all three.
Separate from `integrations/enrichment.py` only if a second vendor is ever used for one.

```python
class ProspectingProvider(Protocol):
    async def discover_companies(
        self, filters: DiscoveryFilters, limit: int
    ) -> list[CompanyStub]: ...

    async def find_contacts(
        self, company: CompanyStub, target_titles: list[str], limit: int
    ) -> list[ContactStub]: ...

    async def verify_email(self, email: str) -> EmailStatus: ...
```

`DiscoveryFilters` is built **from the industry pack**, never hand-written per run:
`icp.firmographics.business_models`, `employee_bands`, `geographies_preferred`,
`icp.roles.target_titles`. One source of truth; changing the pack changes discovery.

V1 implementation: `ManualCsvProvider` (reads an exported CSV, satisfies the same
interface). The vendor adapter lands behind the identical interface after the pilot
clears its threshold. **Nothing downstream knows which provider ran.**

---

## 4. Flow

```
discovery.requested
  → build DiscoveryFilters from industry pack
  → check credit budget; abort with discovery.budget_exhausted if over
  → provider.discover_companies(filters, limit)
  → DEDUP FIRST (before spending contact credits):
      drop domains already in companies table
      drop domains with an active lead (single-thread rule)
      drop suppressed / previously disqualified
  → provider.find_contacts(remaining, target_titles)
  → provider.verify_email() on each
  → drop unverified/invalid — never create a lead with an unverified address
  → create company + contact + lead rows
  → emit lead.captured per survivor  → normal Phase 1 pipeline
  → emit discovery.completed(found, deduped, kept, credits_used)
```

**Dedup before contact lookup is the cost control that matters.** Company discovery is
cheap; contact and verification calls are the expensive ones. Deduping after would pay
for records you already have.

---

## 5. New events

| Event | Payload core | Consumer |
|---|---|---|
| `discovery.requested` | `industry_pack, target_count, triggered_by` | leadgen |
| `discovery.completed` | `found, deduped, kept, credits_used, run_id` | ops notifier |
| `discovery.budget_exhausted` | `industry_pack, window, spent` | ops notifier, halts discovery |
| `discovery.yield_low` | `kept, expected, filters_used` | ops notifier |

`discovery.yield_low` fires when survivors fall far below target. That usually means the
ICP filters are too narrow or the vendor has poor coverage in your geography — both
worth knowing early rather than after three empty runs.

---

## 6. Web search's actual role: signal detection

Not discovery. Runs on companies already in the database, feeding `buying_intent` —
currently the weakest scoring input because nothing populates it.

Signals worth detecting for B2B service firms:
- Hiring for operations, admin, or coordinator roles (job board presence)
- Recent headcount growth mentions
- Public statements about capacity or backlog
- Leadership change in operations

Implementation: a scheduled `signal_scan` job over active leads, LLM-extracted against
`outputs/buying_signals.json` (Phase 1.5 — not required for first send). Store as
attributes with `source: derived:signal_scan` and re-score the lead on new signal.

---

## 7. Guardrails

1. Discovery **never** sends. It only creates leads. Every gate in the send path still applies.
2. `enabled: false` per pack by default. Turning discovery on is a deliberate act.
3. `max_per_run` and `credit_budget_per_week` enforced in code before the vendor call.
4. Verified email or no lead. An unverified address damages sender reputation.
5. Every discovered field carries `source: provider:<name>` provenance.
6. Discovery output goes through the same scoring as manual imports — no shortcuts,
   so the Learning Agent can compare source quality honestly.

---

## 8. Build order

Do not build this during Phase 1. Sequence:

1. Ship M1.1–M1.4 with `ManualCsvProvider` only.
2. Run 30–50 manual leads end to end. Calibrate scoring against real replies.
3. Pilot the vendor manually (CSV export → import) and measure anchor yield.
4. Only if the pilot clears its threshold, implement the vendor adapter and enable
   demand-driven discovery.

Automating discovery before the pipeline converts means scaling an unvalidated offer,
which produces failure faster and costs credits to do it.
