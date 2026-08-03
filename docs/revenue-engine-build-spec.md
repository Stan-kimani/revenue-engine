# AI Revenue Engine — Build Specification

**Purpose:** This document instructs a coding agent on exactly how to build an autonomous multi-agent revenue system. It is the single source of truth for architecture, structure, and build order. Read it fully before writing any code. Log every deviation in `docs/decisions.md`.

---

## 0. Executive Decisions (locked)

These decisions are final unless a blocker is logged in `docs/decisions.md`:

1. **Orchestration is deterministic code, not LLM routing.** Agents never talk to each other directly. They communicate exclusively through typed events written to Postgres. The orchestrator is a state machine + job queue. LLM calls happen only at judgment points, each as a single-purpose call with a strict JSON schema output.
2. **"Agents" are roles, not services.** An agent = a directory of prompt files + output schemas + a tool allowlist + a handler function. All agents run inside one Python monorepo/process pool. No microservices.
3. **Shared memory = Postgres (structured) + pgvector (semantic).** One database. No separate vector DB service in v1. Supabase-hosted Postgres with pgvector extension.
4. **We do not build a CRM.** Our Postgres schema IS the CRM and source of truth. The "CRM Agent" is a sync/dedup service. External CRM sync (HubSpot/Attio) is an adapter added later, behind an interface.
5. **Framework discipline:** No LangChain, CrewAI, AutoGen, or agent frameworks. Direct Anthropic API calls (with OpenAI as a swappable fallback behind one interface). FastAPI for HTTP. Plain SQL migrations. Minimum abstraction — every layer must justify its existence.
6. **MCP is dev-time only.** The coding agent may use MCP servers (Postgres, Gmail, filesystem) while building and testing. The runtime system uses direct API clients (`google-api-python-client`, etc.). No MCP dependencies in production code.
7. **Human-in-the-loop is enforced in the database, not in prompts.** Actions above autonomy thresholds create an `approvals` row and halt. Nothing bypasses this by prompt design — the send/execute functions check for an approval token at the code level.
8. **Industry reusability via config packs, not code branches.** All ICP definitions, scoring weights, messaging pillars, and vertical vocabulary live in `config/industries/*.yaml`. Core code never hardcodes an industry.

---

## 1. Build Phases (strict order)

Do not start a phase until the previous phase's acceptance criteria pass.

### Phase 0 — Skeleton (foundation)
Repo scaffold, database migrations, event outbox + job queue, LLM client with schema-validated outputs, config loader, observability wiring (Langfuse), FastAPI app with health endpoint, docker-compose for local Postgres.
**Accept when:** `docker compose up` runs; a test event flows through the queue; a test LLM call returns schema-validated JSON and appears in Langfuse.

### Phase 1 — Revenue Path (Lead Generation → Qualification → Sales Outreach)
The pipeline that makes money: capture/discover prospects, enrich, score against ICP, classify (Cold/Warm/MQL/SQL), generate personalized outreach for SQLs, send via Gmail with HITL approval gates and daily volume caps, handle replies, schedule meetings via Google Calendar, run follow-up sequences.
**Accept when:** a seeded prospect flows end-to-end from `lead.captured` to an approved, sent email and a logged reply; volume caps and approval gates block correctly in tests.

### Phase 2 — Meeting Intelligence + CRM Sync
Transcript ingestion (upload or webhook), meeting analysis (summary, action items, objections, buying signals, competitors, urgency, deal health score), automatic CRM updates, dedup service, pipeline stage transitions.
**Accept when:** an uploaded transcript produces a structured `meeting.analyzed` event, updates the deal record, and creates follow-up tasks without duplicating any contact/company.

### Phase 3 — Learning Loop
Batch analysis jobs (weekly + on `deal.closed`): win/loss analysis, objection frequency, messaging performance, cycle duration, campaign attribution. Publishes `learning.published` events containing structured recommendations that Phase 1 and Phase 4 consume.
**Accept when:** given fixture data of 20 closed deals, the learning job produces recommendations that validate against schema and are queryable ("which objections appear most often?").

