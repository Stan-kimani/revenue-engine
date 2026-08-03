# Runbook

Ops procedures: deploy steps, key rotation, dead-letter recovery, webhook
re-registration, "the queue is stuck" playbooks (build-spec §11). Populated as
each system lands — this file currently covers only what M0.1 introduced.

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
