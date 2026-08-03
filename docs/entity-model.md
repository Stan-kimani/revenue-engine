# Entity Model

**Status:** architecture phase, deliverable 2. Feeds `migrations/0001_init.sql`.
This document defines the domain nouns only. Operational tables (`events`, `jobs`,
`approvals`, `agent_runs`, `embeddings`) are infrastructure and are specified in §7.

---

## 1. Core decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | `contacts` is the person; `leads` is a **pursuit** (contact × campaign); `deals` is the revenue opportunity. No Salesforce-style lead→contact "conversion". | A person's history must never split. Repeat targeting must be representable. Pipeline metrics must not be polluted by cold prospects. |
| D2 | One **active** lead per contact, enforced by partial unique index. | Prevents two campaigns emailing the same person concurrently. Enforced in DB, not agent logic. |
| D3 | Indexed/filterable fields are columns. AI-inferred fields live in `attributes` JSONB with embedded provenance. | Avoids schema explosion while keeping every score traceable to its evidence. |
| D4 | Soft delete (`deleted_at`) on all domain entities. Hard delete is human-approved only. | Build-spec §4.5. Deleted records still matter to learning. |
| D5 | Natural keys: `companies.domain`, `contacts.email` (citext, unique). | Dedup depends on a real uniqueness constraint, not application logic. |
| D6 | `email_status` is a first-class column, and suppression is enforced at the DB level. | Deliverability. A suppressed contact must be unemailable regardless of agent behaviour. |
| D7 | Scores are append-only in `lead_scores`; `leads.current_score` is a denormalised cache. | Learning needs score history and the component breakdown that produced each score. |

---

## 2. Attribute provenance convention

Every value inside an `attributes` JSONB column is an object, never a bare scalar:

```json
{
  "industry": {
    "value": "B2B SaaS",
    "confidence": 0.82,
    "evidence": "Homepage copy: \"the operating system for B2B SaaS finance teams\"",
    "source": "llm:enrich_company",
    "run_id": "a4f1...",
    "observed_at": "2026-07-23T09:14:00Z"
  },
  "employee_count": {
    "value": 12,
    "confidence": 1.0,
    "evidence": null,
    "source": "provider:apollo",
    "run_id": null,
    "observed_at": "2026-07-23T09:14:00Z"
  }
}
```

**Corrected 2026-08-04** (was previously `{value, source, confidence, run_id,
observed_at}`, missing `evidence`): the model supplies `value`, `confidence`,
and `evidence` — code adds `source`, `run_id`, and `observed_at` when writing
the envelope. `evidence` is the snippet or reasoning that produced `value`;
without it, provenance degrades to "confidence 0.82" with nothing to check it
against, which defeats the point of storing provenance at all. `evidence` may
be `null` for non-LLM sources with no natural text evidence (provider API
responses, human entry).

`source` is always `<kind>:<detail>` where kind is one of `llm`, `provider`, `human`,
`derived`, `webform`. Enforced by `schemas/entities/attribute.json` and validated in
`repositories.py` before write. Never write a bare value into `attributes`.

**Promotion rule:** when a field starts being used in a WHERE clause, a JOIN, or a
scoring weight, promote it to a real column in a new migration. `attributes` is for
context, not for hot paths.

---

## 3. Entities

### 3.1 `companies`
The organization. One row per real company.

| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| domain | citext unique | Natural key. Nullable — some prospects have no site. |
| name | text not null | |
| linkedin_url | text | |
| country | text | |
| employee_band | text | Promoted from attributes: drives scoring. Enum-ish via config. |
| attributes | jsonb not null default '{}' | industry, revenue_est, tech_stack, funding, positioning |
| created_at / updated_at / deleted_at | timestamptz | |

Index: `domain`, `employee_band`.

### 3.2 `contacts`
The human. One row per real person, permanently.

| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| email | citext unique | Natural key. |
| email_status | enum | `unverified, valid, risky, invalid, bounced, suppressed` |
| full_name / first_name / last_name | text | |
| title | text | Raw job title as found. |
| linkedin_url | text | |
| company_id | uuid fk → companies | **Current** employer. Nullable. |
| attributes | jsonb | seniority, decision_authority, inferred_pains, timezone |
| created_at / updated_at / deleted_at | timestamptz | |

Index: `email`, `company_id`, `email_status`.