### Phase 4 — Marketing Brain (Market Intelligence + Marketing Strategy)
Market research jobs (web search, competitor monitoring, trend tracking), ICP refinement proposals (HITL-approved before changing config), campaign generation (email sequences, LinkedIn posts, blog outlines, webinar concepts), campaign performance tracking, and consumption of Learning Agent recommendations.
**Accept when:** a market research run produces an ICP update proposal requiring approval; an approved campaign generates assets stored in `campaign_assets` and linked to the sequences that Phase 1 sends.

**Rationale for this order:** the Learning Agent is worthless without outcome data, and the Marketing Brain is worthless without a Learning Agent feeding it real conversion signal. Ship the pipeline that generates that data first.

---

## 2. Repository Structure

```
revenue-engine/
├── README.md                     # What this is, how to run it, links to docs
├── pyproject.toml                # Python 3.12+, uv-managed
├── docker-compose.yml            # Postgres + pgvector, Langfuse (optional local)
├── .env.example                  # Every env var documented, no real values
├── Makefile                      # make dev, make test, make migrate, make worker
│
├── docs/
│   ├── architecture.md           # Diagrams + data flow (kept current)
│   ├── decisions.md              # Append-only decision log (ADR-lite)
│   ├── agent-contracts.md        # Human-readable summary of every agent contract
│   ├── runbook.md                # Ops: deploy, rotate keys, recover from failures
│   └── deliverability.md         # Email warmup, caps, domain strategy
│
├── config/
│   ├── base.yaml                 # Global defaults: models, caps, schedules
│   ├── thresholds.yaml           # Autonomy thresholds (HITL triggers), score bands
│   └── industries/
│       ├── _template.yaml        # Documented template for new verticals
│       └── expert-entrepreneurs.yaml  # First vertical (example pack)
│
├── prompts/                      # ALL prompts live here. Never inline in code.
│   ├── _conventions.md           # Prompt file format rules (see §6)
│   ├── leadgen/
│   │   ├── enrich_company.md
│   │   ├── enrich_decision_maker.md
│   │   └── build_prospect_profile.md
│   ├── qualification/
│   │   ├── score_lead.md
│   ├── sales/
│   │   ├── research_account.md
│   │   ├── draft_initial_outreach.md
│   │   ├── draft_followup.md
│   │   ├── classify_reply.md
│   │   ├── handle_objection.md
│   │   └── draft_proposal.md
│   ├── meeting_intel/
│   │   ├── summarize_meeting.md
│   │   ├── extract_insights.md
│   │   └── score_deal_health.md
│   ├── crm/
│   │   └── dedup_judgment.md     # Only for ambiguous merge decisions
│   ├── learning/
│   │   ├── analyze_win_loss.md
│   │   ├── analyze_objections.md
│   │   └── generate_recommendations.md
│   ├── market_intel/
│   │   ├── research_market.md
│   │   ├── analyze_competitor.md
│   │   └── propose_icp_update.md
│   └── marketing/
│       ├── generate_campaign.md
│       ├── draft_email_sequence.md
│       ├── draft_linkedin_post.md
│       └── draft_blog_outline.md
│
├── schemas/                      # JSON Schemas. Source of truth for all contracts.
│   ├── events/                   # One file per event type (see §5)
│   ├── entities/                 # company, contact, lead, deal, meeting, campaign
│   └── outputs/                  # One file per prompt — every LLM output validates here
│
├── src/revenue_engine/
│   ├── core/
│   │   ├── llm.py                # complete_json(prompt_path, vars, output_schema) — the ONLY way to call an LLM
│   │   ├── events.py             # emit(), event outbox, typed event registry
│   │   ├── queue.py              # Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED)
│   │   ├── memory.py             # embed(), semantic_search() over pgvector
│   │   ├── approvals.py          # require_approval(), check_token(), notify
│   │   ├── config.py             # Loads base + industry pack, validates, exposes typed config
│   │   └── observability.py      # Langfuse tracing wrapper for every agent run
│   ├── agents/                   # One module per agent. Handlers only — no prompts, no SQL.
│   │   ├── leadgen.py
│   │   ├── qualification.py
│   │   ├── sales.py
│   │   ├── meeting_intel.py
│   │   ├── crm_sync.py
│   │   ├── learning.py
│   │   ├── market_intel.py
│   │   └── marketing.py
│   ├── orchestrator/
│   │   ├── router.py             # event type → handler mapping (the ONLY routing table)
│   │   ├── sequences.py          # Follow-up sequence state machine
│   │   └── schedules.py          # Cron-style jobs (learning runs, digest, retries)
│   ├── integrations/             # Thin clients. One file per service. No business logic.
│   │   ├── gmail.py
│   │   ├── gcal.py
│   │   ├── slack.py              # Approval notifications + daily digest
│   │   ├── enrichment.py         # Interface + provider impls (Apollo/Clay/manual)
│   │   ├── transcription.py      # Interface + provider impls (upload, Recall.ai later)
│   │   └── crm_external.py       # Interface only in v1; HubSpot/Attio adapters later
│   ├── db/
│   │   ├── models.py             # Typed row models (dataclasses/Pydantic, no ORM magic)
│   │   └── repositories.py       # All SQL lives here, grouped by entity
│   └── api/
│       ├── app.py                # FastAPI: webhooks, approval actions, health
│       └── routes/               # /webhooks/gmail, /webhooks/calendar, /approvals, /health
│
├── migrations/                   # Plain SQL, numbered: 0001_init.sql, 0002_....sql
│
├── tests/
│   ├── unit/                     # Pure logic: scoring math, config loading, dedup rules
│   ├── contracts/                # Every prompt output schema has a validation test
│   ├── golden/                   # Fixture input → LLM → assert schema + key fields
│   ├── integration/              # Event flow through queue with mocked integrations
│   └── fixtures/                 # Seed prospects, transcripts, closed-deal datasets
│
└── scripts/
    ├── seed_dev.py               # Seed a realistic dev dataset
    ├── run_worker.py             # Start queue workers
    └── backfill_embeddings.py
```

