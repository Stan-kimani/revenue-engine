# Decision Log

Append-only. Newest at the bottom.
Format: `## YYYY-MM-DD — <decision>` / Context / Decision / Consequence.

## 2026-07-28 — Reconciliation pass applied before M0.1

**Context:** the design docs were written incrementally and later decisions superseded
earlier text that was never updated. Resolved before any code was written.

**Decisions applied:**
1. Lead status enum gains `deferred` (single-thread rule needs a state).
2. `problem_statement` required for inbound only, via CHECK constraint on `source` —
   not a blanket NOT NULL, which would break outbound lead creation.
3. `leads` gains `budget_band`, `budget_source`, `pain_category`, `team_size_band`.
4. Scoring weights rebalanced to five components including `budget_fit: 0.15`.
   Loader asserts the sum is 1.0 at boot.
5. Objection vocabulary gains `handoff` → 8 categories, identical in three files.
6. Prompt frontmatter uses `tier`, never a model name.
7. `qualification/classify_intent.md` does not exist; intent is a sub-score in `score_lead`.
8. Gmail uses polling (`history.list`, ~2 min), not Pub/Sub push. With Slack Socket Mode
   the system needs no public HTTP endpoint.
9. CLAUDE.md gains rule 11 (overrides installed skills) and rule 12 (money is numeric).
10. `agents/sales.py` stays one file until ~400 lines, then splits by lifecycle stage.

**Consequence:** bands (`sql: 78`) must be recalibrated — adding a fifth weighted
component shifts the whole score distribution.

## 2026-07-28 — MCP scope

**Decision:** dev-time yes (Postgres MCP from M0.2, narrow read-only role, local DB only).
Runtime core no — a deterministic pipeline gains nothing from dynamic tool discovery.
Runtime client adapters yes, behind `integrations/` interfaces, triggered by the first
client system we do not natively integrate.

**Consequence:** no MCP packages in `pyproject.toml`. Client adapters land behind
`integrations/crm_external.py` and are additive.

## 2026-08-03 — M0.1: migration runner is a tracked Python script, not a psql loop

**Context:** the first M0.1 plan had `make migrate` apply every file in
`migrations/*.sql` in a bash loop on every run. That does not track which
migrations already ran — re-running it either re-applies everything or misbehaves
silently, and a single bad file has no per-migration failure isolation.

**Decision:** `make migrate` runs `scripts/migrate.py` (asyncpg, no new
dependency). It creates a `schema_migrations` table if missing, applies only files
not yet recorded there, each in its own transaction with the tracking insert in the
same transaction, and exits non-zero with the failing filename on error. Safe to
run repeatedly; a no-op once everything is applied.

**Consequence:** every migration filename must sort correctly (numbered prefixes,
per build-spec §2) since application order follows `sorted(glob("*.sql"))`.

## 2026-08-03 — M0.1: CI runs integration tests against a real Postgres service

**Context:** the first M0.1 plan scoped `.github/workflows/ci.yml` to
`pytest tests/unit tests/contracts` only, reading the milestone instruction
"pytest (unit + contract only)" as a standing CI policy. That was a misreading —
build-spec §8 is explicit that CI runs unit + contract + integration on every PR;
the instruction meant only that there is nothing to integrate yet at M0.1, not that
integration tests should be excluded from CI going forward.

**Decision:** CI adds a `services: postgres` block on `pgvector/pgvector:pg16`
(matching `docker-compose.yml`) with a health check, and runs
`pytest tests/unit tests/contracts tests/integration`. Wired now while the suite is
empty rather than retrofitted later against a failing test and a broken CI database
config simultaneously.

**Consequence:** `tests/unit`, `tests/contracts`, and `tests/integration` currently
hold no test files, so `pytest` exits 5 ("no tests collected"). Both the Makefile
`test`/`golden` targets and the CI pytest step treat exit code 5 as success and any
other non-zero code as a real failure — CI stays meaningfully red once real tests
exist, without being permanently red before they do.

## 2026-08-03 — M0.1: `make setup` is a dependency of `test`, `check`, `worker` only

