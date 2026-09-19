-- 0007_email_status_tiers.sql — M1.4a addendum: three-tier email status
-- (send / restricted / never) and the snapshot a weighted bounce needs.
--
-- docs/deliverability.md §5-§6. B2B domains at our ICP size frequently run
-- catch-all, so a flat [valid] allow-list would refuse most legitimate
-- prospects. Catch-all carries real bounce risk, so it is permitted under
-- stricter accounting (a sub-cap, and double-weighted bounces) rather than
-- treated as equivalent to a verified mailbox.

-- catch_all: the domain accepts everything, so acceptance proves nothing.
-- disposable: throwaway mailbox. role_based: info@/sales@/support@ — these
-- fail on two grounds, bounce risk AND landing in a shared inbox where cold
-- email is deleted unread.
ALTER TABLE contacts
    DROP CONSTRAINT contacts_email_status_check,
    ADD CONSTRAINT contacts_email_status_check CHECK (email_status IN (
        'unverified', 'valid', 'risky', 'invalid', 'bounced', 'suppressed',
        'catch_all', 'disposable', 'role_based'
    ));

-- The recipient's verified status AT THE MOMENT OF RESERVATION. Without this
-- snapshot a later bounce cannot be attributed to a tier (the contact's
-- status may since have changed to 'bounced'), and the double weighting for
-- catch-all bounces would be unenforceable.
ALTER TABLE messages
    ADD COLUMN recipient_email_status text;

CREATE INDEX messages_recipient_email_status_idx
    ON messages (recipient_email_status)
    WHERE send_state IN ('sending', 'sent', 'send_unknown');