**Job changes (v1 behaviour):** the same human keeps the same row; `company_id` is
updated and the prior value is appended to `attributes.employment_history`. A full
employment-history table is deferred until there is a real need.

**Suppression:** `email_status = 'suppressed'` is terminal. `integrations/gmail.py`
checks this before every send. Unsubscribes write it directly.

### 3.3 `campaigns`
A coordinated outreach effort. Owns messaging and audience definition.

| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| name | text not null | |
| industry_pack | text not null | Which `config/industries/*.yaml` governs it. |
| channel | enum | `email, linkedin, multi` |
| status | enum | `draft, pending_approval, active, paused, completed, archived` |
| sequence_key | text | Which sequence definition from config to run. |
| goal | jsonb | target audience size, success metric, owner |
| approved_by / approved_at | text / timestamptz | HITL record |
| created_at / updated_at | timestamptz | |

### 3.4 `leads` — the pursuit
A single attempt to engage one contact under one campaign. **This is the unit of work.**

| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| contact_id | uuid fk not null | |
| company_id | uuid fk | Denormalised at creation for scoring/reporting stability. |
| campaign_id | uuid fk **nullable** | Null for inbound/manual leads. |
| industry_pack | text not null | The config that scored this lead — pinned, never re-read. |
| source | text not null | `webform, manual_import, discovery, referral, inbound_reply` |
| status | enum | `new, deferred, enriching, enrich_failed, scored, qualified, engaged, meeting_booked, converted, disqualified, unsubscribed, dormant` |
| band | enum nullable | `cold, warm, mql, sql` |
| current_score | numeric(5,2) | Cache of latest `lead_scores.total`. |
| deal_id | uuid fk nullable | Set when a deal is created. |
| budget_band | enum nullable | `unknown, under_5k, 5k_15k, 15k_40k, 40k_plus` (D1) |
| budget_source | enum nullable | `self_reported, inferred, discovery_call` (D1) |
| problem_statement | text nullable | Free text, **required for inbound sources** — see CHECK below (D8) |
| pain_category | enum nullable | `manual_data_entry, slow_followup, reporting_visibility, intake, reconciliation, other` (D8) |
| team_size_band | enum nullable | `solo, 2_10, 11_50, 50_plus` (D8) |
| first_touched_at / last_activity_at | timestamptz | |
| created_at / updated_at / deleted_at | timestamptz | |

**Constraint D2:**
```sql
CREATE UNIQUE INDEX one_active_lead_per_contact
  ON leads (contact_id)
  WHERE status NOT IN ('converted','disqualified','unsubscribed','dormant')
    AND deleted_at IS NULL;
```

**Conditional requirement on `problem_statement` (D8):** required for inbound leads
only. A blanket NOT NULL would break outbound lead creation.
```sql
ALTER TABLE leads ADD CONSTRAINT problem_statement_required_for_inbound
  CHECK (
    source NOT IN ('webform','inbound_reply','referral')
    OR problem_statement IS NOT NULL
  );
```

**On `status = 'deferred'` (event catalog `lead.deferred`):** set when the company
already has an active lead and the single-thread index blocks insert. `first_touched_at`
stays null. A scheduled job re-evaluates deferred leads when the blocking lead resolves.
The constraint violation must be caught and converted to the event — never surfaced as
a raw `UniqueViolation`.

**Why `industry_pack` is pinned per lead:** if the pack's weights change next month,
old scores must remain interpretable. The Learning Agent compares like with like.

### 3.5 `lead_scores` — append-only
| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| lead_id | uuid fk not null | |
| total | numeric(5,2) not null | |
| band | enum not null | |
| components | jsonb not null | `{icp_match: {score, weight, evidence}, intent: {...}, ...}` |
| deterministic_part / llm_part | numeric | Split so the two halves can be audited separately. |
| prompt_version | int | From prompt frontmatter. |
| model | text | |
| run_id | uuid fk → agent_runs | |
| scored_at | timestamptz not null | |

Never updated. Never deleted. `leads.current_score` is refreshed on insert.

### 3.6 `deals`
Created only when a lead reaches SQL **and** engages (replies positively or books).

| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| lead_id | uuid fk not null | |
| contact_id / company_id | uuid fk | Denormalised for reporting. |
| stage | text not null | FK-ish to `pipeline_stages.key`, config-seeded. |
| value_amount | numeric(12,2) | |
| currency | char(3) | |
| expected_close_at | date | |
| outcome | enum nullable | `won, lost` — null while open. |
| lost_reason | text | Constrained vocabulary from config, not freeform. |
| health_score | int | 0–100, from Meeting Intelligence. |
| closed_at | timestamptz | |
| created_at / updated_at / deleted_at | timestamptz | |

