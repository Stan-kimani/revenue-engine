-- 0004_one_deferred_lead_per_contact.sql — close a structural gap in the
-- single-thread rule's deferred path (M1.1, docs/decisions.md).
--
-- migrations/0001_init.sql's one_active_lead_per_contact / _per_company both
-- exclude status='deferred' from their predicate, deliberately: a deferred
-- placeholder must never collide with the ACTIVE-lead constraint it exists to
-- route around (see the comment above those two indexes). But excluding
-- 'deferred' from those indexes also means NOTHING previously constrained two
-- deferred rows for the SAME contact — repositories.create_lead()'s deferred
-- branch was a plain INSERT with no conflict target at all. Two concurrent
-- callers hitting the same already-occupied company for the same contact
-- (e.g. two overlapping runs of scripts/import_leads.py against the same
-- CSV) could both pass any application-level check and both successfully
-- INSERT a deferred row — a real, DB-observable duplicate, not merely a
-- theoretical race, since neither existing index's predicate excludes a
-- SECOND row with status='deferred'.
--
-- This index closes that gap the same way D2/R1 closed the active-lead one:
-- structurally, not by trusting application code to check first. A
-- check-then-act guard in scripts/import_leads.py is kept as an
-- optimisation (avoids doing enrichment/DB work for a row that's already
-- imported) but is explicitly NOT the thing preventing the duplicate —
-- this index is.
--
-- Predicate matches repositories.create_lead()'s new
-- `ON CONFLICT (contact_id) WHERE status = 'deferred' AND deleted_at IS NULL`
-- clause exactly — Postgres requires the conflict target's WHERE clause to
-- match an existing partial unique index's predicate verbatim for ON
-- CONFLICT inference to resolve to it.

CREATE UNIQUE INDEX one_deferred_lead_per_contact
    ON leads (contact_id)
    WHERE status = 'deferred'
      AND deleted_at IS NULL;