**Decision:** `make setup` (`uv sync`) is a prerequisite of `test`, `check`, and
`worker` — not `dev` (needs only Docker) or `migrate` (left exactly as specified).
A fresh clone can run `make dev` immediately; `make migrate` still requires
`make setup` first, which the README quickstart orders correctly.

## 2026-08-03 — M0.1: `scripts/migrate.py` loads `.env` directly

**Decision:** `scripts/migrate.py` calls `load_dotenv()` (python-dotenv, already a
dependency) before reading `DATABASE_URL`, so `make migrate` works from a local
`.env` file without requiring the variable to be exported in the shell first.
Later scripts should follow the same pattern rather than assuming an exported
environment.

## 2026-08-03 — M0.1: `make worker` fails soft until `scripts/run_worker.py` exists

**Decision:** `make worker` checks for `scripts/run_worker.py` before invoking it
and prints "scripts/run_worker.py is created in M0.3 — nothing to run yet." with
exit 0 when the file is absent, instead of surfacing a raw Python
`FileNotFoundError`/`ModuleNotFoundError` on a fresh M0.1 checkout.

## 2026-08-03 — M0.1: CI's exit-5 tolerance removed; Makefile keeps it

**Context:** the entry above gave CI's pytest step the same "no tests collected is
not a failure" tolerance as the Makefile's `test`/`golden` targets. On reflection
that's the wrong place for it — CI is the gate that should fail loudly if a PR
claims to add coverage but a path typo or empty `testpaths` means nothing actually
ran.

**Decision:** `.github/workflows/ci.yml` runs
`pytest tests/unit tests/contracts tests/integration` directly, no exit-code
handling. It will fail (exit 5) until real test files exist, starting at M0.2/M1.x.
The Makefile's `test` and `golden` targets are unchanged — they still treat exit 5
as success, since local iteration shouldn't be blocked by an empty suite the same
way a merge gate should.

**Consequence:** CI is expected to be red until the first tests land. This is
intentional — the workflow is still exercised (setup, lint, mypy, migrate) end to
end, and the pytest step becomes a real gate the moment M0.2's contract tests exist.

## 2026-08-03 — M0.1 fixes: ruff scope, migrate.py formatting, .env.example default

**Context:** first real run of `make check` surfaced two defects. (1) `ruff format
--check` was reaching into fenced Python code blocks inside `docs/*.md` (confirmed:
it reformatted the router snippet in `agent-contracts.md` and the `complete_json`
call in `revenue-engine-build-spec.md`) — ruff formats embedded code in Markdown by
default, and documentation prose is not this repo's code to reformat. (2)
`scripts/migrate.py` itself was not format-clean (one comprehension ruff wanted
wrapped differently).

**Decision:**
1. `[tool.ruff] extend-exclude = ["docs", "prompts", "schemas", "migrations"]` added
   to `pyproject.toml`. Confirmed by listing every `.py` file in the repo: ruff now
   only ever sees `scripts/`, `src/`, `tests/` — the only directories that contain
   Python.
2. `ruff format scripts/migrate.py` applied and saved.
3. `.env.example`'s `POSTGRES_PASSWORD` changed from `changeme` to `revenue_engine`,
   and `DATABASE_URL` updated to match — now byte-for-byte identical to
   `docker-compose.yml`'s own `${VAR:-revenue_engine}` fallback defaults and to
   `ci.yml`'s env block. Local-only throwaway credentials, not a secret.

**Verification:** `ruff check .`, `ruff format --check .`, and
`mypy --strict src/revenue_engine/core src/revenue_engine/db` all pass clean.

**Consequence:** `make migrate` on a fresh clone still requires `cp .env.example
.env` first (README quickstart already documents this) — that step was never
optional; the fix ensures the copied values actually work against
`docker-compose.yml` rather than mismatching on password.

## 2026-08-03 — M0.1 defect: migrate.py exited before ever opening a connection

