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
