# Verification Loop

**Note on provenance:** this document was referenced by section number
(§3, §7) across several milestones before it actually existed as a file.
Rather than keep flagging the gap, it's written here now, describing the
process that was already being followed in practice throughout M0.1-M0.3 —
verify every deliverable against real infrastructure before reporting it
done — plus the corrections logged in `docs/decisions.md` for the defect
that prompted finally writing it down.

---

## 1. Why this exists

`ruff` and `mypy --strict` catch a real class of bugs. They do not catch:
a SQL string with the wrong number of placeholders, a Postgres type-inference
ambiguity (`timestamptz < interval`), a partial index whose exclusion list
silently diverges from its twin, or a test fixture that's destructive against
one database and harmless against another. All of these were caught during
this project only by actually running the code against a real, live
Postgres instance — never by reading the code.

**"Verified against a disposable Postgres instance" and "safe to run against
a developer's environment" are different claims.** A fixture can be entirely
correct against a database created solely for that verification session and
still be destructive against a persistent one a developer or CI is actually
working against. State which claim is being made — do not let the former
imply the latter. (This is precisely the defect logged in `docs/decisions.md`
for the milestone that introduced `TEST_DATABASE_URL`.)

## 2. When to run it

Before reporting any deliverable as done — a milestone, a fix, a defect
response. Not after writing the code and reasoning it looks right; after
actually running it.

## 3. Standard command set

Run in this order, against a real Postgres instance:

```bash
uv run python scripts/migrate.py                                  # apply migrations
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/revenue_engine/core src/revenue_engine/db
uv run pytest tests/unit tests/contracts tests/integration -v
uv run pytest tests/ -m protected --collect-only -q               # confirm the expected protected tests exist

# THEN, after the suite has finished running — not before — a post-suite
# database integrity check. This is precisely the class of bug this loop
# exists to catch, and it slipped through three milestones because this
# check was never in the command set:
docker compose exec postgres psql -U revenue_engine -d revenue_engine \
  -c "SELECT * FROM schema_migrations;"
```

The integrity check runs against whatever `docker-compose.yml`'s `postgres`
service is currently serving as `DATABASE_URL` (a developer's dev database in
the common case) — its entire purpose is confirming the test run that just
happened did not disturb it. If the test suite legitimately needed a
disposable database for something, that must be `TEST_DATABASE_URL`, a
distinct database, checked separately — never inferred from this command
passing.

## 4. Disposable infrastructure for the verifier's own use

When verifying as the agent doing the work (not a developer's persistent
setup), stand up a throwaway Postgres rather than reusing whatever the
working directory's `.env` currently points at — a scratch `docker-compose`
file on a free port, migrate it, run the command set above against it, tear
it down afterward. This keeps verification itself from being the thing that
corrupts a real environment, and it's what makes "verified against a
disposable Postgres instance" a true statement rather than an assumption.

## 5. Iteration and triage

A failure found while running the command set gets fixed and the *whole*
command set re-run, not just the one failing step — a fix to one query can
break formatting, or a fix to formatting can hide a logic error the next
`pytest` run would have caught. Every fix found this way is a real defect
that static analysis missed; log what it was, not just that "tests now
pass."

## 6. Judgment calls

Logged in `docs/decisions.md`, per CLAUDE.md §2 — including ones raised
during verification itself, not only ones made while writing the original
code.

## 7. Reporting

Every verification report includes:

- **Real command output** — actual terminal output from the command set in
  §3, not a paraphrase of what it should show.
- **`git diff --stat HEAD -- tests/`** — the test-file diff specifically,
  separate from the full diff, so what changed in test coverage is visible
  on its own.
- **An iteration/triage log** — what failed on the first pass, what the fix
  was, and that the fix was re-verified, not just applied.
- **A NOT VERIFIED section** — named gaps in what was actually checked
  (signal delivery not observed end-to-end, a CI run not yet fetched and
  read, etc.), not silence where a claim would otherwise be assumed.
- **Explicit "disposable" vs. "safe for a developer's environment" language**
  wherever both could be inferred from the same sentence. "All tests pass
  against a real Postgres instance" is true and is not the same claim as "it
  is safe to run this suite against your dev database" — say which one is
  being made.
