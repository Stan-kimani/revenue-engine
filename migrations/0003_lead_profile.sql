-- 0003_lead_profile.sql — leads.profile jsonb (M1.1).
--
-- The Lead Generation agent's third LLM task, leadgen/build_prospect_profile,
-- produces a structured profile (summary, likely_challenges,
-- personalization_anchors, disqualifying_signals, recommended_angle) that
-- downstream Sales drafting (M1.4) and Qualification's score_lead prompt
-- (M1.2, prompts/qualification/score_lead.md's `prospect_profile` variable)
-- both need to read back across a job/event boundary — the process that
-- generated it is long gone by the time a later handler runs. No existing
-- table or column holds this: entity-model.md never modelled a `leads`
-- attributes/profile column (only companies and contacts have `attributes`).
--
-- Plain ALTER TABLE ADD COLUMN, nullable — safe inside a normal transaction.
-- No CONCURRENTLY needed (leads is a low-row-count table at this stage of the
-- build, and this is an additive nullable column, not a backfill). See
-- docs/decisions.md, M1.1 entry.

ALTER TABLE leads
    ADD COLUMN profile jsonb;

COMMENT ON COLUMN leads.profile IS
    'Schema-validated output of leadgen/build_prospect_profile '
    '(schemas/outputs/prospect_profile.json), plus code-added run_id, '
    'prompt_version, generated_at. NULL until enrichment completes.';