### 3.7 `messages`
Every inbound and outbound communication.

| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| lead_id / contact_id | uuid fk | |
| campaign_id | uuid fk nullable | Attribution. |
| direction | enum | `outbound, inbound` |
| channel | enum | `email, linkedin, other` |
| provider_message_id | text unique | Gmail message id. **Idempotency key.** |
| thread_id | text | |
| subject / body_text | text | |
| sequence_step | int nullable | Which step produced it. |
| prompt_version | int nullable | For messaging performance analysis. |
| classification | jsonb nullable | Output of `classify_reply` for inbound. |
| approval_id | uuid fk nullable | Which approval authorised the send. |
| sent_at / received_at | timestamptz | |
| created_at | timestamptz | |

`provider_message_id` unique is what makes webhook replay and the reconciliation
job safe. Every inbound write is an upsert on this key.

### 3.8 `meetings` / `meeting_insights`
`meetings`: id, lead_id, deal_id, contact_id, external_event_id (unique), scheduled_at,
occurred_at, duration_s, transcript_url, transcript_text, status.

`meeting_insights`: id, meeting_id, summary, action_items jsonb, buying_signals jsonb,
urgency, competitors_mentioned jsonb, deal_health_score, run_id, prompt_version.

Split so a transcript can be re-analysed with a newer prompt without losing the original.

### 3.9 `objections`
| id | deal_id | lead_id | message_id / meeting_id | category (enum from config) | verbatim | response_given | resolved | outcome | created_at |

Category vocabulary lives in config, not in the model's head — otherwise the Learning
Agent counts "pricing", "price", and "too expensive" as three different objections.

**Closed set (8) — must be identical in the industry pack, `reply_classification.json`
and `objection_response.json`:** `price, timing, trust, need, authority, competitor,
handoff, other`. `handoff` (who maintains this after you leave — ownership, IP, lock-in,
bus factor) is the dominant unspoken objection in this category; burying it under
`trust` makes it invisible to the Learning Agent's objection-frequency analysis.

### 3.10 `activities`
Unified timeline for the operator view. Append-only: id, entity_type, entity_id,
lead_id, kind, summary, ref_table, ref_id, occurred_at, actor (`agent:sales`, `human:stan`).

Written by CRM Sync only. No other agent writes here.

---

## 4. Relationship summary

```
companies 1──* contacts
contacts  1──* leads          (only one active at a time — D2)
campaigns 1──* leads
leads     1──* lead_scores    (append-only history)
leads     1──0..1 deals
leads     1──* messages
leads     1──* meetings ──1 meeting_insights
deals     1──* objections
```

---

## 5. Resolved decisions (2026-07-23)

**R1 — Single-threaded per company.** One active lead per company at a time. If a
company already has an active lead, no second contact there is opened until the first
resolves (converted / disqualified / unsubscribed / dormant). This suits founder-led,
high-touch selling: one thread, one relationship, no risk of two AI-written emails
reaching the same account. It also removes account-level pacing logic entirely.

Enforced as a DB constraint (mirrors D2 but at company grain):
```sql
CREATE UNIQUE INDEX one_active_lead_per_company
  ON leads (company_id)
  WHERE status NOT IN ('converted','disqualified','unsubscribed','dormant')
    AND deleted_at IS NULL
    AND company_id IS NOT NULL;
```
No `account_limits` block is needed in the industry pack. Buying-committee /
multi-contact targeting is explicitly deferred; revisit only if deal patterns show a
real need, and reintroduce as concurrency + pacing config at that point.

**R2 — Inbound leads are scored but routed differently.** Inbound/reply-sourced leads
run through the full scorer (the Learning Agent needs inbound-vs-outbound score
comparison), but bypass the SQL band gate: any inbound lead routes to a human
immediately regardless of band. Implemented as a routing branch keyed on
`leads.source IN ('webform','inbound_reply','referral')`, not by faking the score.

**R3 — Deal creation trigger = first positive engagement.** A deal is created when a
lead first replies positively or books a meeting — not at SQL classification. SQL is
a prediction; a reply is evidence. This keeps pipeline value real and makes SQL→deal
conversion a true measure of scoring quality. Emitted as `deal.created` from the
Sales agent's reply-classification path when intent ∈ {interested, book_meeting}.

