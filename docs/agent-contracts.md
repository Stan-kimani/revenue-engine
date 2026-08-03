# Agent Contracts

**Status:** architecture phase, deliverable 4. Feeds `src/revenue_engine/agents/*.py`,
`orchestrator/router.py`, and the prompt/schema pairs in deliverable 5.

Derived from `docs/event-catalog.md` §8. Every agent below consumes at least one event
and emits at least one. No agent calls another agent.

---

## 0. Contract conventions

### 0.1 What an agent is
A module in `agents/` exposing `handle_<event>(event, ctx)` functions registered in
`router.py`. It has no state, no background loop, and no knowledge of other agents.
It reads via `db/repositories`, judges via `core/llm.complete_json`, acts via
`integrations/*`, and emits via `core/events.emit`.

### 0.2 The deterministic/LLM split
Every contract below separates **deterministic logic** (code, testable, free, exact)
from **LLM tasks** (judgment, costed, schema-validated). The rule:

> If the answer is derivable from data by a rule, it is code. The LLM is only for
> reading unstructured text and making a judgment a rule cannot express.

Arithmetic, thresholds, band assignment, state transitions, dedup on exact keys,
metric computation — always code. If Claude Code proposes an LLM call for any of
these, reject it.

### 0.3 Model tiers
Set per prompt in frontmatter, resolved from `config/base.yaml`:

| Tier | Used for | Rough share of calls |
|---|---|---|
| `fast` (Haiku-class) | classification, extraction, dedup judgment, sub-scores | ~70% |
| `standard` (Sonnet-class) | outreach drafting, meeting analysis, campaign assets | ~25% |
| `deep` (Opus-class) | learning synthesis, win/loss analysis, ICP proposals | ~5% |

Never use a `deep` model for classification. The cost difference across thousands of
leads is the difference between viable and not.

### 0.4 Autonomy levels
- **A0 — full autonomy:** no external side effects. Research, scoring, analysis.
- **A1 — autonomous with caps:** external side effects allowed within config limits
  (follow-ups inside an approved sequence).
- **A2 — approval required:** blocked until an approval token exists.
- **A3 — human only:** agent may propose, never execute.

### 0.5 Universal failure rules
- LLM output fails schema twice → `llm.validation_failed`, job dead-letters, no partial write.
- Integration 5xx → retry with backoff, then `integration.degraded`.
- Constraint violations representing business outcomes → convert to a domain event
  (e.g. `lead.deferred`), never surface as a raw exception.
- Every handler is idempotent. Re-running on the same event must not double-write.

---

## 1. Lead Generation Agent — `agents/leadgen.py`

**Purpose:** turn a raw prospect into a complete, verified, provenance-tagged profile.
**Phase:** 1 · **Autonomy:** A0

| | |
|---|---|
| **Consumes** | `lead.captured`, `discovery.requested`, schedule: `replenish_check` (hourly) |
| **Reads** | companies, contacts, leads, campaigns, industry pack |
| **Emits** | `lead.enriched`, `lead.enrichment_failed`, `lead.deferred`, `discovery.completed`, `discovery.budget_exhausted`, `discovery.yield_low` |

**Deterministic logic**
- Company resolution by exact `domain` match before any LLM call.
- Contact resolution by exact `email` (citext) match.
- Single-thread check: attempt insert; catch unique violation on
  `one_active_lead_per_company` → emit `lead.deferred` with `blocked_by_lead_id`.
- Email verification via provider; write `email_status` column directly.
- Provenance envelope construction for every `attributes` write (entity model §2).

**LLM tasks**

| Prompt | Schema | Tier | Judges |
|---|---|---|---|
| `leadgen/enrich_company.md` | `outputs/company_enrichment.json` | fast | Structure raw web/provider text into industry, positioning, size signals |
| `leadgen/enrich_decision_maker.md` | `outputs/contact_enrichment.json` | fast | Seniority, decision authority, likely responsibilities from title + bio |
| `leadgen/build_prospect_profile.md` | `outputs/prospect_profile.json` | standard | Synthesis: what this company likely struggles with, hooks worth using |

