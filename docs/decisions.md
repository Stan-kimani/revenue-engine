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

## 2026-08-03 — M0.2: migrations/0001_init.sql, db/models.py, db/repositories.py

Per build-spec §10, scoped to companies/contacts/leads/events/jobs for
`repositories.py`, but migration 0001 creates the *entire* schema (all domain
and infra tables) since entity-model.md §7 says infra tables "will be written
directly into migration 0001 alongside the entities above." Two decisions were
put to the user as genuinely blocking rather than guessed:

1. **Closed-vocabulary columns are `text` + CHECK, not native Postgres ENUM
   types.** Confirmed by the user. Adding/removing a value is then a plain
   `ALTER TABLE ... DROP/ADD CONSTRAINT` inside a normal transaction, matching
   how the objection vocabulary already changed once (7→8 categories,
   2026-07-28 entry above). `db/models.py` still defines Python `StrEnum`s for
   these columns as an application-layer convenience (mypy catches typos) —
   an independent choice from the DB layer, not in tension with it.
2. **`embeddings.vector` has no fixed dimension yet.** Confirmed by the user
   (deferred, "Recommended" option). No embedding provider is named anywhere
   in the docs — build-spec §7's Integrations table covers only the
   completions LLM — and nothing before Phase 2/3 (`core/memory.py`, the first
   `embed()` call) generates embeddings. A fixed dimension and its
   ivfflat/hnsw index land in a later migration once a real provider is
   chosen; until then the column is storage-only, no ANN index.

**Further judgment calls, logged rather than escalated (none blocking or hard
to reverse):**

- **All foreign keys are added in a final `ALTER TABLE` block**, after every
  `CREATE TABLE`, rather than inline. `leads.deal_id` and `deals.lead_id`
  reference each other, so *some* deferral is structurally required; doing it
  for every FK (not just the circular pair) keeps one consistent pattern
  instead of two.
- **`deleted_at` (soft delete, D4) applied to `companies`, `contacts`,
  `campaigns`, `leads`, `deals`, `messages`, `meetings`, `meeting_insights`,
  `objections`** — treating entity-model.md §1 D4 ("all domain entities") and
  build-spec §5.1's "soft delete on entities" as the general rule, with the
  per-table column lists in entity-model.md §3 as additive detail, not
  exhaustive. `campaigns` gets `deleted_at` despite its own §3.3 column list
  omitting it — read as an omission in that list, not an exclusion, given both
  governing docs state the rule as blanket. **Excluded** from `lead_scores`,
  `activities`, and `events` — all three are explicitly documented elsewhere
  as append-only / never deleted, so an always-null `deleted_at` would serve
  no purpose and contradicts the documented design.
- **`events` uses `occurred_at`** (event-catalog.md §R3's full envelope:
  `event_id, type, version, occurred_at, actor, correlation_id, causation_id,
  idempotency_key, payload`), not `created_at` (build-spec §5.1's older,
  terser mention). CLAUDE.md: "later documents supersede earlier ones; the
  build spec is the oldest." `processed_at` (build-spec's operational
  addition, for outbox polling) is kept — additive, not contradicted.
- **Vocabularies for columns entity-model.md's per-table lists don't spell out
  as exhaustive enums**, cross-referenced from elsewhere and given a CHECK:
  `jobs.status` (pending/running/completed/failed/dead_letter — inferred from
  `job.dead_lettered` and general queue semantics), `approvals.action_type`
  and `.status` (event-catalog.md §7.1's expiry table and `approval.*`
  events), `sequence_runs.status` (agent-contracts.md §3's Sales agent state
  machine: pending→active→paused→completed|terminated), `campaign_assets.kind`
  (event-catalog.md §6 `campaign.assets_created` payload), `learnings.scope`
  and `.status` (event-catalog.md §5 `learning.published` payload and
  agent-contracts.md §6). Low-risk given decision 1 above — these are all
  trivially alterable later.
- **`deals.stage` has no FK to `pipeline_stages.key`** — entity-model.md §3.6
  explicitly calls this "FK-ish"; stage transitions are validated in code
  against a config transition table, not a DB constraint. Followed literally,
  not treated as an oversight.
- **`pipeline_stages` is not seeded by this migration.** "Config-seeded"
  (build-spec §5.1) is data, not schema, and CLAUDE.md §6 says seed data only
  via `scripts/seed_dev.py` — which doesn't exist yet either. Migration 0001
  creates the empty table only.
- **`deals.value_base_amount` is a plain stored column, not a SQL `GENERATED`
  column.** A `GENERATED` column always recomputes from its formula, which
  would defeat freezing `fx_rate_to_base` at close (entity-model.md §8.2) —
  `close_deal()`, a later milestone, must be able to write a value that then
  stops changing.