**R4 — Multi-currency from launch.** Selling globally day one. See §8.

---

## 6. What is deliberately NOT modelled in v1

Employment history table, contact-to-contact relationships, account hierarchies
(parent/subsidiary), quotes/line items, products, territories, multiple pipelines,
lead-source cost tracking. Each is a real concept and each is deferred until a
concrete need appears. Add them in a later migration, not now.

---

## 7. Infrastructure tables (specified, not domain)

`events`, `jobs`, `approvals`, `agent_runs`, `embeddings`, `pipeline_stages`,
`sequences`, `sequence_runs`, `campaign_assets`, `learnings`. These are locked by
build-spec §5.1 and need no domain decisions. They will be written directly into
migration 0001 alongside the entities above.

**Added 2026-08-04 — `embeddings` needs `scope`, not just `kind`.** The two are
orthogonal: `kind` is what the content *is* (transcript, email, proof,
objection_precedent); `scope` is the retrieval visibility tier —
`'global' | 'industry' | 'account'`. A proof record can be global or
industry-scoped; both are `kind = 'proof'`. `ref_company_id` is required when
`scope = 'account'` (which company it's scoped to) and must be null otherwise.
`memory.semantic_search` filters on `scope` + `industry` together, not `kind`
alone. `embeddings` also needs `verified boolean not null default false` —
competitive-deltas.md D4 requires only verified proof records be retrievable
for outreach; without this column that filter can't be enforced, and
unverified or aspirational content would eventually be retrieved and asserted
to a prospect as fact. `vector` itself is intentionally left with no fixed
dimension in migration 0001 — no embedding provider is named anywhere in these
docs, and fixing the dimension is the most expensive column to change later
(it means re-embedding every stored chunk). See docs/decisions.md.

---

## 8. Multi-currency model (R4)

Selling globally from launch requires three things most schemas get wrong. The
principle: **transact in the customer's currency, report in one base currency, and
freeze the conversion at close time.**

### 8.1 Base currency
One reporting currency for the whole system, set in `config/base.yaml`:
```yaml
finance:
  base_currency: USD          # every deal is also expressed in this for aggregation
  fx_provider: manual         # v1: rates entered/seeded; API adapter later
```
Every revenue aggregate (campaign ROI, pipeline value, learning metrics) is computed
in `base_currency`. Never sum raw `value_amount` across currencies.

### 8.2 `deals` — revised money columns
Replace the single amount/currency pair with a transacted + base pair, plus a frozen rate:

| Column | Type | Notes |
|---|---|---|
| value_amount | numeric(14,2) | Amount in the deal's own currency. |
| currency | char(3) | ISO 4217. The currency you quote/invoice the customer in. |
| fx_rate_to_base | numeric(18,8) | `base = value_amount * fx_rate_to_base`. |
| value_base_amount | numeric(14,2) | **Generated/stored** at write time. What all reports sum. |
| fx_rate_source | text | `manual`, `provider:<name>`, or `derived`. |
| fx_frozen_at | timestamptz | When the rate was locked (typically `closed_at`). |

Rules:
- While a deal is **open**, `value_base_amount` may be recomputed against the current
  rate (forecasts move with FX — that's fine).
- The moment `outcome` is set (won/lost), `fx_rate_to_base` and `fx_frozen_at` are
  **locked**. Closed revenue never changes afterward. Enforced in the repository's
  `close_deal()` method, not by trusting callers.

### 8.3 FX rates table
Minimal, provider-agnostic:

`fx_rates`: id, base_currency, quote_currency, rate, as_of (date), source, created_at.
Unique on (base_currency, quote_currency, as_of).

v1 seeds this manually or via `scripts/` from any source; a live FX API is an adapter
behind the same table later. The system reads the most recent `as_of <= deal date`.

### 8.4 Money handling rules (CLAUDE.md addition)
- All money is `numeric`, never float. No exceptions.
- No arithmetic across currencies without going through base.
- Display currency = deal currency; analytics currency = base currency. Never blur them.
- `campaigns.goal` and any target amounts are also base-currency.

### 8.5 What is deferred
Per-line-item pricing, tax/VAT handling, multi-currency invoicing documents,
real-time FX hedging. Not needed to *sell* globally — needed to *do accounting*
globally, which is a later problem.
