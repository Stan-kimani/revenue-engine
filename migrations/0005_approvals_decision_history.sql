-- 0005_approvals_decision_history.sql — M1.3: the human-in-the-loop gate
-- (CLAUDE.md §1 non-negotiable 8, agent-contracts.md §0.4 A2/A3).
--
-- ALTERs the `approvals` table migrations/0001_init.sql already created
-- (matching event-catalog.md §7.1's expiry-policy action_type list exactly)
-- rather than redefining it — migrations are forward-only (CLAUDE.md §3).
-- Its existing `status` vocabulary ('pending'/'granted'/'denied'/'expired')
-- and `action_type` CHECK are kept as-is; they already agree with
-- event-catalog.md's `approval.granted`/`approval.denied` event names and
-- orchestrator/router.py's existing references.

-- Audit columns the M1.3 plan needs and migrations/0001 didn't have:
--   decision_reason — the human's stated reason for a denial (event-catalog.md
--     §3: approval.denied's payload carries `reason`; nothing durable stored
--     it before this).
--   correlation_id / causation_id — same envelope discipline every event
--     already carries (event-catalog.md §R3), so an approval row's place in
--     a lead's full journey is traceable without a join through `events`.
--   dedupe_key — a caller-supplied logical key for "this exact action,
--     already pending" (e.g. "outreach_draft:{lead_id}:{sequence_step}").
--     Nullable: not every action_type has a natural single dedupe key, and a
--     NULL dedupe_key is explicitly excluded from the uniqueness check below
--     rather than treated as one shared "no key" bucket.
ALTER TABLE approvals
    ADD COLUMN decision_reason text,
    ADD COLUMN correlation_id  uuid,
    ADD COLUMN causation_id    uuid,
    ADD COLUMN dedupe_key      text;

-- Two pending approvals for the same logical action are a real business bug
-- (e.g. a retried job requesting approval for the same draft twice) — a
-- structural constraint, not a check-then-act race in application code, same
-- pattern as migrations/0001's one_active_lead_per_company /
-- migrations/0004's one_deferred_lead_per_contact.
CREATE UNIQUE INDEX one_pending_approval_per_dedupe_key
    ON approvals (dedupe_key)
    WHERE status = 'pending' AND dedupe_key IS NOT NULL;

-- Append-only decision history (M1.3 plan deliverable 2): a resolved
-- approval is never re-decided, enforced in the database, not only by
-- core/approvals.py's own `UPDATE ... WHERE status = 'pending'` guard.
-- That guard alone is sufficient for the code paths that use it (a second
-- resolve() attempt affects zero rows, handled as "already decided" rather
-- than raising) — this trigger is the backstop against any OTHER write path
-- that forgets it, deliberate or accidental (the milestone's own framing:
-- "Design it so that a future agent cannot route around it even by
-- accident"). It blocks ANY update to a row whose status has already left
-- 'pending', not just a status change — once decided, the row is immutable.
CREATE FUNCTION forbid_redecision_of_approval() RETURNS trigger AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION
            'approval % already decided (status=%), cannot be modified',
            OLD.id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER approvals_forbid_redecision
    BEFORE UPDATE ON approvals
    FOR EACH ROW
    EXECUTE FUNCTION forbid_redecision_of_approval();