- **`src/revenue_engine/core/errors.py` created**, though build-spec §10's
  M0.2 line only names `db/models.py` and `db/repositories.py`. CLAUDE.md §4
  ("raise typed exceptions from `core/errors.py`. Never swallow exceptions
  silently") is a standing rule `repositories.py` must follow *now* — the
  single-thread constraint violation in `create_lead` has to become a typed
  `DuplicateActiveLeadError`, not a raw `asyncpg.UniqueViolationError`, so the
  file it's specified to come from has to exist. Kept to exactly the two
  exceptions M0.2 needs, not a general error taxonomy.
- **`schemas/entities/attribute.json` created** — entity-model.md §2 requires
  every `attributes` JSONB write to validate against it before insert, and
  `companies`/`contacts` (both in M0.2's repository scope) both have
  `attributes` columns. In scope, not an addition.
- **`tests/contracts/test_schemas_valid.py`** is a generic, reusable "every
  file under `schemas/` is valid JSON Schema" test (build-spec §8), using
  `jsonschema.validators.validator_for()` rather than a hardcoded draft so it
  stays correct regardless of which `$schema` a given file declares. It also
  now validates the `schemas/outputs/` and `schemas/events/` files that
  predate this milestone.

**Bug found by actually running this against Postgres, not just linting it:**
`upsert_company`'s `domain IS NOT NULL` branch had 6 target columns in the
`INSERT` but only 5 value placeholders (`$1..$5::jsonb` for `domain, name,
linkedin_url, country, employee_band, attributes`) — a `PostgresSyntaxError`
that `ruff` and `mypy --strict` both had no way to catch, since it's a string
template, not Python syntax. Caught by
`test_upsert_company_creates_then_updates_on_same_domain` (and four other
tests that call `upsert_company` with a domain) against a real, disposable
Postgres instance; fixed to `$1..$6::jsonb`.

**Verification:** migration 0001 applied cleanly to a fresh Postgres 16 +
pgvector instance (23 tables incl. `schema_migrations`, 28 foreign keys, all
three extensions installed) and is idempotent on re-run via `scripts/migrate.py`.
All 47 tests (`tests/unit`, `tests/contracts`, `tests/integration`) pass,
including single-thread enforcement for both `one_active_lead_per_contact` and
`one_active_lead_per_company`, the inbound `problem_statement` CHECK, citext
case-insensitive email/domain matching, jsonb attribute merging across
upserts, employment-history tracking on a contact's company change, event
idempotency-key no-op re-emission, and the full job lifecycle (enqueue → claim
via SKIP LOCKED → complete, and fail-with-retry vs. fail-to-dead-letter).
`ruff check`, `ruff format --check`, and `mypy --strict` on `core/` and `db/`
are all clean.

## 2026-08-04 — M0.2 corrections: deferred exclusion, embeddings scope/verified, evidence field, deferral is not an error

Reworked the 2026-08-03 M0.2 delivery against a more precise re-specification.
Four corrections, one reasoning task, two additions.

**The `deferred`-exclusion reasoning (asked for explicitly, not a correction):**
the stated exclusion list for `one_active_lead_per_contact` /
`one_active_lead_per_company` omitted `'deferred'`. Concluded it must be
added: `lead.deferred`'s documented recovery path (event-catalog.md §3)
creates a SECOND `leads` row, for the same `company_id`, with
`status='deferred'`. If `'deferred'` is not excluded from the partial index's
`WHERE` clause, that second row still matches the predicate — so inserting
the deferral placeholder would itself throw the exact unique violation it
exists to represent, making the documented flow impossible to implement.
Separately, semantically: a deferred lead is inactive by definition and must
not occupy the one-active-thread slot. Added to both indexes' exclusion
lists. Verified end to end:
`test_second_active_lead_same_company_returns_deferral_not_exception` inserts
the deferred row successfully and confirms it doesn't collide.

**Correction — `create_lead()`'s company-level violation is not an error.**
Previously both single-thread constraints raised `DuplicateActiveLeadError`.
Now: `one_active_lead_per_company` is caught, a `status='deferred'` row is
inserted (inside a transaction, alongside a lookup of `blocked_by_lead_id`),
and a new `LeadCreationResult(lead, deferred, blocked_by_lead_id)` is
returned — never raised. `one_active_lead_per_contact` still raises
`DuplicateActiveLeadError`, since there's no documented business-outcome
event for that case (matches the required test list: "second active lead for
the same contact **is rejected**" vs. "...same company **returns the
deferral result, not an exception**"). If the deferred-placeholder insert
itself collides with a *different* active lead for the same contact (a rare
double-collision), that exception is not caught and propagates as-is —
undocumented edge case, not silently handled.

**Correction — `embeddings.vector` reverted to dimensionless, no index.** The
2026-08-03 entry had already decided this (deferred, "Recommended" per the
user's own earlier choice); a subsequent re-specification asked for
`vector(1536)` + hnsw, which would have silently reversed that decision.
Reverted back to dimensionless/no-index per explicit correction. Recorded
here as a hard requirement, not a suggestion: **fixing the dimension and
creating the ANN index is a prerequisite of whichever milestone first
generates embeddings** (Phase 2/3, `core/memory.py`'s first `embed()` call) —
do not defer past that point, and do not guess the dimension; picking wrong
means re-embedding every stored chunk through a different provider, at cost.

**Correction — `evidence` added to the attribute-provenance envelope.**
entity-model.md §2's literal example was `{value, source, confidence, run_id,
observed_at}` — no `evidence`. Confirmed as a doc gap: `evidence` is the
snippet or reasoning behind `value`, and without it, provenance degrades to
an unfalsifiable confidence number. Envelope is now `{value, confidence,
evidence, source, run_id, observed_at}` — model supplies the first three,
code adds the last three. `schemas/entities/attribute.json` updated
(`evidence` required, nullable — non-LLM sources have no natural text
evidence). entity-model.md §2 updated to match, with a correction note rather
than silently rewriting history.

**Addition — `embeddings` gains `scope`, `ref_company_id`, `verified`.**
`scope` (`global | industry | account`) is orthogonal to `kind` (what the
content is) — a docs gap, not a prior misreading, per the correction. `kind`
answers "what is this," `scope` answers "who can retrieve it." `verified`
(default `false`) is required by competitive-deltas.md D4: only verified
proof records may be retrieved for outreach, and that filter is unenforceable
without a column to filter on. Both entity-model.md §7 and build-spec §3.4
were updated to name `scope`, since neither previously did.

**Addition — flagged, not built:** `deals.stage` still has no FK to
`pipeline_stages.key`, and `pipeline_stages` is still empty after this
migration (both unchanged from 2026-08-03, confirmed correct for M0.2).
Recording explicitly: stage values are validated nowhere yet, at the DB or
data level. Before the first deal is created (M1.4), code-level
stage-transition validation against a config transition table must exist,
and `pipeline_stages` must be seeded (`scripts/seed_dev.py`, not written
yet).

**models.py**: converted from dataclasses to Pydantic `BaseModel`, per this
round's explicit spec. Added `LeadCreationResult`.

**New tests, all passing against a real Postgres instance**, matching the
required list exactly (marked `@pytest.mark.protected` — 8 total, confirmed
via `pytest -m protected --collect-only`):
`test_migration_0001_applies_cleanly_and_is_idempotent_on_empty_database`
(applies the real `migrations/0001_init.sql`, not a monkeypatched empty dir,
against a disposable `CREATE DATABASE`-isolated database — the shared test
database other tests reuse is never touched),
`test_second_active_lead_same_contact_is_rejected`,
`test_second_active_lead_same_company_returns_deferral_not_exception`,
`test_outbound_lead_with_null_problem_statement_inserts_fine`,
`test_inbound_lead_with_null_problem_statement_is_rejected`,
`test_duplicate_provider_message_id_is_rejected`,
`test_duplicate_idempotency_key_is_rejected` (raw-SQL constraint test,
distinct from the pre-existing `emit_event` no-op behaviour test — both are
kept), `test_closing_deal_without_fx_rate_is_rejected`.

**`docs/verification-loop.md` does not exist in this repository** — checked
before starting this round of work. Not fabricated; the §7 reporting format
(real command output, `git diff --stat HEAD -- tests/`, NOT VERIFIED section)
was followed as described inline in the instruction instead.

**Full verification:** migration 0001 (corrected) applied cleanly to a fresh
Postgres 16 + pgvector instance — 23 tables, 29 foreign keys (28 + the new
`embeddings.ref_company_id` FK), `embeddings.vector` confirmed dimensionless
via `\d embeddings`, `deals.closed_deal_requires_frozen_fx` and
`embeddings.ref_company_id_required_for_account_scope` confirmed present via
`\d`. All 53 tests (`tests/unit` + `tests/contracts` + `tests/integration`)
pass, including all 8 protected tests. `ruff check`, `ruff format --check`,
and `mypy --strict` on `core/` and `db/` are clean.

## 2026-08-04 — M0.2: create_lead() made total over the deferred-insert step

**Iteration/triage log.** Before writing any code, worked through the
mechanism the instruction asked to confirm agreement on, then verified it
empirically against a real Postgres instance rather than trusting the
reasoning alone:

1. Re-read `migrations/0001_init.sql`'s two partial index definitions
   directly (not from memory). Both `one_active_lead_per_contact` and
   `one_active_lead_per_company` carry the identical exclusion list:
   `NOT IN ('deferred', 'converted', 'disqualified', 'unsubscribed',
   'dormant')`.
2. **Mechanism, stated exactly:** a partial unique index only constrains rows
   that satisfy its `WHERE` predicate. A row with `status='deferred'` fails
   `status NOT IN (...)` for *both* indexes (since `'deferred'` is in both
   lists) — it therefore satisfies *neither* predicate, and is invisible to
   both indexes' uniqueness check entirely, regardless of its `contact_id` or
   `company_id`, and regardless of what other rows already exist for that
   contact or company. Nothing collides with a `status='deferred'` insert on
   these two indexes — not by luck, but because the exclusion makes the row
   unconditionally exempt from both. This is what "both indexes carry the
   identical five-value exclusion list" is actually for: symmetry is what
   makes the deferred row exempt from *both* sides of the single-thread rule
   at once.
3. Confirmed the live catalog matches this reasoning: `pg_indexes.indexdef`
   for both indexes renders the `NOT IN` as `<> ALL (ARRAY['deferred'::text,
   'converted'::text, 'disqualified'::text, 'unsubscribed'::text,
   'dormant'::text])` — byte-identical arrays.
4. **Tried to empirically construct the "double collision" scenario as
   originally imagined:** contact A already has an active lead (at company
   Z), contact A now attempts company Y, which is already occupied by
   contact B's active lead — i.e. *both* single-thread rules would be
   violated by contact A's second attempt. Result:
   `create_lead()` raised `DuplicateActiveLeadError` for
   `one_active_lead_per_contact` — correctly, per the "contact already has an
   active lead → raise" rule — and never reached the deferred-insert branch
   at all, because Postgres reported the contact-level violation on the
   *first* insert attempt before company-level was ever relevant.
5. Reasoned through whether a different constraint-evaluation order could
   route this into the deferred-insert branch instead (Postgres's order of
   checking multiple violated unique indexes on one row isn't a documented,
   stable guarantee). Even in that case, step 2's exemption still holds: the
   deferred insert reuses `contact_id`, `company_id`, and every other column
   from the original (already-valid) attempt, changing only `status` to
   `'deferred'` — which is exempt from both indexes regardless of which one
   Postgres happened to report first. No FK, CHECK, or PK constraint on
   `leads` can fire either, since every other value already passed on the
   first attempt.

**Conclusion:** the specific "deferred insert collides with a real second
active lead" scenario is not reachable through real data under the current
schema — provably, by construction, not by luck of the current index
creation order. The original docstring calling this "a rare double
collision" was wrong; it described a scenario the schema already rules out.

**This does not mean the requested fix is unnecessary.** The exemption in
step 2 is a property of the current schema — it holds *only* as long as both
indexes' exclusion lists stay identical. Nothing enforces that they will:
a future migration could add a status value, touch one index and not the
other, or add an entirely new unique constraint to `leads` that doesn't know
about `'deferred'` at all. `create_lead()` should not depend on a human
correctly re-deriving this proof every time `migrations/*.sql` changes.
Implemented exactly as asked:

- `LeadCreationResult` gains `failed: bool` and `error: str | None`, and
  `lead` becomes `Lead | None` (None only when `failed=True`).
- The deferred-placeholder insert is now wrapped in its own
  `try/except asyncpg.PostgresError`, returning
  `LeadCreationResult(failed=True, error=str(exc), blocked_by_lead_id=...)`
  instead of propagating. No path out of `create_lead()` can now raise a raw
  `UniqueViolationError` (or any other bare `PostgresError`) — the three
  outcomes (created / deferred / failed) are exhaustive, and the contract is
  documented on `LeadCreationResult` itself.
- The contact-level path is unchanged: still raises `DuplicateActiveLeadError`
  immediately, matching the protected test's contract.

**Testing a path that can't be reached with real data.** Since step 4 showed
the failure branch is empirically unreachable with genuine conflicting data,
`test_deferred_insert_failure_returns_typed_result_not_raise` uses fault
injection instead: a thin duck-typed proxy (`_FaultInjectingConnection`)
wraps the real connection and forces `.transaction()` to raise, while
`fetchrow` still passes through to real Postgres for the setup. (First
attempt used `monkeypatch.setattr(conn, "transaction", ...)` directly on the
`asyncpg.Connection` instance — failed immediately with `AttributeError:
'Connection' object attribute 'transaction' is read-only`, since asyncpg's
`Connection` is a compiled C-extension type that doesn't allow arbitrary
instance-attribute assignment. Switched to a wrapper object instead of
fighting the C extension.) This tests that `create_lead()`'s failure path
itself is correct, not that the scenario occurs naturally — both things are
now true and both are documented as such, in the test's docstring and here.

**New tests, both `@pytest.mark.protected` (10 total now, confirmed via
`pytest -m protected --collect-only`):**
`test_both_single_thread_indexes_have_identical_exclusion_list` (reads
`pg_indexes.indexdef` live, regex-extracts each index's exclusion array, and
asserts the two sets are equal — not asserted against the migration file
text, which is exactly what could drift silently) and
`test_deferred_insert_failure_returns_typed_result_not_raise` (fault
injection, described above).

**Verification:** all 55 tests (`tests/unit` + `tests/contracts` +
`tests/integration`) pass against a real, disposable Postgres instance,
including both new protected tests and the empirical double-collision probe
from step 4 above (run as an ad-hoc script, not committed as a test, since it
demonstrates a *correct raise*, not a business rule — the actual protected
regression coverage for "contact already active → raise" already exists).
`ruff check`, `ruff format --check`, and `mypy --strict` on `core/` and
`db/` are clean.

## 2026-08-10 — M0.3: core/events.py, core/queue.py, orchestrator/router.py, scripts/run_worker.py

**Task 1 (separate from M0.3 itself):** CI was verified genuinely green by
downloading the actual GitHub Actions job log for run #9 (commit `245326e`)
via the repo's own push credential (read-only use, never printed), not by
reasoning about the YAML. `DATABASE_URL` was populated (masking of
`revenue_engine:revenue_engine@` in the log — GitHub auto-masks any text
matching a declared env value — is itself proof it wasn't empty); the raw
log shows `collected 55 items` / `55 passed in 3.76s`, `0 skipped`. The 37s
total is accounted for by the step timings (15s of it is `Initialize
containers` — the service health-check gate genuinely waiting — not
anything suspicious). No changes made.

### Correction 1 — single event dispatcher (not per-correlation-id ordering)

Chosen, per the instruction's own lean: **(ii)**. `scripts/run_worker.py`
elects exactly one event dispatcher across however many `run_worker.py`
processes are running, via `pg_try_advisory_lock` on a fixed key held on a
dedicated connection for the dispatch loop's lifetime. This is an *enforced*
guarantee, not an operational promise ("please only run one instance") —
deliberately, since the latter is exactly the kind of thing that becomes an
emergent property the first time someone scales workers without reading the
docs. Concurrency is where the job queue provides real parallelism instead
(the protected concurrent-claim test proves that side).

Documented as a known scaling limit in
`repositories.claim_unprocessed_event`'s docstring and
`scripts/run_worker.py`'s module docstring, not left implicit. Revisit with
option (i) — serialize per `correlation_id`, allow cross-`correlation_id`
concurrency — only if event-dispatch throughput actually becomes a
bottleneck; nothing about the current design blocks adding that later.

### Correction 2 — stale-job reclaim is a separate sweep, not folded into claim

Chosen: **separate sweep** (`repositories.reclaim_stale_jobs`, called by
`core/queue.py::reclaim_stale`, run periodically by `run_worker.py`'s own
loop) — not a single query that both claims pending jobs and reclaims stale
ones. Reasoning: a single query handling both cases needs a `CASE` expression
to increment `attempts` only for the reclaim branch, which is *possible* but
conflates two operationally distinct events (a healthy claim vs. a crash
recovery) into one query that's harder to reason about, log, and test in
isolation from each other. The separate sweep increments `attempts` and
decides pending-vs-dead-letter in the same statement, so a job whose worker
keeps dying reaches `max_attempts` and stops being silently reclaimed forever
— verified directly by
`test_stale_job_reclaimed_repeatedly_eventually_dead_letters`, which
reclaims the same job `max_attempts` times in a loop and asserts it
dead-letters on the last one, not before.

### Correction 3 — tiebreaker

`claim_jobs`' claim query is now `ORDER BY run_after, created_at` (was
`ORDER BY run_after` alone). Trivial but real: jobs enqueued together in one
transaction (the common case, via `enqueue_for_event`) share a `run_after`
default of `now()`, so without the tiebreaker their relative order was
whatever Postgres felt like on a given execution, not anything deterministic.

### Correction 4 — temporary config loader

`core/queue.py::QueueConfig` / `_load_config()` reads exactly the four
`queue.*` keys from `config/base.yaml` directly (`yaml.safe_load`, no
validation beyond dict access) — explicitly marked in both the class and
function docstrings as **temporary**, to be replaced by `core/config.py`'s
typed, validated loader at M0.4, and not to be extended for anything else in
the meantime. `config/base.yaml` itself only gains the four `queue.*` keys
this milestone needs — everything else build-spec §2 describes it eventually
holding ("models, caps, schedules") waits for M0.4 rather than being added
speculatively now.

### Correctness requirements — mechanisms, stated explicitly

**(a) Event-processed / jobs-enqueued atomicity.** One transaction, one
connection: `scripts/run_worker.py::dispatch_one_event` wraps
`claim_unprocessed_event` (`SELECT ... FOR UPDATE SKIP LOCKED`),
`enqueue_for_event` for every `JobSpec` the router returns, and
`mark_event_processed` in a single `async with conn.transaction():` block.
Postgres commits all of it or none of it — there is no window where
`processed_at` is set but a job doesn't exist, or a job exists but the event
still shows as processed on a later, contradictory read. Proven, not just
described: `test_event_never_marked_processed_unless_jobs_were_enqueued`
raises inside that exact block, after the job insert and before
`mark_event_processed`, and asserts both that the event is still unprocessed
*and* that no orphan job exists after the rollback — the failure mode a
mechanism that only got one of those two right would still pass a weaker
test for. If the dispatch transaction is retried after a crash,
`enqueue_for_event`'s dedup on `(event_id, job_type)` (an upsert — check
first, insert only if absent) makes that retry safe against a *previous*
aborted attempt at the same event, not just against genuinely new events.

**(b) Handler idempotency.** Two distinct layers, not conflated: (1) a job
can never be held by two workers at once — `SELECT ... FOR UPDATE SKIP
LOCKED`, proven with two real concurrent connections, not a mock
(`test_two_concurrent_workers_never_claim_the_same_job`); (2) a job *can* be
claimed, partially run, and then reclaimed and re-run from the start after a
crash (that's what the visibility-timeout reclaim is for) — core/queue.py
does not and cannot make an individual handler's body idempotent. That's the
handler's job: upsert on a natural key, `emit()` with a deterministic
`idempotency_key`, never a blind `INSERT` — exactly the discipline every
M0.2 repository function already follows. Stated in `core/queue.py`'s module
docstring, not left as an implied guarantee the queue doesn't actually
provide.

**(c) Poison jobs don't block the queue.** A claimed job with no registered
handler (all of them, in M0.3 — no agents exist) or whose handler raises
fails cleanly through `core/queue.py::fail()` (bounded retries, eventual
dead-letter) rather than crashing the worker process or the batch it's part
of. `run_job_loop` processes a claimed batch via `asyncio.gather`, so one
job's exception doesn't prevent the others in the same batch from
completing — proven by `test_poison_job_does_not_block_other_jobs`, which
processes an always-poison job immediately before a healthy one and asserts
the healthy one still reaches `completed`.

**(d) Backoff is bounded and jittered.** `min(cap, base * 2^attempts) *
uniform(0.5, 1.0)` — full jitter, `base=2s`, `cap=300s`, `max_attempts=5`
(confirmed values, unchanged). Bounded so a job that's failed many times
doesn't end up scheduled a day out; jittered so many jobs failing at the same
moment (a downstream outage) don't all retry in lockstep the instant it
recovers.

### Job-enqueue idempotency key, without a schema change

The confirmed "deterministic job idempotency key from (event_id, job_type),
upsert not insert" has no column to lean on — `jobs` has no
`idempotency_key`-style column, and no migration was in scope this round
("no new tables" was confirmed; no new columns were asked for either).
Implemented instead as an application-level check-then-insert
(`repositories.get_job_by_source_event`, querying
`payload->>'source_event_id'`) — genuinely safe *because* Correction 1 makes
event dispatch single-process: there is exactly one place in the whole system
that ever calls `enqueue_for_event`, so there is no concurrent
check-then-insert race to protect against. If event dispatch ever becomes
concurrent (revisiting Correction 1), this dedup mechanism would need
revisiting into a real DB constraint at the same time — noted here so that
future change doesn't silently reintroduce a duplicate-job race. The job's
payload also carries the source event's `correlation_id`, not just its id —
otherwise a job that dead-letters days after its originating event would have
no way to be traced back to the lead it belongs to.

### Two additional, smaller judgment calls

- **`meeting.requested` routes to `sales.book_meeting`.** event-catalog.md's
  own section header for this event says "Emitted by Sales," but
  agent-contracts.md §3 explicitly lists `meeting.requested` as a Sales
  *consume* trigger. The two docs disagree with each other; followed the
  more specific per-agent contract (agent-contracts.md) rather than guessing
  which one is stale.
- **ROUTES / UNCONSUMED scope.** Routed only events with a documented
  Phase-1-**agent** consumer (leadgen, qualification, sales —
  agent-contracts.md's own phase labels), even where the event itself is
  Phase 1 (e.g. `deal.created`, `reply.classified`, `contact.unsubscribed`,
  `outreach.sent` — all Phase 1 events, but their sole consumer is CRM Sync,
  which agent-contracts.md itself labels "Phase 2"). Consumers behind
  infrastructure not in M0.3's scope (Slack/ops notifier — M1.3;
  `orchestrator/sequences.py` and `orchestrator/schedules.py` — not part of
  this milestone) are UNCONSUMED for the same reason, even for events
  agent-contracts.md's own illustrative router example showed routed
  (`lead.routed_to_human` -> `notify.slack`) — that example describes the
  system's eventual full state, not what M0.3 specifically builds.

### Bug found by actually running this against Postgres

`repositories.reclaim_stale_jobs`'s query compared `locked_at < now() - $1`
with `$1` bound to a Python `timedelta`. Without an explicit cast, Postgres's
parameter-type inference resolved `$1` as `timestamptz` (not `interval`),
making `now() - $1` evaluate to type `interval` and the outer comparison
`timestamptz < interval` — `asyncpg.exceptions.UndefinedFunctionError:
operator does not exist: timestamp with time zone < interval`. Neither
`ruff` nor `mypy --strict` has any way to catch a Postgres type-inference
ambiguity — it only showed up running the reclaim tests against real
Postgres. Fixed with an explicit `$1::interval` cast.

### ROUTES/UNCONSUMED coverage verified against the live document, not by hand-count

`tests/contracts/test_router_coverage.py` extracts every backtick-quoted,
dotted token from `docs/event-catalog.md` directly (with `{a|b}` brace
expansion for headings like `` `lead.qualified.{cold|warm|mql|sql}` ``),
rather than hand-copying a list into the test. Run once, by hand, against the
live file before writing `ROUTES`/`UNCONSUMED`, to build the table correctly
in the first place rather than iterating against a failing test — 51 tokens
extracted, 3 excluded by name because they aren't event types
(`send.email` — R1's own explicit negative example of what not to name an
event; `approvals.expiry` — a `config/thresholds.yaml` key path;
`events.idempotency_key` — a column reference), leaving 48 real event types,
all 48 already covered by the tables built from the per-agent contracts.
§8's emitter/consumer matrix was deliberately not parsed — it uses no
backticks around event names and introduces no event not already covered by
a `###` heading or the §7 operational-events table, so parsing it would add
extraction risk (a different format to get subtly wrong) for zero coverage
gain.

### Verification

Migration unaffected (still 23 tables, 29 FKs) — this milestone touches only
application code, `config/base.yaml`, and two new event schema files
(`lead.captured.json`, `job.dead_lettered.json` — needed because
`core/events.py::emit()` refuses unknown types, and M0.3's own tests and
`core/queue.py`'s dead-letter path need to emit real, schema-backed events;
the other ~28 documented event types remain unschema'd since nothing needs to
emit them until the agents that would are built). All 78 tests (`tests/unit`
+ `tests/contracts` + `tests/integration`) pass against a real, disposable
Postgres instance, including all 4 newly-required protected tests plus one
added complementary positive-case test
(`test_event_marked_processed_only_after_jobs_committed_together`) marked
protected alongside the required crash-simulation test — 15 protected tests
total now, confirmed via `pytest -m protected --collect-only`. The concurrent
double-claim test was run 3 additional times in isolation to check for
flakiness (none observed). `scripts/run_worker.py` was also smoke-tested as
an actual running process (not just via its functions imported into tests):
starts cleanly, logs structured JSON, acquires the advisory lock, and leaves
no stuck `pg_advisory_lock` behind after being killed. `ruff check`, `ruff
format --check`, and `mypy --strict` on `core/` and `db/` are clean.

**Not verified:** signal-based graceful shutdown (SIGTERM specifically) was
exercised via Python's standard `asyncio` pattern
(`loop.add_signal_handler`, with a `signal.signal` fallback for event loops
that don't support it) but not observed end-to-end sending a real SIGTERM to
a running process in this Windows dev sandbox, where POSIX signal semantics
differ from the Linux production target (build-spec §7).

## 2026-08-10 — Defect: test suite corrupted a developer's dev database

**Reproduced first, exactly.** Before writing any fix, stood up a persistent
("dev-like") Postgres instance, applied migrations, ran the M0.3 test suite
against it as `DATABASE_URL`, and confirmed both halves of the report:
`78 passed`, then `psql -c "SELECT * FROM schema_migrations"` →
`ERROR: relation "schema_migrations" does not exist`; running `scripts/migrate.py`
again against the same database then failed with `relation "companies"
already exists`, trying to re-apply `0001_init.sql` onto a schema whose
tracking table was gone but whose data tables weren't. Root cause confirmed
exactly as diagnosed: `tests/integration/test_migrate.py`'s
`clean_schema_migrations` fixture ran `DROP TABLE IF EXISTS
schema_migrations` before *and* after three tests, against whatever
`DATABASE_URL` pointed at — which in this project's own dev/CI setup, until
now, was the only database anything ran against.

**Choice: (b), a separate `TEST_DATABASE_URL`.** Per the instruction's own
lean, and because it's the more airtight guarantee for the actual amount of
work involved: (a) would require every existing fixture across five test
files to create-and-drop a per-test database, a much larger change for
marginally more isolation than a single, permanent, distinct test database
provides. (b) makes the separation a visible, named fact in `.env.example`
and `docs/runbook.md` — a developer reading either file sees that
`TEST_DATABASE_URL` exists and why — rather than something implicit in
fixture behaviour that only becomes visible by reading test code.

**Enforcement, not convention.** `tests/_db_safety.py::resolve_test_database_url()`
is pure (no I/O), called from `tests/integration/conftest.py` at **module
import time** — i.e. the instant pytest starts collecting anything under
`tests/integration/`, before any fixture or test body runs — and aborts the
*entire session* via `pytest.exit()`, not a per-test skip, if
`TEST_DATABASE_URL` is unset or identical to `DATABASE_URL`. Both conditions
refuse, not just the second: a missing `TEST_DATABASE_URL` is exactly as
unsafe as a duplicated one, since either way tests would fall back to
`DATABASE_URL`. Verified directly, twice, against the same dev-like database
used for reproduction: removing `TEST_DATABASE_URL` from `.env` entirely (not
just unsetting it at the shell — `tests/conftest.py`'s own `load_dotenv()`
would silently restore it from `.env` otherwise) produces a clear refusal
before any database connection is attempted; setting it equal to
`DATABASE_URL` does the same. `schema_migrations`' `applied_at` timestamp was
identical before and after every verification run in this session, confirmed
by direct comparison, not inference.

**No manual setup step, and no changes to `docker-compose.yml` or
`Makefile`.** `tests/integration/conftest.py` creates `TEST_DATABASE_URL`'s
target database automatically (via `DATABASE_URL`'s own connection, which
already has `CREATEDB` — confirmed in the 2026-08-03 M0.2 entry above) if it
doesn't exist, and applies migrations to it by loading and calling
`scripts/migrate.py`'s own `run()` function — reusing the single canonical
migration-application path rather than duplicating its logic — the first
time any integration test is collected. `make migrate` is unchanged and
still targets `DATABASE_URL` only.

**`test_migrate.py`'s three affected tests were rewritten**, not patched, to
use a new `disposable_database_url` fixture (`tests/integration/conftest.py`)
— a fresh, uniquely-named, `CREATE DATABASE`-per-test database, the same
proven pattern the existing `test_migration_0001_applies_cleanly_...` test
already used — instead of sharing and destructively resetting
`TEST_DATABASE_URL`'s own tracking table. None of the five integration test
files' local `database_url` fixtures survive; all now come from the shared
`tests/integration/conftest.py` fixture, which is also what fixed the
duplication of that exact fixture across five files.

**New protected tests (18 total now, confirmed via `pytest -m protected
--collect-only`):**
- `test_schema_migrations_survives_the_full_suite`
  (`tests/integration/test_zz_suite_integrity.py`) — named with a `zz`
  prefix specifically so it collects and runs *last* under pytest's default
  (deterministic, alphabetical) collection order, since its entire point is
  checking the state the whole suite left `schema_migrations` in.
- `test_refuses_when_test_database_url_is_unset` and
  `test_refuses_when_test_database_url_equals_database_url`
  (`tests/unit/test_db_safety.py`) — unit tests (no I/O) against the actual
  `resolve_test_database_url()` function `conftest.py` calls, not a proxy
  for it. Kept in `tests/unit/` rather than `tests/integration/` since the
  function itself never touches a database — only what calls it does.

### `docs/verification-loop.md` — created, not just referenced

This file was referenced by section number (§3, §7) across three consecutive
milestones without ever existing in the repository — flagged each time, but
never actually written. Created now: §3 (the standard command set) gains the
post-suite database integrity check this instruction specifies, run *after*
the suite, not before; §7 (the report format) gains the explicit
"disposable" vs. "safe for a developer's environment" distinction that this
whole defect was about not stating clearly enough. Both additions are
described in the file itself, not just applied to reports going forward.
Logged here rather than silently treated as though it always existed.

### Minor: `.github/workflows/ci.yml`'s Python-version duplication

The instruction described `ci.yml` pinning `python-version: "3.12"`
literally. On inspection, the file actually had `python-version-file:
"pyproject.toml"` — a different duplication than described (deriving from
`requires-python` rather than a literal string), but still two sources for
one fact, since `.python-version` exists specifically to be that single
source (2026-08-03 entry above: "so `uv` selects a matching interpreter
automatically"). Changed to `python-version-file: ".python-version"` —
`.python-version` is now the one place this project's Python version is
declared as a fact; `pyproject.toml`'s `requires-python` remains a *range*
(`>=3.12,<3.13`), a different and compatible statement, not a duplicate of
the exact version.

### Verification

Reproduced the exact reported defect first (both halves: the drop, and the
cascading `relation "companies" already exists` on the next `migrate.py`
run) against a real, persistent Postgres instance standing in for a
developer's dev database — not inferred from reading the fixture. Applied
the fix, then re-verified against the *same* database (reset once, cleanly,
between the reproduction and the fix): full suite (`83 passed`), `ruff
check`, `ruff format --check`, and `mypy --strict` on `core/`/`db/` all
clean, `schema_migrations`'s `applied_at` timestamp byte-identical before and
after, both refusal scenarios (unset / equal) confirmed to abort the session
before any connection is attempted, and `revenue_engine_test` confirmed
created automatically by `\l`. `pytest -m protected --collect-only` shows 18
protected tests.
