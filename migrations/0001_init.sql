-- 0001_init.sql — full schema: domain entities (entity-model.md §3) and
-- infrastructure tables (entity-model.md §7, build-spec §5.1).
--
-- Structure of this file, in order:
--   1. Extensions
--   2. CREATE TABLE (columns, PKs, inline CHECK constraints — no FKs yet)
--   3. Indexes, including the single-thread partial unique indexes
--   4. Foreign keys, added last so table creation order never has to fight
--      circular references (leads.deal_id <-> deals.lead_id, in particular)
--
-- Closed-vocabulary columns are `text` + CHECK, not native Postgres ENUM
-- types: adding or removing a value is then a plain ALTER TABLE ... DROP/ADD
-- CONSTRAINT, which runs inside a normal transaction, matching how the
-- objection vocabulary has already changed once (7 -> 8 categories, see
-- docs/decisions.md 2026-07-28). scripts/migrate.py's `-- migrate:
-- no-transaction` convention exists for DDL that genuinely cannot run inside
-- a transaction (CREATE INDEX CONCURRENTLY and similar), not for enum
-- changes under this design.
--
-- gen_random_uuid() is built into Postgres core since v13 — no pgcrypto
-- extension needed.

-- ============================================================================
-- 1. Extensions
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- 2. Domain tables (entity-model.md §3)
-- ============================================================================

CREATE TABLE companies (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    domain        citext UNIQUE,
    name          text NOT NULL,
    linkedin_url  text,
    country       text,
    employee_band text,
    attributes    jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);

CREATE TABLE contacts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         citext NOT NULL UNIQUE,
    email_status  text NOT NULL DEFAULT 'unverified'
                  CHECK (email_status IN ('unverified', 'valid', 'risky', 'invalid', 'bounced', 'suppressed')),
    full_name     text,
    first_name    text,
    last_name     text,
    title         text,
    linkedin_url  text,
    company_id    uuid,
    attributes    jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);

CREATE TABLE campaigns (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL,
    industry_pack text NOT NULL,
    channel       text NOT NULL CHECK (channel IN ('email', 'linkedin', 'multi')),
    status        text NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'pending_approval', 'active', 'paused', 'completed', 'archived')),
    sequence_key  text,
    goal          jsonb,
    approved_by   text,
    approved_at   timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);

CREATE TABLE leads (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id         uuid NOT NULL,
    company_id         uuid,
    campaign_id        uuid,
    industry_pack      text NOT NULL,
    source             text NOT NULL
                       CHECK (source IN ('webform', 'manual_import', 'discovery', 'referral', 'inbound_reply')),
    status             text NOT NULL DEFAULT 'new'
                       CHECK (status IN (
                           'new', 'deferred', 'enriching', 'enrich_failed', 'scored', 'qualified',
                           'engaged', 'meeting_booked', 'converted', 'disqualified', 'unsubscribed', 'dormant'
                       )),
    band               text CHECK (band IN ('cold', 'warm', 'mql', 'sql')),
    current_score      numeric(5, 2),
    deal_id            uuid,
    budget_band        text CHECK (budget_band IN ('unknown', 'under_5k', '5k_15k', '15k_40k', '40k_plus')),
    budget_source      text CHECK (budget_source IN ('self_reported', 'inferred', 'discovery_call')),
    problem_statement  text,
    pain_category      text
                       CHECK (pain_category IN (
                           'manual_data_entry', 'slow_followup', 'reporting_visibility',
                           'intake', 'reconciliation', 'other'
                       )),
    team_size_band     text CHECK (team_size_band IN ('solo', '2_10', '11_50', '50_plus')),
    first_touched_at   timestamptz,
    last_activity_at   timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    deleted_at         timestamptz,
    CONSTRAINT problem_statement_required_for_inbound CHECK (
        source NOT IN ('webform', 'inbound_reply', 'referral') OR problem_statement IS NOT NULL
    )
);

-- Append-only score history (entity-model.md §3.5). Never updated, never
-- deleted — no updated_at/deleted_at columns.
CREATE TABLE lead_scores (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id            uuid NOT NULL,
    total              numeric(5, 2) NOT NULL,
    band               text NOT NULL CHECK (band IN ('cold', 'warm', 'mql', 'sql')),
    components         jsonb NOT NULL,
    deterministic_part numeric(5, 2),
    llm_part           numeric(5, 2),
    prompt_version     integer,
    model              text,
    run_id             uuid,
    scored_at          timestamptz NOT NULL DEFAULT now()
);

-- Multi-currency columns per entity-model.md §8.2. value_base_amount is a
-- plain stored column, not a SQL GENERATED column: a GENERATED column always
-- recomputes from its formula, which would defeat freezing fx_rate_to_base at
-- close. close_deal() in repositories.py is responsible for freezing it.
CREATE TABLE deals (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id            uuid NOT NULL,
    contact_id         uuid,
    company_id         uuid,
    -- Not a hard FK to pipeline_stages.key — entity-model.md §3.6 calls this
    -- "FK-ish"; stage transitions are validated in code against a config
    -- transition table, not enforced by a DB constraint.
    stage              text NOT NULL,
    value_amount       numeric(14, 2),
    currency           char(3),
    fx_rate_to_base    numeric(18, 8),
    value_base_amount  numeric(14, 2),
    fx_rate_source     text,
    fx_frozen_at       timestamptz,
    expected_close_at  date,
    outcome            text CHECK (outcome IN ('won', 'lost')),
    lost_reason        text,
    health_score       integer CHECK (health_score IS NULL OR health_score BETWEEN 0 AND 100),
    closed_at          timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    deleted_at         timestamptz
);

CREATE TABLE messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id             uuid,
    contact_id          uuid,
    campaign_id         uuid,
    direction           text NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    channel             text NOT NULL CHECK (channel IN ('email', 'linkedin', 'other')),
    provider_message_id text UNIQUE,
    thread_id           text,
    subject             text,
    body_text           text,
    sequence_step       integer,
    prompt_version      integer,
    classification      jsonb,
    approval_id         uuid,
    sent_at             timestamptz,
    received_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

CREATE TABLE meetings (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id           uuid,
    deal_id           uuid,
    contact_id        uuid,
    external_event_id text UNIQUE,
    scheduled_at      timestamptz,
    occurred_at       timestamptz,
    duration_s        integer,
    transcript_url    text,
    transcript_text   text,
    status            text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    deleted_at        timestamptz
);

-- Split from meetings so a transcript can be re-analysed by a newer prompt
-- without destroying the original reading (entity-model.md §3.8).
CREATE TABLE meeting_insights (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id           uuid NOT NULL,
    summary              text,
    action_items         jsonb,
    buying_signals       jsonb,
    urgency              text CHECK (urgency IN ('high', 'medium', 'low')),
    competitors_mentioned jsonb,
    deal_health_score    integer CHECK (deal_health_score IS NULL OR deal_health_score BETWEEN 0 AND 100),
    run_id               uuid,
    prompt_version       integer,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    deleted_at           timestamptz
);

-- Closed set of 8 categories, identical to reply_classification.json and
-- objection_response.json (entity-model.md §3.9). `handoff` is the dominant
-- unspoken objection in this category per docs/competitive-deltas.md D2.
CREATE TABLE objections (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id         uuid,
    lead_id         uuid NOT NULL,
    message_id      uuid,
    meeting_id      uuid,
    category        text NOT NULL
                    CHECK (category IN ('price', 'timing', 'trust', 'need', 'authority', 'competitor', 'handoff', 'other')),
    verbatim        text,
    response_given  text,
    resolved        boolean,
    outcome         text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);

-- Unified timeline (entity-model.md §3.10). Append-only, written by CRM Sync
-- only — no updated_at/deleted_at.
CREATE TABLE activities (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL,
    entity_id   uuid NOT NULL,
    lead_id     uuid,
    kind        text NOT NULL,
    summary     text,
    ref_table   text,
    ref_id      uuid,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL
);

-- ============================================================================
-- 3. Infrastructure tables (entity-model.md §7, build-spec §5.1)
-- ============================================================================

-- Outbox. Envelope per event-catalog.md §R3: event_id, type, version,
-- occurred_at, actor, correlation_id, causation_id, idempotency_key, payload.
-- processed_at is the operational addition from build-spec §5.1 for outbox
-- polling. Immutable audit log (event-catalog.md §R1) — never updated, never
-- deleted, no updated_at/deleted_at.
CREATE TABLE events (
    event_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type            text NOT NULL,
    version         integer NOT NULL DEFAULT 1,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    actor           text NOT NULL,
    correlation_id  uuid NOT NULL,
    causation_id    uuid,
    idempotency_key text NOT NULL,
    payload         jsonb NOT NULL,
    processed_at    timestamptz
);

CREATE TABLE jobs (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type       text NOT NULL,
    payload    jsonb NOT NULL,
    status     text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'running', 'completed', 'failed', 'dead_letter')),
    run_after  timestamptz NOT NULL DEFAULT now(),
    attempts   integer NOT NULL DEFAULT 0,
    locked_by  text,
    locked_at  timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- action_type vocabulary matches the expiry policy table in
-- event-catalog.md §7.1.
CREATE TABLE approvals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_type       text NOT NULL
                      CHECK (action_type IN (
                          'outreach_draft', 'sequence_step', 'proposal_send', 'pricing_discount',
                          'campaign_launch', 'icp_update', 'crm_merge', 'record_delete'
                      )),
    payload           jsonb NOT NULL,
    requested_by_agent text,
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'granted', 'denied', 'expired')),
    decided_by        text,
    token             text UNIQUE,
    expires_at        timestamptz,
    decided_at        timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent         text NOT NULL,
    trigger_event uuid,
    trace_id      text,
    cost          numeric(10, 4),
    latency_ms    integer,
    status        text CHECK (status IN ('success', 'failed')),
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- vector has no fixed dimension yet — deliberate. No embedding provider is
-- specified anywhere in the docs (build-spec §7's Integrations table covers
-- only the completions LLM), and nothing before Phase 2/3 (core/memory.py,
-- the first embed() call) generates embeddings. A fixed dimension and its
-- ivfflat/hnsw index are added in a later migration once a real provider is
-- chosen — see docs/decisions.md.
CREATE TABLE embeddings (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       text NOT NULL,
    ref_table  text NOT NULL,
    ref_id     uuid NOT NULL,
    chunk      text NOT NULL,
    vector     vector,
    industry   text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Config-seeded (build-spec §5.1), not by this migration — seeding is
-- application/seed-script data, not schema (CLAUDE.md §6: "Seed data only via
-- scripts/seed_dev.py").
CREATE TABLE pipeline_stages (
    key        text PRIMARY KEY,
    label      text NOT NULL,
    sort_order integer NOT NULL,
    is_won     boolean NOT NULL DEFAULT false,
    is_lost    boolean NOT NULL DEFAULT false
);

CREATE TABLE sequences (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key        text NOT NULL UNIQUE,
    name       text,
    steps      jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- States per agent-contracts.md §3 Sales agent: pending -> active -> paused
-- -> completed | terminated.
CREATE TABLE sequence_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence_id   uuid NOT NULL,
    lead_id       uuid NOT NULL,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'active', 'paused', 'completed', 'terminated')),
    current_step  integer NOT NULL DEFAULT 0,
    resume_after  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE campaign_assets (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('email_sequence', 'linkedin_post', 'blog_outline')),
    content     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE learnings (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           text NOT NULL CHECK (scope IN ('weekly', 'deal')),
    period_start    date,
    period_end      date,
    findings        jsonb,
    recommendations jsonb,
    status          text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'rejected')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Provider-agnostic (entity-model.md §8.3). Seeded manually or via scripts/
-- in v1; a live FX API is an adapter behind this same table later.
CREATE TABLE fx_rates (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    base_currency  char(3) NOT NULL,
    quote_currency char(3) NOT NULL,
    rate           numeric(18, 8) NOT NULL,
    as_of          date NOT NULL,
    source         text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (base_currency, quote_currency, as_of)
);

-- ============================================================================
-- 4. Indexes
-- ============================================================================

CREATE INDEX companies_employee_band_idx ON companies (employee_band);
-- Trigram similarity for CRM Sync's fuzzy company dedup (agent-contracts.md §5).
CREATE INDEX companies_name_trgm_idx ON companies USING gin (name gin_trgm_ops);

CREATE INDEX contacts_company_id_idx ON contacts (company_id);
CREATE INDEX contacts_email_status_idx ON contacts (email_status);

CREATE INDEX leads_contact_id_idx ON leads (contact_id);
CREATE INDEX leads_company_id_idx ON leads (company_id);
CREATE INDEX leads_campaign_id_idx ON leads (campaign_id);
CREATE INDEX leads_status_idx ON leads (status);
CREATE INDEX leads_band_idx ON leads (band);

-- D2 / R1: exactly one active lead per contact, and per company. "Active"
-- means not in a terminal status. Enforced in the DB, not application logic.
CREATE UNIQUE INDEX one_active_lead_per_contact
    ON leads (contact_id)
    WHERE status NOT IN ('converted', 'disqualified', 'unsubscribed', 'dormant')
      AND deleted_at IS NULL;

CREATE UNIQUE INDEX one_active_lead_per_company
    ON leads (company_id)
    WHERE status NOT IN ('converted', 'disqualified', 'unsubscribed', 'dormant')
      AND deleted_at IS NULL
      AND company_id IS NOT NULL;

CREATE INDEX lead_scores_lead_id_idx ON lead_scores (lead_id);

CREATE INDEX deals_lead_id_idx ON deals (lead_id);
CREATE INDEX deals_stage_idx ON deals (stage);

CREATE INDEX messages_lead_id_idx ON messages (lead_id);
CREATE INDEX messages_thread_id_idx ON messages (thread_id);

CREATE INDEX meetings_lead_id_idx ON meetings (lead_id);
CREATE INDEX meetings_deal_id_idx ON meetings (deal_id);

CREATE INDEX meeting_insights_meeting_id_idx ON meeting_insights (meeting_id);

CREATE INDEX objections_deal_id_idx ON objections (deal_id);
CREATE INDEX objections_lead_id_idx ON objections (lead_id);
CREATE INDEX objections_category_idx ON objections (category);

CREATE INDEX activities_lead_id_idx ON activities (lead_id);
CREATE INDEX activities_entity_idx ON activities (entity_type, entity_id);

-- Re-emission is a no-op, not an error (event-catalog.md §R3 / §9.4).
CREATE UNIQUE INDEX events_idempotency_key_idx ON events (idempotency_key);
CREATE INDEX events_type_idx ON events (type);
CREATE INDEX events_correlation_id_idx ON events (correlation_id);
CREATE INDEX events_unprocessed_idx ON events (processed_at) WHERE processed_at IS NULL;

-- Supports the SELECT ... FOR UPDATE SKIP LOCKED worker poll (build-spec §2).
CREATE INDEX jobs_poll_idx ON jobs (status, run_after);
CREATE INDEX jobs_type_idx ON jobs (type);

CREATE UNIQUE INDEX approvals_token_idx ON approvals (token) WHERE token IS NOT NULL;
CREATE INDEX approvals_status_idx ON approvals (status);

CREATE INDEX agent_runs_trigger_event_idx ON agent_runs (trigger_event);

CREATE INDEX embeddings_ref_idx ON embeddings (ref_table, ref_id);
CREATE INDEX embeddings_kind_idx ON embeddings (kind);
CREATE INDEX embeddings_industry_idx ON embeddings (industry);

CREATE INDEX sequence_runs_sequence_id_idx ON sequence_runs (sequence_id);
CREATE INDEX sequence_runs_lead_id_idx ON sequence_runs (lead_id);

CREATE INDEX campaign_assets_campaign_id_idx ON campaign_assets (campaign_id);

-- ============================================================================
-- 5. Foreign keys (added last — see header note)
-- ============================================================================

ALTER TABLE contacts
    ADD CONSTRAINT contacts_company_id_fkey FOREIGN KEY (company_id) REFERENCES companies (id);

ALTER TABLE leads
    ADD CONSTRAINT leads_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts (id),
    ADD CONSTRAINT leads_company_id_fkey FOREIGN KEY (company_id) REFERENCES companies (id),
    ADD CONSTRAINT leads_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES campaigns (id),
    ADD CONSTRAINT leads_deal_id_fkey FOREIGN KEY (deal_id) REFERENCES deals (id);

ALTER TABLE lead_scores
    ADD CONSTRAINT lead_scores_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES leads (id),
    ADD CONSTRAINT lead_scores_run_id_fkey FOREIGN KEY (run_id) REFERENCES agent_runs (id);

ALTER TABLE deals
    ADD CONSTRAINT deals_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES leads (id),
    ADD CONSTRAINT deals_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts (id),
    ADD CONSTRAINT deals_company_id_fkey FOREIGN KEY (company_id) REFERENCES companies (id);

ALTER TABLE messages
    ADD CONSTRAINT messages_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES leads (id),
    ADD CONSTRAINT messages_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts (id),
    ADD CONSTRAINT messages_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES campaigns (id),
    ADD CONSTRAINT messages_approval_id_fkey FOREIGN KEY (approval_id) REFERENCES approvals (id);

ALTER TABLE meetings
    ADD CONSTRAINT meetings_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES leads (id),
    ADD CONSTRAINT meetings_deal_id_fkey FOREIGN KEY (deal_id) REFERENCES deals (id),
    ADD CONSTRAINT meetings_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts (id);

ALTER TABLE meeting_insights
    ADD CONSTRAINT meeting_insights_meeting_id_fkey FOREIGN KEY (meeting_id) REFERENCES meetings (id),
    ADD CONSTRAINT meeting_insights_run_id_fkey FOREIGN KEY (run_id) REFERENCES agent_runs (id);

ALTER TABLE objections
    ADD CONSTRAINT objections_deal_id_fkey FOREIGN KEY (deal_id) REFERENCES deals (id),
    ADD CONSTRAINT objections_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES leads (id),
    ADD CONSTRAINT objections_message_id_fkey FOREIGN KEY (message_id) REFERENCES messages (id),
    ADD CONSTRAINT objections_meeting_id_fkey FOREIGN KEY (meeting_id) REFERENCES meetings (id);

ALTER TABLE activities
    ADD CONSTRAINT activities_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES leads (id);

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_trigger_event_fkey FOREIGN KEY (trigger_event) REFERENCES events (event_id);

ALTER TABLE sequence_runs
    ADD CONSTRAINT sequence_runs_sequence_id_fkey FOREIGN KEY (sequence_id) REFERENCES sequences (id),
    ADD CONSTRAINT sequence_runs_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES leads (id);

ALTER TABLE campaign_assets
    ADD CONSTRAINT campaign_assets_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES campaigns (id);

-- embeddings.ref_table/ref_id are a deliberate polymorphic reference (chunks
-- from meetings, messages, learnings, etc.) — no single FK target is
-- possible, by design.