**Separation rules the coding agent must enforce:**
- `agents/` may import `core/`, `db/repositories`, and `integrations/` interfaces — never other agents.
- `prompts/` and `schemas/` contain zero code; `src/` contains zero prompt text.
- `integrations/` contains zero business logic — they translate our types to API calls and back.
- All SQL lives in `db/repositories.py`. No inline SQL in agents or routes.

---

## 3. Architecture

### 3.1 Runtime model
One FastAPI process (webhooks + approval endpoints) and N worker processes polling the Postgres job queue. Scheduled jobs enqueue work via `schedules.py`. Everything shares one Postgres database.

```
[Webhooks: Gmail push, Calendar, transcript upload]      [Schedules: cron jobs]
                 │                                              │
                 ▼                                              ▼
        ┌────────────────── Postgres ──────────────────┐
        │  events (outbox)  │  jobs (queue)  │  data   │
        └────────────────────────────────────────────────┘
                 │
                 ▼
        [Workers] → router.py → agent handler
                                   │
                     ┌─────────────┼──────────────┐
                     ▼             ▼              ▼
              core/llm.py    integrations/   db/repositories
              (schema-        (Gmail, GCal,
              validated       Slack, enrich)
              LLM calls)
                     │
                     ▼
              emits new events → cycle continues
```

### 3.2 Event flow (the spec's feedback loop, made concrete)
```
campaign.published → lead.captured → lead.enriched → lead.scored
  → lead.qualified.{cold|warm|mql|sql}
  → (SQL only) outreach.drafted → [approval if required] → outreach.sent
  → reply.received → reply.classified → {followup.scheduled | meeting.requested | objection.logged | unsubscribed}
  → meeting.scheduled → meeting.completed → meeting.analyzed
  → deal.stage_changed → deal.closed.{won|lost}
  → learning.published → {icp.update_proposed, messaging.update_proposed}
  → [approval] → config/campaign updates → campaign.published (loop closes)
```

### 3.3 The LLM boundary (non-negotiable)
Every LLM interaction goes through exactly one function:

```python
result = await complete_json(
    prompt_path="sales/draft_initial_outreach.md",
    variables={...},                     # rendered into the prompt template
    output_schema="outputs/outreach_draft.json",
    model=None,                          # None → per-task model from config
    trace=ctx.trace,                     # Langfuse trace for this agent run
)
```

`complete_json` renders the prompt file, calls the model, validates the response against the JSON schema, retries once on validation failure (feeding the validation error back), and raises after two failures — which dead-letters the job for human review. No agent parses freeform LLM text. Ever.

### 3.4 Shared memory
- **Structured:** all entities in Postgres (§5). Any agent reads any table via repositories.
- **Semantic:** an `embeddings` table (pgvector) storing chunks of: meeting transcripts, email threads, winning messaging, objection-response pairs, market research notes. `memory.semantic_search(query, kinds=[...], industry=..., scope=...)` is the single retrieval interface — `kind` is what the content *is*, `scope` (`global | industry | account`) is who it's retrievable for, and only `verified = true` chunks are ever retrieved for outreach (competitive-deltas.md D4; entity-model.md §7). Sales drafting prompts receive "similar past wins" and "how we've handled this objection before" as retrieved context.
- **What is NOT memory:** agents do not maintain private conversational state. All state is rows.

---

## 4. Agent Contracts

Every agent is defined by the same contract shape. The coding agent must implement each as a module in `src/revenue_engine/agents/` whose public surface is only `handle(event, ctx)` functions registered in `router.py`. Summarize all contracts in `docs/agent-contracts.md`.

**Contract fields:** triggers (events/schedules) · inputs (entity reads) · LLM tasks (prompt + output schema) · tools allowed · events emitted · autonomy level · escalation path.

### 4.1 Lead Generation Agent (`leadgen.py`) — Phase 1
- **Triggers:** `lead.captured` (webhook/form/manual import), `campaign.published` (prospect discovery job), schedule: daily discovery run.
- **LLM tasks:** `enrich_company` (structure raw research into company profile), `enrich_decision_maker`, `build_prospect_profile` (synthesis).
- **Tools:** enrichment provider (behind `integrations/enrichment.py` interface — v1 can be manual CSV import + web research; Apollo/Clay adapter later), web search, email verification provider.
- **Emits:** `lead.enriched`. On enrichment failure after retries: `lead.enrichment_failed` → review queue.
- **Autonomy:** full. No external side effects beyond API lookups.
- **Stores:** company (name, domain, industry, size, revenue est.), contact (name, role, LinkedIn, verified email), provenance for every field (source + timestamp — required for trust and debugging).

### 4.2 Lead Qualification Agent (`qualification.py`) — Phase 1
- **Triggers:** `lead.enriched`, `reply.received` (re-score on engagement), `web.activity` (if tracking added later).
- **Design:** scoring is **hybrid — deterministic first, LLM second.** Numeric components (ICP field matches, size band, industry weight, engagement events) are computed in code from `config/industries/*.yaml` weights. The LLM task `score_lead` judges only the fuzzy components (buying-intent signals in text, role seniority interpretation) and returns structured sub-scores with reasoning. Final score = weighted sum in code. This makes scores explainable and testable.
- **Classification bands** (from `thresholds.yaml`): cold / warm / MQL / SQL. Only SQLs emit `lead.qualified.sql`.
- **Emits:** `lead.scored`, `lead.qualified.{band}`.
- **Autonomy:** full. Band thresholds are config, never prompt-decided.