**Discovery** (see `docs/discovery-addendum.md`): demand-driven, not scheduled. An hourly
check emits `discovery.requested` when active leads fall below `discovery.replenish_floor`.
Filters are built from the industry pack, never hand-written. Dedup runs BEFORE contact
lookup — company discovery is cheap, contact and verification calls are not.
Provider is `ManualCsvProvider` in v1, behind `integrations/prospecting.py`.

**Tools allowed:** web search, web fetch, enrichment provider, email verification,
prospecting provider.
**Explicitly not allowed:** email send, calendar, any write to deals or messages.

**Escalation:** `lead.enrichment_failed` after 3 attempts → review queue. Never guess a
missing email; an unverified address is worse than no lead.

**Tests that must exist**
- Same `lead.captured` twice → one lead row (idempotency).
- Company with active lead → `lead.deferred`, no exception raised.
- Enrichment writes provenance envelope, not bare scalar.
- Invalid email → `email_status='invalid'`, no downstream qualification.

---

## 2. Lead Qualification Agent — `agents/qualification.py`

**Purpose:** score and band a lead, explainably.
**Phase:** 1 · **Autonomy:** A0

| | |
|---|---|
| **Consumes** | `lead.enriched`, `reply.received` (re-score on engagement) |
| **Reads** | leads, contacts, companies, messages, lead_scores, industry pack |
| **Emits** | `lead.scored`, `lead.qualified.{cold\|warm\|mql\|sql}`, `lead.routed_to_human` |

**Deterministic logic (the majority of the score)**
- ICP field matches: industry in list, employee_band in range, geography, disqualifiers.
- Engagement points: opens*, clicks, replies, meetings — counted from `messages`.
- Weighted sum using `scoring.weights` from the **pinned** `industry_pack` on the lead.
- Band assignment from `scoring.bands` thresholds. **Never LLM-decided.**
- Inbound bypass (R2): if `source ∈ {webform, inbound_reply, referral}` → emit
  `lead.routed_to_human` in addition to the normal band event.

\* open rates are unreliable post-MPP; weight them near zero and say so in config comments.

**LLM tasks**

| Prompt | Schema | Tier | Judges |
|---|---|---|---|
| `qualification/score_lead.md` | `outputs/lead_subscores.json` | fast | Only fuzzy components: buying-intent signals in unstructured text, seniority interpretation, fit narrative. Returns sub-scores 0–1 **with evidence strings.** |

The LLM never returns a total or a band. It returns components; code computes the sum.
`lead_scores.deterministic_part` and `llm_part` are stored separately so the two halves
can be audited independently.

**Tools allowed:** none. This agent has no external side effects.

**Tests that must exist**
- Identical input + same pack → same deterministic part (exact equality).
- Band boundaries: score exactly at threshold lands in the correct band.
- Inbound source emits both band event and `lead.routed_to_human`.
- Changing pack weights does not alter historical `lead_scores` rows.

---

## 3. Sales Agent — `agents/sales.py`

**Purpose:** everything between a qualified lead and a booked meeting.
**Phase:** 1 · **Autonomy:** A1/A2 depending on action

This is the largest agent. **Boundary decision:** reply handling is *not* split into a
separate agent in v1, because drafting and reply-classification share the same
conversation context and the same sequence state; splitting them would mean passing
that state through events purely to satisfy a diagram. Revisit if `sales.py` exceeds
~400 lines, at which point split by *lifecycle stage* (outbound vs conversation), not
by task.

| | |
|---|---|
| **Consumes** | `lead.qualified.sql`, `reply.received`, `followup.due`, `meeting.requested`, `approval.granted` |
| **Reads** | leads, contacts, companies, messages, sequences, sequence_runs, deals, semantic memory |
| **Emits** | `outreach.drafted`, `outreach.sent`, `outreach.blocked`, `reply.classified`, `reply.unmatched`, `objection.logged`, `deal.created`, `meeting.scheduled`, `sequence.paused`, `sequence.completed`, `contact.unsubscribed` |

**Deterministic logic — the gates (all in code, checked at send time)**

