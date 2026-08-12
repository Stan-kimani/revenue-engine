-- 0002_agent_runs_llm_fields.sql — additive columns core/llm.py::complete_json()
-- needs on `agent_runs` (M0.4).
--
-- build-spec §5.1 / entity-model.md §7 locked agent_runs' shape as (agent,
-- trigger_event, trace_id, cost, latency, status) — deliberately minimal,
-- generic across every agent. The M0.4 task instruction explicitly requires
-- complete_json() to record "prompt_version, tier, model, token counts" to
-- this table, which that original shape has no columns for. Per CLAUDE.md
-- ("later documents supersede earlier ones; the build spec is the oldest"),
-- and since this was a direct, explicit instruction rather than an inferred
-- one, this migration extends the table rather than pushing those fields
-- onto some other row. See docs/decisions.md.
--
-- Plain ALTER TABLE ADD COLUMN, nullable/defaulted — safe inside a normal
-- transaction, no CONCURRENTLY needed, no backfill (table is at or near zero
-- rows; no agent code exists yet to have written any).

ALTER TABLE agent_runs
    ADD COLUMN prompt_id text,
    ADD COLUMN prompt_version integer,
    ADD COLUMN tier text CHECK (tier IN ('fast', 'standard', 'deep')),
    ADD COLUMN model text,
    ADD COLUMN input_tokens integer,
    ADD COLUMN output_tokens integer,
    ADD COLUMN retry_count integer NOT NULL DEFAULT 0;