### 4.3 Sales Agent (`sales.py`) — Phase 1
- **Triggers:** `lead.qualified.sql`, `reply.received`, `followup.due`, `meeting.requested`.
- **LLM tasks:** `research_account` (synthesize everything known + semantic memory into a brief), `draft_initial_outreach`, `draft_followup`, `classify_reply` (intent: interested / objection / not-now / unsubscribe / OOO / referral), `handle_objection` (draft response using retrieved past objection-handling wins), `draft_proposal`.
- **Tools:** Gmail send (gated), Google Calendar (availability + booking), semantic memory retrieval.
- **Sequence state machine** (`orchestrator/sequences.py`): configurable steps (e.g., D0 initial → D3 follow-up 1 → D7 follow-up 2 → D14 breakup), auto-paused on any reply, resumed or terminated by reply classification. Sequence definitions live in config, not code.
- **Hard gates (code-level, not prompt-level):**
  - Daily send cap per mailbox from `thresholds.yaml` (start: 20/day — see `docs/deliverability.md`).
  - First-touch emails require approval until an operator flips `outreach.autonomy: auto` per campaign in config; follow-ups within an approved sequence run autonomously.
  - Any drafted proposal, pricing mention, or discount → mandatory approval, no config override.
  - `classify_reply` confidence below threshold → human review queue, not a guessed reply.
- **Escalation to human:** interested replies with meeting friction, legal/procurement questions, anger/complaint sentiment, enterprise-size accounts (config threshold).
- **Emits:** `outreach.drafted`, `outreach.sent`, `reply.classified`, `meeting.requested`, `objection.logged`, `deal.stage_changed`.