Order matters; each is a hard stop:
1. `email_status = 'suppressed'` → `outreach.blocked(suppressed_contact)`.
2. Daily cap for the mailbox reached → `outreach.blocked(daily_cap_reached)`, requeue for tomorrow.
3. `ENV != production` and recipient ≠ `DEV_SANDBOX_EMAIL` → redirect, emit `outreach.blocked(dev_sandbox)`.
4. Action requires approval and no valid token → `approval.requested`, halt.

These live in `integrations/gmail.py::send()` itself, not in the agent, so no code path
can bypass them.

**Sequence state machine** (`orchestrator/sequences.py`)
States: `pending → active → paused → completed | terminated`.
- Any inbound reply → `paused` immediately, before classification.
- `not_now` → `paused` with `resume_after` date.
- `unsubscribe` / `interested` → `terminated`.
- Steps exhausted → `completed`.
Sequence definitions come from config, never hardcoded.

**LLM tasks**

| Prompt | Schema | Tier | Judges |
|---|---|---|---|
| `sales/research_account.md` | `outputs/account_brief.json` | standard | Synthesise profile + semantic memory into an angle |
| `sales/draft_initial_outreach.md` | `outputs/outreach_draft.json` | standard | First-touch copy |
| `sales/draft_followup.md` | `outputs/outreach_draft.json` | standard | Step-aware follow-up |
| `sales/classify_reply.md` | `outputs/reply_classification.json` | fast | Intent, confidence, objection category |
| `sales/handle_objection.md` | `outputs/objection_response.json` | standard | Response using retrieved past wins |
| `sales/draft_proposal.md` | `outputs/proposal_draft.json` | standard | Always A2-gated |

**Confidence gate:** `classify_reply.confidence < thresholds.reply_confidence_min`
(default 0.75) → route to human review, do not act on the guess.

**Deal creation (R3):** on `reply.classified` with intent `interested`, or on
`meeting.scheduled` — emit `deal.created`. Not at SQL.

**Tools allowed:** Gmail send (gated), Gmail read, Google Calendar, semantic memory.
**Not allowed:** direct writes to deals beyond creation, CRM stage changes (CRM Sync owns those).

**Escalation to human:** anger/complaint sentiment, legal or procurement language,
pricing questions, confidence below threshold, enterprise-size account, any reply the
classifier marks `unclear`.

**Tests that must exist**
- Suppressed contact → blocked, zero sends, event emitted.
- Cap reached → blocked and requeued, not dropped.
- Missing approval token → no send, `approval.requested` emitted.
- Reply mid-sequence → sequence paused *before* classification runs.
- Same `provider_message_id` delivered twice → one `reply.classified`.
- `interested` → exactly one `deal.created`, even on event replay.

---

## 4. Meeting Intelligence Agent — `agents/meeting_intel.py`

**Purpose:** turn a transcript into structured deal intelligence.
**Phase:** 2 · **Autonomy:** A0 (analyses only; CRM Sync performs writes)

| | |
|---|---|
| **Consumes** | `meeting.completed` |
| **Reads** | meetings, deals, leads, contacts, messages |
| **Emits** | `meeting.analyzed`, `objection.logged` |

**Deterministic logic**
- Transcript acquisition and normalisation (VTT/SRT/plain → text + speaker turns).
- Chunking and embedding into `embeddings` for semantic memory.
- Objection category mapping to the **closed config vocabulary** — the LLM proposes,
  code maps to the enum, unmapped values go to `other` with the verbatim retained.

**LLM tasks**

| Prompt | Schema | Tier | Judges |
|---|---|---|---|
| `meeting_intel/summarize_meeting.md` | `outputs/meeting_summary.json` | standard | Narrative summary |
| `meeting_intel/extract_insights.md` | `outputs/meeting_insights.json` | standard | Action items, objections, buying signals, competitors, urgency — one structured pass |
| `meeting_intel/score_deal_health.md` | `outputs/deal_health.json` | standard | 0–100 with cited reasons |

Insights are written to `meeting_insights` with `prompt_version`, so a transcript can be
re-analysed by a newer prompt without destroying the original reading.

**Tools allowed:** transcription provider, semantic memory write.
**Not allowed:** any CRM write, any email or calendar action.

**Tests that must exist**
- Fixture transcript with a known price objection → `objection_category: price`.
- Re-analysis creates a second `meeting_insights` row, does not overwrite.
- Missing transcript → no partial insight row; job waits or dead-letters cleanly.

