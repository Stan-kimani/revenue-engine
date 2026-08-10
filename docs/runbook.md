# Runbook

Ops procedures: deploy steps, key rotation, dead-letter recovery, webhook
re-registration, "the queue is stuck" playbooks (build-spec §11). Populated as
each system lands — this file currently covers only what M0.1-M0.3 introduced.

## Test database — TEST_DATABASE_URL vs. DATABASE_URL

`DATABASE_URL` is the database you're actually working against — your dev
data, or in CI, the database migrations get applied to as a deployment
would. `TEST_DATABASE_URL` is a **separate** database the integration test
suite runs against. They must never be the same value.

**Why this matters:** integration test fixtures are allowed to do things to
their database that would be destructive against a real working database —
truncating tables between tests, creating and dropping whole disposable
databases, and (in an earlier version of this suite) dropping
`schema_migrations` outright to test `scripts/migrate.py`'s from-scratch
behaviour. That fixture was written and verified against a database created
solely for that verification session, where dropping the tracking table was
harmless. It was never updated to also be safe against `DATABASE_URL`
pointing at an actual developer's dev database — which it corrupted (all 22
domain/infra tables survived; only `schema_migrations` was lost, then a
second `scripts/migrate.py` run failed with `relation "companies" already
exists` trying to re-apply `0001_init.sql` onto the now-untracked schema).
"Verified against a disposable Postgres instance" and "safe to run against a
developer's environment" are different claims — see `docs/decisions.md` and
`docs/verification-loop.md` §7.

**The fix:** `tests/integration/conftest.py` refuses to run — the whole
session, immediately, with a clear message — if `TEST_DATABASE_URL` is unset
or identical to `DATABASE_URL` (`tests/_db_safety.py`). `.env.example` sets
`TEST_DATABASE_URL` to the same server as `DATABASE_URL` with a `_test`
suffix on the database name by default. **No manual setup step is needed**:
the first integration test run creates that database (via `DATABASE_URL`'s
own connection, which has `CREATEDB`) and applies migrations to it
automatically if it doesn't already exist.

If you ever need to reset it by hand:

```bash
psql "$DATABASE_URL" -c 'DROP DATABASE IF EXISTS revenue_engine_test'
make test   # recreates and re-migrates it automatically on the next run
```

## Migrations

`make migrate` (`scripts/migrate.py`) applies `migrations/*.sql` in filename
order, tracking applied filenames in `schema_migrations`. Safe to run repeatedly
— already-applied migrations are skipped.

**Non-transactional migrations.** Most migrations run inside their own
transaction, with the `schema_migrations` tracking row inserted in the same
transaction — either both commit or neither does. Some DDL cannot run inside a
transaction block at all: `CREATE INDEX CONCURRENTLY`, some `ALTER TYPE ...
ADD VALUE` forms. To mark a migration as one of these, make its exact first
line:

```sql
-- migrate: no-transaction
```

The migration then runs as a single autocommit statement with no transaction
wrapper, and the `schema_migrations` row is inserted in a separate statement
immediately after it succeeds.

**Recovery if one fails partway.** Because there is no transaction to roll
back, a failure after the DDL has partially taken effect (e.g. a
`CONCURRENTLY` index build that started but did not finish) is not
automatically undone, and the migration is *not* recorded as applied. Before
re-running `make migrate`, check the actual database state (e.g.
`\d+ <table>` for an `INVALID` index left behind by a failed `CONCURRENTLY`
build) and clean it up manually if needed — re-running the same migration file
against a partially-applied state may error differently the second time.