### 4.4 Meeting Intelligence Agent (`meeting_intel.py`) — Phase 2
- **Triggers:** `meeting.completed` (calendar webhook + transcript available), manual transcript upload endpoint.
- **LLM tasks:** `summarize_meeting`, `extract_insights` (action items, objections, buying signals, competitors mentioned, urgency, next steps — one structured output), `score_deal_health` (0–100 with reasons).
- **Tools:** transcription provider interface (v1: accept uploaded transcripts/VTT from Zoom/Meet's own transcription; a bot service like Recall.ai is a later adapter — do not build recording infrastructure).
- **Emits:** `meeting.analyzed` → CRM sync consumes it; objections flow into the `objections` table with deal linkage; transcript chunks embedded into semantic memory.
- **Autonomy:** full (read/analyze only; all writes go through CRM sync).

### 4.5 CRM Sync Agent (`crm_sync.py`) — Phase 2
- **Role:** not a CRM — a consistency service over our Postgres. Consumes every entity-affecting event and performs idempotent upserts, activity logging, and pipeline stage transitions.
- **Dedup:** deterministic first (email exact, domain + fuzzy name via trigram similarity above threshold). Only genuinely ambiguous cases invoke the `dedup_judgment` LLM task, and merges above a config confidence bar still queue for approval. **Record deletion is always human-approved. No exceptions.**
- **External CRM:** `integrations/crm_external.py` defines the interface (`upsert_contact`, `upsert_company`, `log_activity`, `update_deal`). V1 ships a no-op implementation. HubSpot/Attio adapters are additive later.
- **Emits:** `crm.updated`, `crm.merge_proposed`.

### 4.6 Learning Agent (`learning.py`) — Phase 3
- **Triggers:** `deal.closed.{won|lost}` (single-deal analysis), weekly schedule (cohort analysis).
- **Design:** metrics are **computed in SQL, interpreted by LLM.** Reply rates, conversion by industry, cycle duration, objection frequency, campaign attribution — all deterministic queries in `repositories`. The LLM tasks (`analyze_win_loss`, `analyze_objections`, `generate_recommendations`) receive computed metrics + retrieved conversation excerpts and produce structured findings and recommendations, each with cited evidence (deal IDs, message IDs).
- **Emits:** `learning.published` containing typed recommendations: `messaging.update_proposed`, `icp.update_proposed`, `sequence.update_proposed`. **Recommendations never auto-apply.** They are proposals consumed by Marketing (Phase 4) or approved by the operator; approved messaging updates are embedded into semantic memory tagged as current-best-practice so Sales drafting retrieves them.
- **Answers the spec's questions** via a small set of saved queries + a `/learning/ask` endpoint that maps natural-language questions to the metrics layer.

### 4.7 Market Intelligence Agent (`market_intel.py`) — Phase 4
- **Triggers:** weekly schedule per industry pack; manual research request.
- **LLM tasks:** `research_market` (web search synthesis → trends, news, audience opportunities), `analyze_competitor` (structured competitor profile), `propose_icp_update` (diff against current industry pack).
- **Tools:** web search, web fetch, semantic memory (store + retrieve prior research).
- **Autonomy:** research is autonomous; **ICP changes are always approval-gated** because they alter scoring for every future lead.
- **Emits:** `market.insight_published`, `icp.update_proposed`.

### 4.8 Marketing Strategy Agent (`marketing.py`) — Phase 4
- **Triggers:** `learning.published`, `market.insight_published`, schedule: campaign review; manual campaign request.
- **LLM tasks:** `generate_campaign` (concept + audience + channel plan + success metrics), `draft_email_sequence`, `draft_linkedin_post`, `draft_blog_outline`.
- **Metrics tracked** (computed in SQL from `messages`/`campaigns`): open rate*, CTR, reply rate, cost per lead, lead→SQL rate, SQL→deal rate, revenue per campaign. (*Open tracking is unreliable post–Mail Privacy Protection; treat reply rate as the primary signal and say so in dashboards.)
- **Hard gates:** campaign launches above a config audience-size threshold require approval; all ad spend requires approval (v1 has no ad platform integration — LinkedIn/Meta adapters are future work behind an interface).
- **Emits:** `campaign.published`, `campaign.assets_created`.

---

## 5. Data Model & Schemas

### 5.1 Core tables (migration 0001)
`companies`, `contacts`, `leads` (contact+campaign context, status, band), `lead_scores` (append-only score history with component breakdown), `campaigns`, `campaign_assets`, `sequences`, `sequence_runs`, `messages` (every email in/out: direction, thread_id, gmail_id, classification, campaign linkage), `meetings`, `meeting_insights`, `deals`, `pipeline_stages` (config-seeded), `activities` (unified timeline), `objections` (text, category, deal_id, resolution, outcome), `learnings` (findings + recommendations, status: proposed/approved/rejected), `embeddings` (pgvector: kind, ref_table, ref_id, chunk, vector, industry), `events` (outbox: type, payload JSONB, created_at, processed_at), `jobs` (queue: type, payload, status, run_after, attempts, locked_by), `approvals` (action_type, payload, requested_by_agent, status, decided_by, token, expires_at), `agent_runs` (agent, trigger_event, trace_id, cost, latency, status).

**Conventions:** UUID PKs; `created_at`/`updated_at` everywhere; soft delete (`deleted_at`) on entities; every AI-derived field stores provenance (`source`, `model`, `run_id`); JSONB payloads always schema-validated in code before insert.

### 5.2 JSON Schemas (`schemas/`)
- One schema per event type in `schemas/events/` (e.g., `lead.qualified.sql.json`). `events.py` refuses to emit an event whose payload doesn't validate.
- One schema per LLM output in `schemas/outputs/`, named to match its prompt file. Example — `outputs/reply_classification.json`:

```json
{
  "type": "object",
  "required": ["intent", "confidence", "reasoning", "suggested_action"],
  "properties": {
    "intent": {"enum": ["interested", "objection", "not_now", "unsubscribe", "out_of_office", "referral", "unclear"]},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "reasoning": {"type": "string", "maxLength": 600},
    "objection_category": {"enum": ["price", "timing", "trust", "need", "authority", "competitor", null]},
    "suggested_action": {"enum": ["send_followup", "book_meeting", "pause_sequence", "escalate_human", "close_lost"]}
  },
  "additionalProperties": false
}
```

`additionalProperties: false` on every output schema. Enums over free text wherever a downstream branch depends on the value.

---

## 6. Prompt File Conventions (`prompts/_conventions.md`)

Every prompt file is Markdown with YAML frontmatter:

```markdown
---
id: sales/draft_initial_outreach
tier: standard                  # fast | standard | deep — resolved via config/base.yaml
output_schema: outputs/outreach_draft.json
max_tokens: 1200
variables: [prospect_profile, account_brief, campaign_angle, similar_wins, industry_voice]
version: 3
---
# Role
You write first-touch emails for {{industry_voice.sender_persona}}...

# Context
{{prospect_profile}}
...

# Rules
- Under 120 words. One specific observation about their business. One clear CTA.
- Never invent facts not present in the context above.

# Output
Respond with JSON matching the output schema. No prose outside JSON.
```

Rules:
- Never hardcode a model name in a prompt file. `tier` maps to a model in
  `config/base.yaml`, so changing models is one config edit, not ten file edits.
- Version bumps on any material change; `agent_runs` logs the version used, so performance can be compared across prompt versions.
- Industry voice/vocabulary comes from the config pack via variables — prompts never hardcode a vertical.
- Every prompt has at least one golden test (§8).

---

## 7. Integrations & Environment

| Concern | V1 implementation | Interface for later |
|---|---|---|
| Email send/receive | Gmail API. **Polling** `history.list` (~2 min), NOT Pub/Sub push | SMTP/Outlook adapter |
| Calendar | Google Calendar API (availability + event create) | Cal.com adapter |
| Approvals & digest | Slack (buttons hitting `/approvals` endpoints); email fallback | — |
| Enrichment | Manual import + structured web research | Apollo / Clay adapter |
| Prospect discovery | `ManualCsvProvider` behind `integrations/prospecting.py` | Explorium / Apollo adapter |
| Transcripts | Upload endpoint (accept Zoom/Meet native transcripts) | Recall.ai adapter |
| External CRM | No-op | HubSpot / Attio adapter |
| Observability | Langfuse (traces, cost per run, prompt versions) | OTel exporter |
| LLM | Anthropic API via `core/llm.py` | OpenAI/Gemini behind same interface |

**Dev-time MCP servers for the coding agent:** Postgres MCP (inspect schema/data while building), filesystem, Gmail MCP (manual testing only against a dev account). These never appear in `pyproject.toml`.

**Why polling, not push (supersedes the original Pub/Sub design):** polling
`history.list` every ~2 minutes sits inside quota, and a 2-minute delay on cold-outreach
replies is immaterial. It removes Pub/Sub setup, removes the 7-day watch-renewal job, and
makes reconciliation the primary path rather than an untested backup. Combined with Slack
**Socket Mode** (outbound WebSocket), the system needs **no public HTTP endpoint at all** —
no TLS, no reverse proxy, no exposed surface. Deployment is one small VPS running
docker-compose: workers, scheduler, Postgres. FastAPI stays in the codebase, unexposed,
for the webform later.

**Environment:** all secrets via env vars, documented in `.env.example`. Separate Google Cloud project + OAuth credentials for dev vs prod. Outreach in dev mode routes all email to a sandbox address (enforced in `gmail.py` when `ENV != production`).

---

## 8. Testing Strategy

1. **Unit** (`tests/unit/`): scoring arithmetic, band classification, sequence state machine transitions, dedup rules, config loading + validation, approval gate logic. No network, no LLM.
2. **Contract** (`tests/contracts/`): every file in `schemas/` is valid JSON Schema; every prompt frontmatter references an existing schema; every event emitted anywhere in `src/` has a schema (enforced by scanning the event registry).
3. **Golden** (`tests/golden/`): fixture input → real LLM call → assert output validates + key field assertions (e.g., a fixture reply saying "too expensive right now" must classify `intent: objection`, `objection_category: price`). Run on demand and in CI nightly (they cost money); record pass rates per prompt version.
4. **Integration** (`tests/integration/`): full event flows through the real queue against a test database with mocked integrations — including the negative paths: send cap reached blocks sending; missing approval token blocks proposal delivery; reply mid-sequence pauses the sequence; dedup prevents duplicate contact on re-import.
5. **Failure drills** documented in `runbook.md`: LLM validation failures dead-letter correctly; Gmail webhook outage recovers via reconciliation job; a poison job doesn't block the queue.

CI gates: lint (ruff), type-check (mypy strict on `core/` and `db/`), unit + contract + integration on every PR. Golden nightly.

---

## 9. Industry Reusability (config packs)

`config/industries/<vertical>.yaml` defines everything vertical-specific:

```yaml
name: expert-entrepreneurs
icp:
  firmographics: {industries: [...], size_range: [1, 20], revenue_signals: [...]}
  roles: {target_titles: [...], seniority_weight: {...}}
  disqualifiers: [...]
scoring:
  weights: {icp_match: 0.35, intent: 0.25, engagement: 0.25, size_fit: 0.15}
  bands: {sql: 80, mql: 60, warm: 40}
voice:
  sender_persona: "..."
  tone_rules: [...]
  vocabulary: {say: [...], avoid: [...]}
sequences:
  default: {steps: [{day: 0, prompt: draft_initial_outreach}, {day: 3, ...}]}
channels: {email: true, linkedin: false}
```

Deploying to a new industry = new YAML file + reviewed golden fixtures. Zero code changes. The config loader validates packs against `schemas/entities/industry_pack.json` at startup and refuses to boot on an invalid pack.

---

## 10. Build Order for the Coding Agent (milestones)

Work in this exact order. Each milestone ends with passing tests and an entry in `docs/decisions.md` for any judgment calls made.

1. **M0.1** Scaffold repo per §2, `pyproject.toml`, docker-compose (Postgres+pgvector), Makefile, CI skeleton.
2. **M0.2** Migrations 0001 (all core tables), `db/models.py`, `db/repositories.py` for companies/contacts/leads/events/jobs.
3. **M0.3** `core/events.py` + `core/queue.py` (outbox pattern, SKIP LOCKED worker, retries with backoff, dead-letter). Integration test: emit → route → handle.
4. **M0.4** `core/llm.py` with schema validation + retry-on-invalid + Langfuse tracing. `core/config.py` with industry pack validation. Golden-test harness.
5. **M1.1** Leadgen agent: capture webhook + manual import, enrichment interface + prompts, provenance storage. 
6. **M1.2** Qualification agent: deterministic scoring + LLM sub-scores, bands, re-scoring on engagement events.
7. **M1.3** `core/approvals.py` + Slack integration + `/approvals` endpoints. Test: gated action blocks without token.
8. **M1.4** Sales agent: account research, outreach drafting, Gmail send behind caps + gates, reply webhook + classification, sequence state machine, calendar booking. End-to-end Phase 1 acceptance test.
9. **M2.1** Meeting intel: transcript upload, analysis prompts, embedding into memory.
10. **M2.2** CRM sync: idempotent upserts, dedup (deterministic + judgment), stage transitions, activity timeline. Phase 2 acceptance test.
11. **M3.1** Metrics layer (SQL) + learning prompts + weekly job + `learning.published` recommendations. Phase 3 acceptance with fixture deals.
12. **M4.1** Market intel research jobs + ICP proposal flow. 
13. **M4.2** Marketing campaign generation + asset storage + campaign metrics + consumption of learning recommendations. Full-loop acceptance test: campaign → lead → deal → learning → campaign update proposal.

---

## 11. Documentation Requirements

- `README.md`: 5-minute orientation — what it is, architecture diagram, how to run locally, where things live.
- `docs/decisions.md`: append-only. Every deviation from this spec, every judgment call, dated.
- `docs/agent-contracts.md`: regenerate whenever a contract changes; this is the operator's reference.
- `docs/deliverability.md`: send caps and warmup schedule (start 20/day/mailbox, +10/week to max 50), separate sending domain from the primary brand domain, SPF/DKIM/DMARC setup, unsubscribe handling (mandatory one-click, suppression list enforced in `gmail.py`), reply-rate monitoring with automatic cap reduction if bounce rate exceeds 2%.
- `docs/runbook.md`: deploy steps, key rotation, dead-letter recovery, webhook re-registration, "the queue is stuck" playbook.

---

## 12. Out of Scope for V1 (explicitly)

Do not build: ad platform integrations, a web UI beyond approval endpoints (Slack is the UI), LinkedIn automation (ToS risk — LinkedIn content is drafted for manual posting), call recording bots, multi-tenant support, external CRM sync implementations, local LLM support. All of these have interfaces or config stubs where noted, and nothing more.