---

## 5. CRM Sync Agent — `agents/crm_sync.py`

**Purpose:** keep the database consistent. The only agent that writes the timeline.
**Phase:** 2 · **Autonomy:** A0 for upserts, A2 for merges, A3 for deletes

| | |
|---|---|
| **Consumes** | `outreach.sent`, `reply.classified`, `deal.created`, `meeting.scheduled`, `meeting.analyzed`, `contact.unsubscribed` |
| **Reads** | all entities |
| **Emits** | `crm.updated`, `crm.merge_proposed`, `deal.stage_changed`, `deal.closed.{won\|lost}` |

**Deterministic logic**
- Idempotent upserts on natural keys (`companies.domain`, `contacts.email`,
  `messages.provider_message_id`).
- Dedup ladder, in order: exact email → exact domain → normalised domain
  (strip www/tld variants) → trigram name similarity above config threshold.
  Only what survives all four ambiguously reaches the LLM.
- Pipeline stage transitions from a config-defined transition table. Invalid
  transitions are rejected, not silently applied.
- Suppression writes on `contact.unsubscribed`.
- FX freeze on close (entity model §8.2) — enforced inside `close_deal()`.

**LLM tasks**

| Prompt | Schema | Tier | Judges |
|---|---|---|---|
| `crm/dedup_judgment.md` | `outputs/dedup_judgment.json` | fast | Only genuinely ambiguous pairs: same company or not, with evidence |

**Why company dedup is load-bearing here:** the single-thread rule is enforced per
`company_id`. Two rows for one company defeat it, and the same account gets pursued
twice. Company dedup protects a business rule, not just tidiness.

**Tools allowed:** none external in v1 (`crm_external` is a no-op).
**Deletion:** never autonomous, at any confidence, under any config.

**Tests that must exist**
- Re-import of the same CSV → zero new rows.
- `acme.com` and `www.acme.com` → one company, no LLM call.
- Merge above threshold still requires approval before executing.
- Invalid stage transition rejected with a clear error.
- Closed deal's `fx_rate_to_base` cannot be modified afterwards.

---

## 6. Learning Agent — `agents/learning.py`

**Purpose:** explain what is working and propose changes. Never applies them.
**Phase:** 3 · **Autonomy:** A3 (proposal only)

| | |
|---|---|
| **Consumes** | `deal.closed.{won\|lost}`, schedule: `weekly_learning` |
| **Reads** | deals, leads, lead_scores, messages, objections, meetings, campaigns |
| **Emits** | `learning.published`, `messaging.update_proposed`, `icp.update_proposed`, `sequence.update_proposed` |

**Deterministic logic — all metrics are SQL, not model output**
Reply rate by prompt_version · conversion by industry/band/source · cycle duration ·
objection frequency by category · campaign attribution to `value_base_amount` ·
inbound vs outbound score comparison · SQL→deal conversion (the scoring quality metric).

The LLM never counts. It interprets counts.

**LLM tasks**

| Prompt | Schema | Tier | Judges |
|---|---|---|---|
| `learning/analyze_win_loss.md` | `outputs/win_loss_analysis.json` | deep | Why this cohort closed or didn't, citing deal_ids |
| `learning/analyze_objections.md` | `outputs/objection_analysis.json` | standard | Patterns across objection text |
| `learning/generate_recommendations.md` | `outputs/recommendations.json` | deep | Concrete proposed diffs with evidence + sample size |

**Evidence requirement:** every finding must cite `deal_ids` or `message_ids` and state
`sample_size`. A recommendation without evidence fails schema validation. Below a config
minimum sample (default 15), findings are marked `low_confidence` and excluded from
auto-surfacing.

**Tools allowed:** semantic memory read.
**Never:** writes to config, campaigns, or prompts. Proposals only.

**Tests that must exist**
- Fixture of 20 closed deals → schema-valid recommendations with citations.
- Sample below minimum → flagged `low_confidence`, not suppressed silently.
- No proposal ever mutates an industry pack file.

---

## 7. Market Intelligence Agent — `agents/market_intel.py`

**Purpose:** external research and ICP refinement proposals.
**Phase:** 4 · **Autonomy:** A0 for research, A3 for ICP proposals