**Context:** on verification, `scripts/migrate.py` printed "No migration files
found" and exited before connecting to Postgres. Two "successful" `make migrate`
runs against a healthy database had created no `schema_migrations` table — the
connection path, credentials, and tracking-table DDL were completely untested.
The early return on an empty `migrations/` glob, added when the script was first
written, was the bug: an empty `migrations/` directory is normal at this point in
the build (M0.2 hasn't run) and must not skip the connection.

**Decision:** reordered `run()` to always, in this order: (1) connect to
`DATABASE_URL`, failing loudly with the underlying exception if unreachable,
(2) `CREATE TABLE IF NOT EXISTS schema_migrations`, (3) scan `migrations/*.sql`
and apply anything pending, (4) report either `"N migrations applied"` or
`"up to date, N previously applied"`. No path through the function can now avoid
opening a connection.

**Verification:** since the sandbox's default `localhost:5432` was occupied by an
unrelated, pre-existing container with a stale password baked into its volume
(not something to touch or reuse), verification ran against a fully isolated,
disposable Postgres on a scratch port: `scripts/migrate.py` run twice against an
empty directory (creates the table, 0 rows, idempotent), then again against a
directory with one real `.sql` file (applies it, creates its table, second run
reports `1 previously applied` and creates nothing) — confirming both the fixed
ordering and the untouched apply/idempotency logic actually work end to end
against a real database, not just at the syntax level.

**New test:** `tests/integration/test_migrate.py`, marked `integration`, asserts
(a) running against an empty (monkeypatched) `migrations/` dir creates
`schema_migrations`, and (b) running twice is idempotent. It loads
`scripts/migrate.py` by file path via `importlib`, since scripts/ is intentionally
not part of the installed `src/revenue_engine` package. Runs in CI now that the
postgres service exists (previous decision above).

**Judgment call flagged, not resolved:** `scripts/migrate.py` contains inline SQL
(the `schema_migrations` DDL and its queries), which reads as a literal violation
of CLAUDE.md rule 4 ("All SQL lives in `db/repositories.py`. No inline SQL in
... scripts"). Treating it as migration-tooling infrastructure — the same
category as `migrations/*.sql` itself, which the "Where things go" table
explicitly exempts — rather than the domain SQL rule 4 targets, since
`db/repositories.py` does not exist until M0.2 and the migration runner has to be
connectable and testable before then. Revisit explicitly at M0.2 once
`repositories.py` exists, rather than assumed settled by this note.

**Resolved 2026-08-03.** Confirmed as proposed: `scripts/migrate.py`'s SQL is
schema tooling, not application data access, and stays where it is. CLAUDE.md
rule 4 amended to say so explicitly — it now reads "All *application* SQL lives
in `db/repositories.py`. No inline SQL in agents, routes, or orchestrator code.
Exempt: migration files (`migrations/*.sql`) and the migration runner
(`scripts/migrate.py`), which are schema tooling, not data access. Nothing else
is exempt." `scripts/` was dropped from the no-inline-SQL list and replaced with
a named, bounded exemption — not a blanket carve-out for all scripts. Any other
script (`seed_dev.py`, `run_worker.py`, `backfill_embeddings.py`, etc.) still
must not contain inline SQL once `db/repositories.py` exists at M0.2; only the
migration runner and the migration files themselves are schema tooling.

## 2026-08-03 — M0.1 defect: integration test crashed instead of skipping without .env

**Context:** `tests/integration/test_migrate.py` read `os.environ["DATABASE_URL"]`
directly. `scripts/migrate.py` calls `load_dotenv()` itself, but nothing loaded
`.env` for the *test process* before that — so a normal `uv run pytest
tests/integration` on a machine with a `.env` file but no exported shell
variable raised `KeyError`, not a real test failure. The prior verification of
the migrate.py reorder only passed because `DATABASE_URL` was exported manually
in that shell, which masked this.

**Decision:** added `tests/conftest.py` calling `load_dotenv()` at import time,
so every test process sees `.env` exactly as `scripts/migrate.py` does, with no
per-test or per-fixture loading. `tests/integration/test_migrate.py` gained a
`database_url` fixture that calls `pytest.skip(...)` with a clear reason if
`DATABASE_URL` is still unset after loading — the two tests and the
`clean_schema_migrations` fixture now depend on it instead of reading
`os.environ` directly, so a missing database skips cleanly instead of crashing.

**Verification:** confirmed both directions — with a local `.env` present
(pointed at a disposable, isolated Postgres instance) and no exported
`DATABASE_URL`, `uv run pytest tests/integration -v` passed both tests; with
`.env` removed and nothing exported, the same command reported 2 skipped, not an
error.

## 2026-08-03 — M0.1 defect: local runtime could resolve to Python 3.14, not 3.12

**Context:** on this environment, an unpinned interpreter resolved to Python
3.14.4, while `ruff target-version`, `mypy python_version`, and CI's
`setup-uv python-version` all say 3.12. Linting and type-checking one version
while executing another is a real inconsistency, and `asyncpg` is a C extension
without guaranteed wheels on a version this new — a failure mode that would have
first surfaced as a confusing install error at M0.2, not here.

**Decision:** `pyproject.toml`'s `requires-python` tightened from `">=3.12"` to
`">=3.12,<3.13"`, and a `.python-version` file (containing `3.12`) added so `uv`
selects a matching interpreter automatically instead of falling back to whatever
`python3` resolves to on the machine. `ruff.target-version` (`py312`),
`mypy.python_version` (`3.12`), and `ci.yml`'s `setup-uv python-version` (`3.12`)
were already correct — confirmed, not changed.

**Verification:** `uv sync` downloaded and used CPython 3.12.13 (visible in its
output), installing `asyncpg==0.31.0` as a prebuilt wheel with no build step.
`uv run python --version` reports `Python 3.12.13`. Full `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy --strict src/revenue_engine/core
src/revenue_engine/db`, and `uv run pytest tests/unit tests/contracts
tests/integration` all pass under the pinned interpreter. `uv.lock` is now
committed alongside `.python-version` for reproducibility.

## 2026-08-03 — M0.1 polish: reset target, detached dev, mypy guard, non-transactional migrations

Four small fixes ahead of M0.2, in preparation for iterating on migration 0001
repeatedly.

1. **`make reset`** added: `docker compose down -v && docker compose up -d` —
   a deliberate, named clean-slate command rather than a flag someone has to
   remember to add to `down`.

2. **`make dev` runs detached** (`docker compose up -d`, was `up`). Foreground
   blocked the terminal on every invocation; `reset` also runs detached for the
   same reason.

3. **`make check`'s mypy step now guards on directory existence.** It loops
   over `src/revenue_engine/core` and `src/revenue_engine/db`, skips (with a
   printed note) any that don't exist, runs `mypy --strict` only against
   whatever does exist, and prints "Nothing to type-check yet." rather than
   failing if neither does. Verified all three states directly (both present,
   neither present, only one present) by temporarily moving the directories
   aside and back — the git tree was unaffected (`git status --short src/`
   showed nothing after restoring). `ci.yml` was deliberately left unchanged:
   `actions/checkout` always pulls the tracked `__init__.py` files, so CI isn't
   exposed to the scenario this guards against the way a stray local clone
   might be, and only `make check` was asked for.

4. **`scripts/migrate.py` supports non-transactional migrations.** A migration
   file whose exact first line is the comment `-- migrate: no-transaction` now
   runs as a single autocommit statement with no `conn.transaction()` wrapper,
   with the `schema_migrations` row inserted in a separate statement
   immediately after it succeeds. Needed for `CREATE INDEX CONCURRENTLY` and
   some `ALTER TYPE ... ADD VALUE` forms, which Postgres refuses to run inside
   a transaction block. Documented at the top of `migrate.py` and, more fully
   (including partial-failure recovery — there is no transaction to roll back,
   so a failed non-transactional migration is not automatically undone and is
   not recorded as applied), in a new "Migrations" section of `docs/runbook.md`.
   Not needed for migration 0001 against an empty database; will matter the
   first time a `CONCURRENTLY` index runs against real data.

**Verification:** all three `tests/integration/test_migrate.py` tests
(including the new `test_no_transaction_migration_applies_and_is_recorded`)
pass against a real, disposable Postgres instance.
