-- 0006_suppressions_and_send_state.sql — M1.4a: the send path's database state
-- (docs/deliverability.md §4-§6).
--
-- 1. suppressions — address- AND domain-level (§5). contacts.email_status alone
--    cannot express "suppress this whole company domain" or an address with no
--    contact row, so this table is the send-time source of truth; the send gate
--    also still refuses on contacts.email_status (entity-model.md D6).
-- 2. sending_pauses — §6's hard pause. Persistent, not an in-memory flag:
--    sending stays stopped across restarts until a human resumes with a reason.
-- 3. messages send state — messages already records outbound mail
--    (provider_message_id, thread_id, approval_id, sent_at), so no separate
--    sent-messages table. It gains the columns the gate needs: who it is from and
--    to (caps count per sending domain), and an explicit send state machine that
--    makes a double send structurally impossible.

-- ---------------------------------------------------------------------------
-- suppressions
-- ---------------------------------------------------------------------------
-- address IS NULL means the whole `domain` is suppressed. An address row may
-- also carry its domain for reporting; the gate only treats address-NULL rows
-- as domain-wide. expires_at IS NULL means permanent (§5); a non-null value is a
-- temporary pause (soft bounce, out-of-office — written by M1.4b).
CREATE TABLE suppressions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    address    citext,
    domain     citext,
    reason     text NOT NULL CHECK (reason IN (
                   'unsubscribe', 'hard_bounce', 'soft_bounce', 'spam_complaint',
                   'hostile_reply', 'manual'
               )),
    source     text NOT NULL,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT suppression_targets_address_or_domain
        CHECK (address IS NOT NULL OR domain IS NOT NULL)
);

CREATE INDEX suppressions_address_idx ON suppressions (address) WHERE address IS NOT NULL;
CREATE INDEX suppressions_domain_idx ON suppressions (domain) WHERE address IS NULL;
CREATE INDEX suppressions_created_at_idx ON suppressions (created_at);

-- §5: "Permanent, never contactable again." A suppression is never edited or
-- removed by any code path — enforced here, not by convention. A temporary
-- suppression ends by its own expires_at, not by deletion.
CREATE FUNCTION forbid_suppression_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'suppressions are append-only (% on %)', TG_OP, OLD.id
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER suppressions_append_only
    BEFORE UPDATE OR DELETE ON suppressions
    FOR EACH ROW
    EXECUTE FUNCTION forbid_suppression_mutation();

-- ---------------------------------------------------------------------------
-- sending_pauses
-- ---------------------------------------------------------------------------
CREATE TABLE sending_pauses (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sending_domain citext NOT NULL,
    reason         text NOT NULL,
    metrics        jsonb NOT NULL,
    paused_at      timestamptz NOT NULL DEFAULT now(),
    resumed_at     timestamptz,
    resumed_by     text,
    resume_reason  text,
    -- §6: "Resuming requires a manual action and a recorded reason."
    CONSTRAINT resume_requires_actor_and_reason CHECK (
        resumed_at IS NULL
        OR (resumed_by IS NOT NULL AND resume_reason IS NOT NULL AND length(trim(resume_reason)) > 0)
    )
);

CREATE UNIQUE INDEX one_open_pause_per_sending_domain
    ON sending_pauses (sending_domain)
    WHERE resumed_at IS NULL;

-- ---------------------------------------------------------------------------
-- messages: send state
-- ---------------------------------------------------------------------------
-- drafted      -> awaiting approval / a send attempt
-- sending      -> reserved: all gates passed and committed; counts against the
--                 cap from this instant. The Gmail call happens after commit.
-- sent         -> Gmail accepted it; provider_message_id recorded
-- send_failed  -> Gmail definitively rejected it; nothing was delivered
-- send_unknown -> the outcome cannot be known (crash or timeout after
--                 reservation). NEVER retried automatically — a human decides.
-- blocked      -> a terminal gate refused it; send_block_reason says which
ALTER TABLE messages
    ADD COLUMN from_address      citext,
    ADD COLUMN to_address        citext,
    ADD COLUMN send_state        text CHECK (send_state IN (
                                     'drafted', 'sending', 'sent', 'send_failed',
                                     'send_unknown', 'blocked'
                                 )),
    ADD COLUMN send_started_at   timestamptz,
    ADD COLUMN send_block_reason text,
    ADD CONSTRAINT reserved_send_has_start_time CHECK (
        send_state IS NULL
        OR send_state NOT IN ('sending', 'sent', 'send_unknown')
        OR send_started_at IS NOT NULL
    ),
    ADD CONSTRAINT sent_message_has_provider_id CHECK (
        send_state IS DISTINCT FROM 'sent' OR provider_message_id IS NOT NULL
    );

CREATE INDEX messages_send_started_at_idx
    ON messages (send_started_at)
    WHERE send_state IN ('sending', 'sent', 'send_unknown');

-- A sent email cannot be unsent, so the state machine is enforced in the
-- database: no transition may lead back to 'drafted' or out of 'sent', which is
-- what would make a second send of the same row possible. Allowed:
--   NULL -> drafted (or any state on INSERT)
--   drafted -> sending | blocked
--   sending -> sent | send_failed | send_unknown
--   send_unknown -> sent | send_failed   (human reconciliation)
CREATE FUNCTION enforce_message_send_state_transition() RETURNS trigger AS $$
BEGIN
    IF NEW.send_state IS NOT DISTINCT FROM OLD.send_state THEN
        RETURN NEW;
    END IF;
    IF (OLD.send_state IS NULL AND NEW.send_state = 'drafted')
       OR (OLD.send_state = 'drafted' AND NEW.send_state IN ('sending', 'blocked'))
       OR (OLD.send_state = 'sending' AND NEW.send_state IN ('sent', 'send_failed', 'send_unknown'))
       OR (OLD.send_state = 'send_unknown' AND NEW.send_state IN ('sent', 'send_failed'))
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'message % send_state % -> % is not allowed', OLD.id, OLD.send_state, NEW.send_state
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER messages_send_state_transition
    BEFORE UPDATE OF send_state ON messages
    FOR EACH ROW
    EXECUTE FUNCTION enforce_message_send_state_transition();