| | |
|---|---|
| **Consumes** | schedule: `weekly_market_research`, manual request |
| **Reads** | industry pack, prior insights, semantic memory |
| **Emits** | `market.insight_published`, `icp.update_proposed` |

**LLM tasks**

| Prompt | Schema | Tier | Judges |
|---|---|---|---|
| `market_intel/research_market.md` | `outputs/market_insight.json` | standard | Trends, news, audience opportunities from search results |
| `market_intel/analyze_competitor.md` | `outputs/competitor_profile.json` | standard | Structured competitor read |
| `market_intel/propose_icp_update.md` | `outputs/icp_proposal.json` | deep | Diff against current pack, with rationale |

**Source discipline:** every insight carries `sources: [url]`. Claims without a source
fail schema validation. This prevents confident hallucinated market "facts" entering
the system and being retrieved later as memory.

**Tools allowed:** web search, web fetch, semantic memory write.
**ICP changes are always A3** — they alter scoring for every future lead.

---

## 8. Marketing Strategy Agent — `agents/marketing.py`

**Purpose:** turn insight and learning into campaigns and assets.
**Phase:** 4 · **Autonomy:** A2 for launches above threshold, A1 below

| | |
|---|---|
| **Consumes** | `learning.published`, `market.insight_published`, manual request, schedule: `campaign_review` |
| **Reads** | campaigns, campaign_assets, learnings, insights, metrics |
| **Emits** | `campaign.published`, `campaign.assets_created` |

**Deterministic logic**
- Campaign metrics from SQL: reply rate (primary), CTR, cost per lead, lead→SQL,
  SQL→deal, revenue in `base_currency`.
- Audience size computed from filters before launch; above
  `thresholds.campaign.max_autonomous_audience` → A2.

**LLM tasks**

| Prompt | Schema | Tier | Judges |
|---|---|---|---|
| `marketing/generate_campaign.md` | `outputs/campaign_plan.json` | standard | Concept, audience, channels, success metrics |
| `marketing/draft_email_sequence.md` | `outputs/email_sequence.json` | standard | Full sequence copy |
| `marketing/draft_linkedin_post.md` | `outputs/social_post.json` | standard | Post copy (manual posting — no LinkedIn automation) |
| `marketing/draft_blog_outline.md` | `outputs/blog_outline.json` | standard | Outline only |

**Open gap to close in Phase 4:** the nurture path for `mql` and `warm` leads is
currently unconsumed — `lead.qualified.mql` has no handler until this agent exists.
Until then those leads sit at `status='scored'`. Acceptable for Phase 1; must not be
forgotten.

---

## 9. Router summary

`orchestrator/router.py` is a dict, not conditionals:

```python
ROUTES: dict[str, list[JobSpec]] = {
    "lead.captured":        [JobSpec("leadgen.enrich")],
    "lead.enriched":        [JobSpec("qualification.score")],
    "lead.qualified.sql":   [JobSpec("sales.start_sequence")],
    "lead.routed_to_human": [JobSpec("notify.slack")],
    "reply.received":       [JobSpec("sales.handle_reply")],
    "reply.classified":     [JobSpec("crm_sync.apply")],
    "deal.created":         [JobSpec("crm_sync.apply")],
    "meeting.completed":    [JobSpec("meeting_intel.analyze")],
    "meeting.analyzed":     [JobSpec("crm_sync.apply")],
    "deal.closed.*":        [JobSpec("learning.analyze_deal")],
    # ...
}
```

**Verification:** a contract test asserts every event type in the catalog appears
either as a route key or on an explicit `UNCONSUMED` allowlist. Unconsumed events must
be a deliberate, named decision — this is what catches the MQL nurture gap above.

---

## 10. Agent boundary rules (enforced in review)

1. No agent imports another agent.
2. Only CRM Sync writes `activities`.
3. Only Sales sends email.
4. Only CRM Sync changes `deals.stage`.
5. Only Learning and Market Intelligence create proposals; neither applies them.
6. Only Meeting Intelligence writes `meeting_insights`.
7. Any agent may read anything.

If a needed action crosses these lines, emit an event — do not reach across.
