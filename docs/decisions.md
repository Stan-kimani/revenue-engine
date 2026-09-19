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

## 2026-08-11 — M0.4: core/config.py, core/llm.py, core/observability.py, golden harness

Several judgment calls made while implementing, beyond the four already resolved
in the approved plan (Langfuse SDK not hand-rolled, model IDs verified against
docs.claude.com before use, `reply_ooo`'s `out_of_office` enum value confirmed
present, `prompts/_conventions.md` written).

**`agent_runs` extended beyond build-spec §5.1's locked shape.** build-spec §5.1
and `entity-model.md` §7 describe `agent_runs` as `(agent, trigger_event, trace_id,
cost, latency, status)` — "locked... need no domain decisions" — and
`migrations/0001_init.sql` matches that exactly. The instruction for this
milestone explicitly required `complete_json()` to record "prompt_version, tier,
model, token counts and cost to agent_runs", none of which that shape has columns
for (`lead_scores` has its own `prompt_version`/`model` columns instead — a
different table, populated by a later milestone). Per CLAUDE.md §0 ("later
documents supersede earlier ones; the build spec is the oldest") and because this
was a direct, explicit instruction rather than an inferred one, added
`migrations/0002_agent_runs_llm_fields.sql` (additive `ALTER TABLE`: `prompt_id`,
`prompt_version`, `tier`, `model`, `input_tokens`, `output_tokens`, `retry_count`)
rather than inventing a place to put these fields that contradicts the instruction.
Flagging rather than silently resolving, since it is a real conflict between two
binding documents under CLAUDE.md's own conflict rule.

**V4 "force `requires_approval`" reinterpreted as "force `pricing_present: true`".**
`docs/phase1-llm-boundary.md` §4's table says V4's on-failure action is "force
`requires_approval`, log honesty violation". `schemas/outputs/proposal_draft.json`
has no `requires_approval` property and `additionalProperties: false` — adding one
would itself be a schema violation. The schema's own description states
proposals are "Always approval-gated (A2)" unconditionally, and that
"`pricing_present` is used by the gate" — i.e. `pricing_present` *is* the field
the approval gate reads, not a separate flag. Implemented V4 as forcing
`pricing_present: true` only; "force requires_approval" is satisfied because
every proposal is already approval-gated regardless, and forcing the honest value
of the one field the gate actually inspects is the mechanism, not a literal
second field.

**`complete_json()` signature expanded**, same pattern as `core/events.py::emit()`
in M0.3: `conn`, `correlation_id`, `actor` (required keyword-only — `agent_runs`
and a possible `llm.validation_failed` emission both need them, unconditionally),
`causation_id` (doubles as `agent_runs.trigger_event` — one caller-supplied value
backs both, since both mean "the event that caused this call"), and `client`
(injectable Anthropic client, defaulting to a real `AsyncAnthropic()` — this is
what makes the protected retry/failure tests possible without an API key).

**Retry-once protected tests placed in `tests/integration/`, not `tests/unit/`.**
The instruction listed them under "tests/unit and tests/contracts — no API key
needed". They don't need an API key (the Anthropic client is always stubbed) but
they do write real `agent_runs` rows and may emit a real `llm.validation_failed`
event — build-spec §8.1 scopes unit tests to "no network, no LLM" but doesn't say
"no database", and §8.4 describes integration tests as running "against a real
test database with mocked integrations", which is exactly this shape (Postgres
real, Anthropic client the mocked integration). Followed the project's own
established DB-testing convention (`TEST_DATABASE_URL`, the `conn` fixture
pattern already used in `tests/integration/test_events.py` and
`test_queue.py`) rather than the literal directory name, and split accordingly:
pure V1-V9/render logic in `tests/unit/test_llm.py` (no I/O at all), full
`complete_json()` control-flow tests in `tests/integration/test_llm.py`. Golden
tests needed the same real-database access for the same reason, so
`tests/golden/conftest.py` reuses `tests/_db_safety.py`'s safety check directly
rather than re-deriving it — it does not auto-create/migrate the test database
the way `tests/integration/conftest.py` does, since golden tests are invoked
manually well after `make test`/`make migrate` has already run at least once in
any real workflow; duplicating that bootstrap for a money-costing, manually-run
category wasn't worth it.

**Cost table uses standard, not introductory, Sonnet 5 pricing.** Sonnet 5 has
introductory pricing ($2/$10 per MTok) through 2026-08-31 per
docs.claude.com. `config/base.yaml`'s `models.standard.*_cost_per_mtok` uses the
standard post-introductory rate ($3/$15) deliberately, so `agent_runs.cost`
doesn't silently jump when introductory pricing ends — a cost figure that changes
meaning on a date nobody configured is worse than a cost figure that's
conservatively a little high before then.

**`test_pack_supplies_all_variables`'s two-bucket design** (config-resolvable vs.
`RUNTIME_VARIABLES`) was your explicit answer to the pre-plan question — recorded
here because the actual per-variable classification (30 variables across the 10
prompt files, listed with a comment per entry naming which future agent supplies
each runtime one) was a judgment call made while writing
`tests/contracts/test_prompts_valid.py`, not something the answer itself
specified variable-by-variable. `step_intent` was the one genuinely ambiguous
case: it looks like per-call sequence state but is actually
`pack.sequences.default.steps[N].intent`, a pack-authored string looked up by
step number — classified as config-resolvable, not runtime.

**V2's word-count tokenisation rule**, per your instruction to state it once and
apply it identically in code and tests: split on whitespace, keep tokens
containing at least one alphanumeric character. A hyphenated word or a
contraction is one token. Stated in `core/llm.py::_word_count`'s docstring and
applied unchanged in `tests/unit/test_llm.py`.

**`schemas/entities/industry_pack.json` typed accessors are partial by design.**
`core/config.py`'s `IndustryPack` deep-models only the sections M0.4 code
actually touches (`status`, `scoring`, `voice`, `objection_categories`); `icp`,
`qualification`, `commercial_boundaries`, `sequences`, `service_catalogue`,
`discovery`, `channels`, `account_limits` are exposed as read-only
`MappingProxyType` attributes rather than hand-modelled dataclasses — every
section is still reached through a named attribute (never a raw dict key on the
loaded YAML), but full field-level typing waits for the milestone that consumes
each section (CLAUDE.md §4: minimum abstraction, no speculative modelling).

### Defects found during verification (not from reading the code)

**`langfuse` v2-era API assumed, installed package is v4 (OTel-based).**
`pyproject.toml` pinned `langfuse>=2.0` (open, per DECISION 1 — no upper bound was
specified or asked for); `uv sync` resolved `langfuse==4.14.3`. Introspected the
actual installed package (`inspect.signature`) rather than trusting memory or docs
that could be stale for a fast-moving SDK: `Langfuse(public_key=..., secret_key=...,
host=...)` still constructs the client, but there is no `.generation()` method —
span/generation creation is `client.start_observation(trace_context={"trace_id":
...}, as_type="generation", model=..., metadata=..., usage_details=...,
cost_details=..., level=...)`, returning an object with `.end()`, then
`client.flush()`. `core/observability.py` and its tests were written against this
confirmed real shape, not the assumed one. `usage_details`/`cost_details` taking
real dicts (not the placeholder `None`s in the first draft) meant threading
`input_tokens`/`output_tokens` through `record_span()`, an improvement made
possible by the correction.

**`.env.example` had a bug that broke the M0.3 TEST_DATABASE_URL safety
mechanism.** `DATABASE_URL` and `TEST_DATABASE_URL` were byte-identical
(`.../revenue_engine_test`, both with a stray `' >> .env` shell-artifact suffix) —
directly violating the invariant `tests/_db_safety.py` exists to enforce, and
which the file's own adjacent comment states. Traced via `git log`/`git show` to
two manual commits made just before this session (`a6d719c` "postgres edit",
`042d3b3` "database url edit", both 2026-08-10 — a shell redirect that landed
partially inside the file instead of running), not an M0.1-era latent bug as
first assumed here — corrected after checking, not left as a misattribution.
Never caught because nothing had run `cp .env.example .env` and then the suite
since those commits landed. Fixed: `DATABASE_URL` points at `revenue_engine`
(matching `docker-compose.yml`'s default), `TEST_DATABASE_URL` at
`revenue_engine_test`, artifact suffix removed from both.

**`config/industries/b2b-service-firms.yaml` had an unquoted colon inside a
`tone_rules` list item.** `- Concrete over abstract: name the workflow, not "your
processes".` parses in YAML as a one-key mapping (`{"Concrete over abstract":
"..."}`), not a string — because of the `: ` — silently turning one `tone_rules`
entry into an object where `schemas/entities/industry_pack.json` (and the
`array of string` the field is supposed to be) expects a string. Never caught
before this milestone because nothing validated the pack against a schema until
`core/config.py` existed to do it — this is exactly the defect class that
validation exists to catch, and did, on the very first real load. Fixed by
single-quoting the line. Checked the rest of the file for the same pattern
(`grep` for other unquoted `- ...: ...` list items) — the only other colon-bearing
list items are genuine, intentional mappings (`disqualifiers`, `discovery_checklist`,
`sequences.default.steps`, all objects with named keys), not accidental strings.

**`prompts/leadgen/build_prospect.md` — filename didn't match its own frontmatter
`id`.** Frontmatter declared `id: leadgen/build_prospect_profile`; the file itself
was `build_prospect.md`, missing `_profile`. Caught by the new
`test_frontmatter_id_matches_own_path` contract test — the same check
`core/llm.py::_load_prompt()` enforces at runtime, which means this prompt was
previously unusable end to end (`complete_json()` would always have raised).
Checked every other of the 10 prompt files for the same class of mismatch — none
found, this was isolated. Before renaming, checked whether the *id* or the
*filename* was the outlier: `docs/agent-contracts.md`, `docs/phase1-llm-boundary.md`,
`docs/competitive-deltas.md`, and `docs/revenue-engine-build-spec.md`'s own repo
layout diagram all independently and consistently say `build_prospect_profile`
(never `build_prospect`) — four-for-four agreement is not "a path seems wrong,
ask" territory (CLAUDE.md §1 non-negotiable 1); it's fixing a typo against
unanimous, already-binding documentation. Renamed the file to
`build_prospect_profile.md`; grepped the repo afterward for any remaining
reference to the old name (none).

**Two files sharing the basename `test_llm.py` across `tests/unit/` and
`tests/integration/` collided under pytest's default import mode** (neither
directory has `__init__.py`, so both resolved to the same top-level module name).
Renamed the integration one to `tests/integration/test_complete_json.py` rather
than adding `__init__.py` to every test subdirectory — the narrower fix, since
restructuring how pytest imports the *entire* existing suite for one new
collision risked side effects on already-verified M0.1-M0.3 tests that weren't
worth it for this.

**Four `core/config.py` pack-selection tests were polluted by `.env`'s own new
`INDUSTRY_PACK=b2b-service-firms` default** (added to `.env.example` by this same
milestone) — `os.environ.get("INDUSTRY_PACK")` resolved before the autodetection
path these tests meant to exercise ever ran, since `tests/conftest.py` loads the
real `.env` for every test process. Fixed by having those four tests
`monkeypatch.delenv("INDUSTRY_PACK", raising=False)` explicitly, rather than
relying on the variable happening to be absent — which stopped being true the
moment this milestone gave it a real default.

**Not a code defect, flagged for you:** the pre-existing, 8-hours-old
`revenue-engine-postgres-1` container (this machine's real dev database,
`docker-compose.yml`'s project) is reachable on `localhost:5432` but its actual
Postgres password does not match `.env`'s `revenue_engine`/`revenue_engine`
(`password authentication failed`) — its data volume was almost certainly
initialised under different credentials at some point before this session, and
Postgres only applies `POSTGRES_PASSWORD` on first init of an empty data
directory, not on every container start. `make migrate`/`make test` against it
will fail the same way until that's reconciled (reset the volume with `docker
compose down -v` if its data isn't needed, or `ALTER ROLE revenue_engine WITH
PASSWORD 'revenue_engine'` from inside the container if it is). Not touched or
reset in this session — all verification here ran against a separate, disposable
instance instead (see below), specifically to avoid guessing at what that
container's data is worth.

### Verification

See the M0.4 verification report (chat) for the full §7 write-up: real command
output, `git diff --stat HEAD -- tests/`, iteration/triage log, and the NOT
VERIFIED section (golden tests were built but not executed — no
`ANTHROPIC_API_KEY` in this environment).

## 2026-08-12 — config/base.yaml: model-ID pinning and pricing re-verified pre-M1.1

**Context:** two corrections requested before M1.1, both about `config/base.yaml`'s
`models` block, on the stated concern that a silently-changing model ID or a wrong
cost figure would corrupt the Learning Agent's cross-time messaging comparisons
and `agent_runs` cost tracking.

**1. Tier pinning.** The premise — that `standard` (`claude-sonnet-5`) and `deep`
(`claude-opus-5`) "appear to use undated aliases" the way `fast` doesn't — turned
out not to hold, checked directly against Anthropic's model-ID documentation
rather than assumed either way:
[https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions),
fetched 2026-08-12. Quoting it directly: *"Starting with the Claude 4.6
generation, model IDs use a dateless format... For the 4.6 generation and later,
the dateless ID is the canonical model ID for that release. It maps to a single,
fixed model snapshot... A 4.6-generation ID such as `claude-sonnet-4-6` is not an
alias. It is the snapshot."* The doc names this exact assumption — that a
dateless ID behaves like an evergreen alias — as *"a common misconception."*
There is no separate dated-snapshot form of `claude-sonnet-5` or `claude-opus-5`
documented anywhere (the model overview table's "Claude API ID" and "Claude API
alias" columns are literally identical strings for both), so there is nothing to
pin to instead — inventing a fake dated ID would be actively wrong, not more
correct. `fast` (`claude-haiku-4-5-20251001`) was already the true dated
snapshot for its pre-4.6 generation, not the `claude-haiku-4-5` alias.
**Conclusion: all three tier→model IDs in `config/base.yaml` were already
maximally and permanently pinned. No model ID changed.** The underlying goal
(no silent model swap under a fixed tier) is satisfied by the current
identifiers as-is.

**2. Pricing.** Re-verified against
[https://platform.claude.com/docs/en/about-claude/pricing](https://platform.claude.com/docs/en/about-claude/pricing),
also fetched 2026-08-12, cross-checked against the models-overview table
fetched the same day:

| Tier | Model | Input $/MTok | Output $/MTok |
|---|---|---|---|
| fast | claude-haiku-4-5-20251001 | 1 | 5 |
| standard | claude-sonnet-5 | **2** | **10** |
| deep | claude-opus-5 | 5 | 25 |

`fast` and `deep` were already correct — unchanged. `standard` was wrong:
M0.4 deliberately set it to $3/$15, the *post-introductory* rate, as a hedge
against a scheduled September 1, 2026 price increase from the $2/$10
introductory rate. Anthropic's pricing page now states, verbatim: *"The $2/$10
per million input/output token pricing for Claude Sonnet 5, announced at launch
as introductory pricing through August 31, 2026, is now the standard price. The
previously scheduled increase to $3/$15 per million input/output tokens on
September 1, 2026 will not occur."* The hedge is stale; updated `standard` to
$2/$10, the confirmed-permanent rate. This was actively wrong in the direction
of overcounting cost for every `standard`-tier `agent_runs` row recorded since
M0.4 landed — worth noting since `deep` (Opus-class, used by the Learning Agent
over large evidence sets per this instruction) was correct throughout and never
had this problem.

**Verification:** `config/base.yaml`'s comment block rewritten to cite both
sources and the 2026-08-12 date directly (not just referenced here). No test
asserts a literal cost figure (`tests/unit/test_config.py`'s cost test compares
against the config's own configured rates, not hardcoded numbers), so no test
changes were needed. `ruff check`, `ruff format --check`, `mypy --strict` on
`core/`/`db/`, and the full `tests/unit tests/contracts tests/integration` suite
(190 tests) all re-run clean after the change.

## 2026-08-13 — complete_json() never showed the model the schema; fixed with structured outputs

**Context:** all 12 golden tests failed the first time they were actually run
(`ANTHROPIC_API_KEY` became available). Every failure was the model inventing a
plausible-but-wrong enum value or field name (`'follow_up'` instead of
`'send_followup'`, `'escalate_to_human'` instead of `'escalate_human'`,
`'C-Suite'` for a closed `seniority` enum, additional properties not in the
schema, `None` where a type didn't allow it). Root cause, confirmed by reading
`complete_json()`: the JSON Schema was used to *validate* the response after
the call, but was never sent *in* the request — the prompt's `# Output` section
only said "Respond with JSON matching the output schema," prose the model had
to infer a shape from. This was invisible to all 197 unit/contract/integration
tests because none of them inspected the outgoing request — every one of them
only checked what `complete_json()` did with a canned reply.

**Decision: (b), the Anthropic API's native structured-output mechanism, not
(a) (appending schema text to the prompt body).** Confirmed available by
introspecting the installed SDK directly (`anthropic==0.120.2`,
`inspect.signature`/`inspect.getsource`), the same discipline used for the
`langfuse` v4 API correction — not trusted from memory or general docs:
`AsyncMessages.create()` has an `output_config: OutputConfigParam | Omit`
parameter; `OutputConfigParam.format: JSONOutputFormatParam`;
`JSONOutputFormatParam = {"type": "json_schema", "schema": dict}`. Then
cross-checked against
[https://platform.claude.com/docs/en/build-with-claude/structured-outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
(fetched 2026-08-13) to confirm behaviour, not just shape: *"The API always
returns valid JSON matching your schema when structured outputs are enabled"*
— this constrains generation directly (compiled grammar), not a request phrased
more persuasively. All three tiers' models (`claude-haiku-4-5-20251001`,
`claude-sonnet-5`, `claude-opus-5`) are on the documented supported-model list.
(b) was chosen over (a) because it is strictly stronger — a JSON-shape error
becomes structurally impossible rather than merely less likely — and because
sending schema text in the prompt body *and* using `output_config` would be
redundant (paying tokens twice for the same constraint, one of them
unenforced). Not layered on top of (a); (b) alone.

**One real transform was required, not zero.** The docs list unsupported
JSON Schema keywords: numeric/string length constraints (`minLength`,
`maxLength`, `minimum`, `maximum`) are *"stripped"* by the SDK itself before
the request is sent (confirmed: *"Python, TypeScript, Ruby, and PHP SDKs
automatically strip unsupported constraints"*) — nothing to do on our side,
and our own `jsonschema` validation against the full, untransformed schema
still catches any real violation afterward, same as before. But `enum`
containing `null` is different: documented as *"Not Supported... use anyOf
with `{"type": "null"}` instead"* — not auto-fixed, and three of our nine
output schemas use exactly that pattern for inferred-value fields
(`company_enrichment.json`'s `business_model`/`employee_band`/`revenue_signal`,
`contact_enrichment.json`'s `seniority`/`decision_authority`/`functional_area`,
`reply_classification.json`'s `objection_category`). Implemented
`core/llm.py::_to_structured_output_schema()`: a generic recursive transform
converting any `{"enum": [..., null]}` into `{"anyOf": [{"enum": [...]},
{"type": "null"}]}`, applied to a derived copy of the schema built fresh for
`output_config` only — `schemas/outputs/*.json` itself is untouched and
remains what `_validate_json_response()` checks the response against
afterward. Chose a generic recursive transform over hand-editing the three
affected schema files so this can't silently miss a fourth file later, and so
`schemas/outputs/*.json` stays exactly what the response is validated against
regardless of what a future structured-output API version does or doesn't
support.

**Retry feedback now includes the schema, not just the error string**, per
your explicit instruction, applied regardless of (a)/(b): the corrective
message appended between attempt 1 and attempt 2 now embeds the full schema
JSON alongside the validation errors. This matters even with `output_config`
active on every attempt (including retries) because V1-V9 cross-field failures
(anchor-id references, word count, pricing honesty) are not JSON Schema
violations at all — `output_config` cannot prevent them — so the model's
second attempt needs the schema as grounding context for *why* a business rule
was violated, not just a bare error sentence anchored on the first attempt's
wrong shape.

**Regression guard, per your explicit instruction:**
`tests/integration/test_complete_json.py::test_request_sent_to_model_includes_the_output_schema`
asserts the actual outgoing stub-client request — not the reply — carries
`output_config.format.schema` with the real enum values, and specifically that
`objection_category`'s enum-with-null was transformed to `anyOf`, not sent
raw. A second new test in the same file asserts the retry's corrective message
text contains the schema, not just the error string. A third,
`tests/unit/test_llm.py::test_real_reply_classification_schema_has_no_enum_containing_null_after_transform`,
walks the *actual shipped* `reply_classification.json` after transform (not a
hand-built fixture) and asserts no `enum` anywhere in the tree still contains
`null`. All three are marked `@pytest.mark.protected` — this is exactly the
class of check whose absence let the original bug ship invisibly through 190
passing tests.

**`prompts/_conventions.md` updated**: new rule under §3 stating the schema is
supplied by `complete_json()` via structured outputs on every call and must
never be duplicated into a prompt body — `schemas/outputs/*.json` is the single
source of truth; a hand-written copy would drift the first time the schema
changes without every prompt referencing it being updated to match. Checked
all 10 existing prompt files for this pattern before writing the rule — none
duplicate schema content; every `# Output` section already just says "Respond
with JSON matching the output schema," which the new rule confirms as the
correct (and only) form.

**Verification:** `ruff check`, `ruff format --check`, `mypy --strict` on
`core/`/`db/`, and the full `tests/unit tests/contracts tests/integration`
suite (197 tests, 30 protected) all pass against a disposable Postgres
instance. One real bug caught during this round's own verification and fixed
before commit: the first implementation nested `output_config` one level too
shallow (passed `{"type": "json_schema", "schema": ...}` directly as
`output_config` instead of wrapping it under a `"format"` key) — caught by the
new regression test itself failing (`KeyError: 'format'`), not by manual
inspection, which is exactly the test doing its job. `make golden` output
(with a real `ANTHROPIC_API_KEY` if available in this session, or the honest
absence of one if not) is reported separately in chat, per the instruction not
to fabricate results.

## 2026-08-14 — Correction: structured outputs also rejects bounding keywords, not just null-enums

**Context:** the 2026-08-13 entry above stated, based on reading
https://platform.claude.com/docs/en/build-with-claude/structured-outputs, that
"length/numeric constraints... are silently stripped by the SDK itself before
the request is sent." **That was wrong.** Running `make golden` for real
produced 400 errors directly from the API:

```
output_config.format.schema: For 'number' type, properties maximum, minimum are not supported
output_config.format.schema: For 'array' type, property 'maxItems' is not supported
```

Re-fetching the same docs page a second time produced a materially different
account of which keywords are supported than the first fetch did (the first
said basic regex `pattern` was supported; a later fetch listed `pattern` as
unsupported) — the page's content, or WebFetch's summarisation of it, is not
stable enough to treat as authoritative on its own. **The real 400 response is
the ground truth here, not any single doc fetch.** Passing a raw dict via
`output_config` evidently bypasses whatever automatic constraint-stripping the
SDK's higher-level helpers may do for other call shapes — this module builds
the dict directly, so nothing was actually being stripped before the request
went out.

**Fix:** `core/llm.py::_STRUCTURED_OUTPUT_UNSUPPORTED_KEYWORDS` — a hardcoded,
deliberately conservative set, stripped by `_to_structured_output_schema()`
alongside the existing enum-null transform: `minimum`, `maximum`,
`exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minItems`, `maxItems`,
`minLength`, `maxLength`, `pattern`, `format`, `uniqueItems`. Covers both
keywords from the actual observed 400s and documented neighbours in the same
family not currently triggering an error (`exclusiveMinimum`/`Maximum`,
`multipleOf`, `uniqueItems` — none of our schemas use these today, but a
future schema might, and the strip needs to not depend on remembering to
extend a list at that point either — it's already comprehensive). Checked
which of these actually appear in `schemas/outputs/*.json`: `minimum` and
`maximum` in all 9 (every confidence-style field is bounded 0..1), `maxLength`
in all 9, `maxItems` in all 9, `pattern` in 3 (`anchor_id` references),
`minLength` in 2, `format` in 1, `minItems` in 1 — confirming this wasn't a
narrow, easily-missed edge case; it would have broken every one of the 10
prompts' real calls.

**CRITICAL, verified not just asserted:** `_to_structured_output_schema()`
only ever operates on a value returned from `json.loads`/dict-comprehension —
never mutates its input in place (`test_transform_does_not_mutate_the_loaded_schema_in_place`
proves this against the actual cached `_load_output_schema` return value, not
a fresh copy). `schemas/outputs/*.json` on disk, and what
`_validate_json_response()` checks the response against, keeps every
constraint stripped from the request. `test_confidence_out_of_bounds_is_still_rejected_after_structured_output_strip`
proves this isn't just true in principle: a stubbed response with
`confidence: 1.5` (schema bounds it to `[0, 1]`) is still retried once and
then still raises `LLMValidationError` — the bound and the retry path both
still function exactly as before this feature existed.

**Tests, per your explicit instructions:**
- `tests/unit/test_llm.py::test_every_real_output_schema_has_no_unsupported_keyword_after_transform`
  — parametrized across all 9 real `schemas/outputs/*.json` files, asserts
  the transformed (request-side) copy has none of
  `_STRUCTURED_OUTPUT_UNSUPPORTED_KEYWORDS` anywhere in the tree.
- `tests/unit/test_llm.py::test_on_disk_output_schemas_still_enforce_their_bounds`
  (protected) — parametrized across the same 9 files, asserts the *on-disk*
  schema still has at least one bounding keyword — proving the strip targets
  a copy, not the source.
- `tests/integration/test_complete_json.py::test_request_sent_to_model_includes_the_output_schema`
  extended to also assert no unsupported keyword survives in what's actually
  sent to the stubbed client, and that the on-disk schema still has
  `maximum`.
- `tests/integration/test_complete_json.py::test_confidence_out_of_bounds_is_still_rejected_after_structured_output_strip`
  (protected) — the stubbed-client bound-survival test described above.

**Verification:** `ruff check`, `ruff format --check`, `mypy --strict` on
`core/`/`db/`, and the full suite (218 tests now, 40 protected) all pass
against a disposable Postgres instance, torn down after. Not re-run against
the live API in this environment (no `ANTHROPIC_API_KEY` here) — you're
re-running `make golden` yourself, which is the real test of this fix.

## 2026-08-14 — word_count becomes code-computed, not model-reported

**Context:** 10/12 golden tests passed after the structured-output fixes above.
The remaining real failure —
`test_profile_no_anchors_yields_outreach_draft_with_empty_facts_asserted` —
rejected an otherwise-good draft: `"word_count=80 does not match body's
actual word count 74 (+/-2)"`, a 6-word self-report discrepancy. Confirmed as
V2's design being wrong, not a model error: token-based models cannot
reliably count their own words, so V2 was asking the model to do arithmetic
on its own output and burning a retry (or risking a dead-letter) over a
non-defect.

**Decision: word_count is no longer part of what the model is asked for at
all.** This follows agent-contracts.md §0.2's deterministic/LLM split
directly, quoted verbatim: *"If the answer is derivable from data by a rule,
it is code. The LLM is only for reading unstructured text and making a
judgment a rule cannot express... Arithmetic ... — always code."* A word
count is a pure function of `body` — exactly the kind of thing that rule
says must never be an LLM task in the first place; V2's original design (ask
the model to self-report, then check it) violated this from the start, just
not visibly until a real run actually rejected a good draft over it.

**Implementation, matching the instruction exactly:**
- `schemas/outputs/outreach_draft.json`: `word_count` removed from
  `required` (the model may omit it — and does, going forward). Kept as a
  property with its existing generic bounds (`minimum: 20`, `maximum: 220`)
  unchanged — deliberately loose, a basic sanity range shared by both
  prompts, not the real business rule (see below). Description updated to
  say it's code-computed and injected, not model-reported.
- `core/llm.py`: new `_inject_word_count()`, computed via the tokenisation
  rule already defined for V2 (whitespace split, keep tokens with at least
  one alphanumeric character), registered in a new `_INJECTING_TRANSFORMS`
  dict keyed by `output_schema` (parallel to `_CORRECTING_VALIDATORS`/
  `_REJECTING_VALIDATORS`) and applied via `_apply_injecting_transforms()`
  — called from `_validate_json_response()` **before** `validator.iter_errors()`
  runs, not after. This ordering is load-bearing: `word_count` isn't in
  `required` anymore, but the property still exists in the schema with a
  type/bounds constraint, so it needs a real value in place before ordinary
  JSON Schema validation checks it. Unconditionally overwrites — if a model
  includes a `word_count` anyway (still a valid, just-not-required property),
  it's discarded, never trusted.
- V2 (`_v2_word_count_matches` → `_v2_word_count_within_limit`): since
  `word_count` is now always code-injected, it can no longer diverge from
  `body` by construction — there is nothing left to "match" a self-report
  against. V2's job changes to enforcing the actual constraint the
  self-report was only ever a proxy for: is the draft short enough. Checks
  against a new module constant, `_OUTREACH_MAX_WORDS = 120`, mirroring
  `draft_initial_outreach.md`'s own Rule 3 ("Under 120 words in the body").
  **One deliberate, explicitly-flagged exception to "no prompt content in
  Python"**: this is a single integer, not prompt wording, and CLAUDE.md §1
  non-negotiable 3 is about prose living in `.py` files, not about a
  schema-adjacent numeric limit also being asserted in code — there is no
  other machine-readable source this number could be read from instead
  (unlike the earlier structured-output fix, which eliminated an actual
  *duplicated schema shape* by making `complete_json()` the single place the
  schema is expressed). Flagged here specifically so it can't silently drift
  from the prompt file's own stated number if that number ever changes.
  `draft_followup.md` has no fixed word count of its own — Rule 4 is
  relative ("Shorter than the previous message in the thread") — so the same
  120-word ceiling applies there too, deliberately: a followup can never
  legitimately need to exceed what the initial outreach was already capped
  at, and `outreach_draft.json` is explicitly documented as shared by both
  prompts.
- `schemas/outputs/outreach_draft.json`'s own `word_count.maximum` (220)
  stays a generic, shared sanity ceiling on purpose, not tightened to 120 —
  keeping V2 as the actual enforcer of the real limit rather than letting
  ordinary schema validation silently absorb that job (which would make V2
  unreachable dead code, since `_apply_rejecting_validators` only ever runs
  on a candidate that already passed schema validation).
- `docs/phase1-llm-boundary.md` §4's V2 table row updated to describe the
  new check — a binding doc's own table describing stale behaviour is
  exactly the drift this project has otherwise been careful to avoid.
- `prompts/sales/draft_initial_outreach.md`: Rule 7 ("`word_count` must
  equal the real word count of `body`. It is checked in code.") removed.
  Rule 3 ("Under 120 words") kept verbatim — that's the actual constraint,
  unaffected by this change. **`prompts/sales/draft_followup.md` needed no
  edit** — grepped both files for `word_count` before editing either; only
  `draft_initial_outreach.md` ever had a line naming it explicitly.
  `draft_followup.md`'s Rule 1 ("All rules from draft_initial_outreach
  apply") referenced the old rule only by inheritance, with nothing of its
  own to remove.

**Tests, per your explicit instructions:** a stubbed response with a body
well over 120 words is rejected (retried once, then raises,
`test_body_exceeding_word_limit_is_rejected_after_structured_output_strip`);
one within the limit succeeds on the first attempt and the returned dict
carries the real, code-computed `word_count` — the stub deliberately omits
`word_count` entirely, since the model is no longer asked for it
(`test_body_within_word_limit_succeeds_with_injected_word_count`). Plus unit
coverage for `_inject_word_count()` directly (computes from body, overwrites
any model-supplied value, handles a missing body) and for `_v2_word_count_within_limit()`
(rejects over limit, accepts under and at exactly the limit, and — the
explicit regression check that the *old* behaviour is really gone — accepts
a wildly-wrong-but-under-limit `word_count`, proving V2 no longer compares
against a self-report at all).

**Verification:** `ruff check`, `ruff format --check`, `mypy --strict` on
`core/`/`db/`, and the full suite (224 tests now, 44 protected) all pass
against a disposable Postgres instance, torn down after.

## 2026-08-13 — Split reply_price_objection.txt into two fixtures

**Context:** `tests/fixtures/reply_price_objection.txt` contained the text:
*"...the pricing feels like it'd be high for where we are right now. We just don't
have the budget for something like this this quarter. Maybe check back later in
the year?"* — a budget-timing deferral, not a price objection (cost-vs-value
dispute). The model correctly returned `intent=not_now`, causing the golden test
asserting `intent=objection, objection_category=price` to fail. This was a
fixture authoring error, not a model error.

**Decision:** Rewrote `reply_price_objection.txt` as an unambiguous cost-vs-value
dispute with no timing element (explicitly states "It's not a timing thing; we
have budget" and disputes ROI). Created `reply_not_now_budget.txt` containing the
original text. Updated the golden test for the price-objection case (unchanged
assertions: `intent=objection, objection_category=price`) and added
`test_not_now_budget_deferral_no_explicit_date` asserting `intent=not_now,
objection_category=null, suggested_action=pause_sequence, requested_resume_date=null`.
"Later in the year" is not an explicit calendar date — the model must not hallucinate
a date, and V8 (correcting validator) would null any non-null `requested_resume_date`
anyway when `intent != not_now`.

**Consequence:** Both golden tests now cover distinct, correctly-labelled scenarios.
The V7 cross-field check (`objection_category` non-null only when
`intent==objection`) and V8 (`requested_resume_date` non-null only when
`intent==not_now`) are the code-level guards that enforce the invariants these
fixtures exercise.

## 2026-08-13 — Explicit timeout and retry policy on AsyncAnthropic client

**Context:** Three API timeouts occurred across three golden runs. The
`AsyncAnthropic()` client was constructed with no `timeout` argument, so requests
that hung (network stall, model delay) blocked the worker indefinitely rather
than failing cleanly.

**Decision:** Added `llm.timeout_s` (60 s) and `llm.max_client_retries` (2) to
`config/base.yaml`. `_default_anthropic_client()` in `core/llm.py` now reads both
from `get_config().llm` and passes them to `AsyncAnthropic(timeout=...,
max_retries=...)`. A hung request now raises `httpx.ReadTimeout` after 60 s,
which propagates as a job failure rather than a silent hang.

**SDK transport retries are distinct from `complete_json`'s validation retry and
must not inflate `retry_count` in `agent_runs`.** The SDK transport retries —
triggered by network errors, 429 rate-limit responses, or 5xx server errors —
happen transparently inside a single `await client.messages.create()` call and
are completely invisible to `complete_json`'s `attempt` counter. `agent_runs.retry_count`
= `attempt - 1` where `attempt` counts only validation-level retries (JSON schema
failures and V1-V9 cross-field failures). A call that succeeds on the second
validation attempt (`retry_count=1`) may have made up to `max_client_retries + 1`
transport-level attempts *per* validation attempt; the `retry_count=1` in
`agent_runs` still correctly represents one validation retry, not the total number
of network round-trips. The Learning Agent's analysis of `agent_runs.retry_count`
must treat it as measuring schema/validation re-drafts only.

## 2026-08-25 — M1.1: Leadgen agent, ManualCsvProvider, scripts/import_leads.py

Environment note first, unrelated to the milestone itself: this round of
work began by discovering the working checkout (a OneDrive-synced Windows
clone) had fallen behind `origin/main` after a force-push rewrote the tip
three commits into a different shape (same content, squashed differently).
That checkout was abandoned; all M1.1 work below was done against
`/home/adminkim/projects/revenue-engine` (WSL) at `1706ec5`, confirmed via
`git log --oneline -1` before anything was read or written.

**Two decisions confirmed before writing code (asked, not assumed):**

1. **`core/llm.py::complete_json()`'s return contract is unchanged.**
   Provenance envelopes need `agent_runs.id` as `run_id`, but
   `complete_json()` only ever returned the parsed output dict — the
   `AgentRun` row it inserts on success was discarded, not handed back.
   Chose "query it back out" over "change the return type": new
   `repositories.get_latest_agent_run(agent, prompt_id, trigger_event,
   status)`, called immediately after each `complete_json()` call. Safe
   because job-claim exclusivity (`SELECT ... FOR UPDATE SKIP LOCKED`) means
   only one worker is ever writing a row for a given `(agent, prompt_id,
   trigger_event)` at a time, and `ORDER BY created_at DESC LIMIT 1` stays
   correct even across a crash-and-retry (a reclaimed job's second attempt
   still finds its own, most recent row). Zero blast radius on M0.4's
   existing contract or its two test files.
2. **`leads.profile jsonb` (migrations/0003_lead_profile.sql).** The
   `build_prospect_profile` output has nowhere to persist — `leads` has no
   `attributes` column (only `companies`/`contacts` do), and
   `prompts/qualification/score_lead.md` (M1.2, future) needs it back as its
   `prospect_profile` variable across a job/event boundary the generating
   process won't survive. Nullable, additive, plain `ALTER TABLE ADD
   COLUMN` — no backfill needed (no agent code existed to have written
   anything yet).

**Correction 1 (post-plan, before code): re-import idempotency made
structural, not check-then-act.** The originally planned mechanism was a
pre-check (`get_active_or_deferred_lead_by_contact`) before calling
`create_lead` — flagged, correctly, as a two-operation race: two concurrent
imports of the same CSV could both pass the check and both insert a deferred
placeholder, since neither `one_active_lead_per_contact` nor
`one_active_lead_per_company` (migrations/0001) excludes a *second*
`status='deferred'` row for the same contact — only the *active*-lead case
was ever structurally guarded.

Fixed the same way D2/R1 fixed the active-lead case: a new partial unique
index, `migrations/0004_one_deferred_lead_per_contact.sql`
(`ON leads (contact_id) WHERE status = 'deferred' AND deleted_at IS NULL`).
`repositories.create_lead()`'s deferred-insert branch now targets it with
`ON CONFLICT (contact_id) WHERE status = 'deferred' AND deleted_at IS NULL
DO NOTHING`; on conflict, the existing deferred row is re-read and returned
instead of a second one being created. The application-level pre-check is
kept in `scripts/import_leads.py` — but explicitly as an optimisation (skips
redundant DB/LLM work for an already-imported row), not as the guarantee.
`DuplicateActiveLeadError` (the *active*-lead race, already structurally
guarded since M0.2) is now also caught in `_import_row` and converted to the
same idempotent re-emit path, for the same reason: a caller that loses that
race must get a typed, idempotent outcome, not a crash.

New protected test:
`test_concurrent_imports_of_same_csv_produce_one_lead_and_one_lead_captured_event`
(`tests/integration/test_import_leads.py`) — two genuinely independent
`asyncpg` connections (two real concurrent calls to
`scripts/import_leads.py::run()`, not two coroutines sharing one connection)
race to import an identical single-row CSV; asserts exactly one `leads` row
and one `lead.captured` event survive.

**Correction 2 — checked empirically, not accepted as stated.** The
instruction that reached this round said the `import_note` attribute
envelope's `evidence: null` "fails the attribute validator (evidence must be
a string; `""` allowed, null not)" and framed using `evidence: ""` instead
as a defect the validator was catching. Ran the actual check before writing
either version:

```
$ uv run python -c "
import json, jsonschema
schema = json.load(open('schemas/entities/attribute.json'))
v = jsonschema.Draft202012Validator(schema)
env = {'value': 'x', 'confidence': 1.0, 'evidence': None, 'source': 'human:manual_import', 'run_id': None, 'observed_at': '2026-08-25T00:00:00Z'}
print('evidence=None errors:', [e.message for e in v.iter_errors(env)])
print('evidence=empty-string errors:', [e.message for e in v.iter_errors(dict(env, evidence=''))])
"
evidence=None errors: []
evidence=empty-string errors: []
```

Both pass. `evidence`'s schema type is `["string", "null"]`, and its own
description names "human entry" as an explicit example of where `null` is
correct — this is not a defect; entity-model.md §2 says so directly.
**Implemented the requested change anyway** (`import_note`'s `evidence` is
`""`, not `null`, in `scripts/import_leads.py`) — the instruction's outcome
is harmless and was explicit — but recorded the real fact here rather than
writing a fictitious "validator caught a defect" claim into this log: no
defect exists, both values are valid, `""` was chosen by instruction, not by
necessity.

**`orchestrator/router.py` needed no change.** `"lead.captured":
[JobSpec("leadgen.enrich")]` was already present from M0.3, added in
anticipation of this milestone. The real wiring gap was
`scripts/run_worker.py`'s `HANDLERS` dict, empty since M0.3 by design
("no agents exist until M1.1+"). Fixed there:
`HANDLERS["leadgen.enrich"] = leadgen.handle_enrich`. Confirmed end-to-end,
not just unit-level, by
`test_lead_captured_routes_through_worker_to_leadgen_and_emits_lead_enriched`
(`tests/integration/test_leadgen.py`), which emits a real `lead.captured`
event and drives it through the real `run_worker.dispatch_one_event` +
`run_worker.process_one_job` + the real `HANDLERS` registration (only the
Anthropic client is monkeypatched to a stub, via
`monkeypatch.setitem(run_worker.HANDLERS, ...)` — the routing/dispatch/claim
machinery itself is untouched) — asserting the enqueued job's `type` is
literally `"leadgen.enrich"`, the job completes, and `lead.enriched` is the
resulting event.

**Judgment calls, logged as approved (not re-litigated), plus the
`no partial write` mechanism and a status-model gap surfaced while
implementing:**

- **CSV's bare `linkedin_url` column → the contact's personal profile**, not
  the company's — the one genuinely ambiguous column in an otherwise settled
  contract (every other unprefixed column, `company_name`/`domain`, is
  unambiguous).
- **`ManualCsvProvider.verify_email()` is syntax-only** (regex check;
  `INVALID` if malformed, `UNVERIFIED` otherwise, never `VALID` — a syntax
  check alone can't confirm deliverability, and returning `VALID` from it
  would be a guess wearing a confident label). No vendor adapter built.
  `csv_path` is optional on `ManualCsvProvider.__init__` specifically so
  `agents/leadgen.py` can construct an empty instance to reuse this
  instance-independent method without a CSV to parse — its own discovery
  data (`.errors`, companies, contacts) is simply empty in that case.
- **`raw_research` fed to all three prompts is built from exactly what's
  known** (company name/domain, contact name/title/LinkedIn, the
  `import_note` attribute if present) — no web search/fetch tool exists yet
  (out of scope for M1.1). The prompts are explicitly designed for input
  this sparse (phase1-llm-boundary.md §2, §5's sparse fixtures): null values
  and `insufficient_context: true` are the correct, honest output, not a
  defect to work around.
- **Per-field confidence gate:** an attribute envelope is written only if
  `confidence >= pack.scoring.min_confidence_to_store` (0.4 in
  `b2b-service-firms.yaml`) — phase1-llm-boundary.md §2's "code drops
  anything below `enrichment.min_confidence` rather than storing a guess
  with provenance that makes it look trustworthy." `insufficient_context`
  itself is informational only — it does not block writing whatever
  individual fields did clear the threshold, and does not route to
  `lead.enrichment_failed` (that's reserved for real validation failure
  after retries, not "the model found little evidence").
- **`tech_signals`, `likely_responsibilities`, `inferred_pains` are each
  stored as one array-valued envelope**, not per-item — these schema fields
  carry a confidence per item (or, for `tech_signals`, none at all), not one
  for the field, so there's no single model-supplied top-level confidence to
  read. The envelope's `confidence` is the max across items (`tech_signals`
  items are treated as confidence 1.0, since the schema already requires
  each to cite `evidence` to be included at all); `evidence` is `null` for
  these three (an aggregate of several items' individual reasoning has no
  one quotable snippet).
- **`lead.enrichment_failed.attempts` is fixed at 2** for an LLM validation
  failure — `complete_json()`'s own internal retry-once-then-raise
  (`_MAX_ATTEMPTS = 2`) is what "after retries" in this milestone's required
  test means; agent-contracts.md's "after 3 attempts" language isn't
  precisely defined at this milestone (it could mean job-level queue
  retries, a distinct future escalation policy, or this) and wasn't
  resolved by any prior decision. Flagged here rather than guessed
  silently — revisit if a future milestone gives this a firmer
  specification. `attempts=1` for the `no_domain` case (no LLM call was
  ever attempted).
- **"No partial write" is enforced by ordering, not a transaction wrapping
  all three LLM calls:** `agents/leadgen.py::handle_enrich` runs all three
  `complete_json()` calls to completion (or catches the first
  `LLMValidationError`) *before* any write to `companies`, `contacts`, or
  `leads.profile`. A company is never enriched with no matching contact
  enrichment, or vice versa.
- **Lead status has no dedicated "enriched" value** —
  `migrations/0001_init.sql`'s `leads.status` enum has `enriching` and
  `enrich_failed` but nothing for "enrichment succeeded, not yet scored."
  `handle_enrich` sets `enriching` at the start and, on success, leaves it
  there — the `lead.enriched` *event* is the success signal; qualification
  (M1.2) is what will move a lead to `scored`. Not treated as a gap to fix
  now: entity-model.md's status enum is locked schema, and inventing a new
  status value wasn't asked for or needed by anything M1.1 builds.

**Tests, matching the required list exactly, all against a real Postgres
instance:**
- Protected: `test_reimporting_same_csv_is_a_noop`,
  `test_row_for_company_with_active_lead_emits_deferred_and_import_continues`,
  `test_enrichment_writes_provenance_envelope_not_bare_scalar` (validates
  every written envelope against the live `schemas/entities/attribute.json`
  validator, not just "is a dict"; also asserts a below-threshold field —
  `sub_industry`, stubbed at confidence 0 — was never written),
  `test_concurrent_imports_of_same_csv_produce_one_lead_and_one_lead_captured_event`.
- Standard: `test_row_missing_domain_or_email_is_rejected_others_still_import`,
  `test_enrichment_failure_after_retries_emits_lead_enrichment_failed_no_partial_write`
  (stub client that never returns valid JSON — a real `LLMValidationError`,
  not simulated — asserts no `lead.enriched` event, empty `companies.attributes`,
  `leads.profile` still null),
  `test_lead_captured_routes_through_worker_to_leadgen_and_emits_lead_enriched`.
- Plus `test_lead_with_no_company_emits_enrichment_failed_reason_no_domain` and
  a `ManualCsvProvider()`-with-no-path sanity check, not explicitly required
  but covering paths the milestone's own design introduced.

## 2026-08-26 — M1.1 NOT VERIFIED gap closed: concurrency stability + real-model sparse-input golden test

**Context:** two items were left NOT VERIFIED at M1.1 close (docs/verification-loop.md
§7): the concurrent-import test had only been run once, and every M1.1 LLM call had
only ever run against a stub — `build_prospect_profile`, the call that produces
`personalization_anchors` (the only facts `sales/draft_initial_outreach.md` is
permitted to assert), had never met a real model on genuinely sparse, CSV-only input.

**Decision 1 — concurrency stability:**
`tests/integration/test_import_leads.py::test_concurrent_imports_of_same_csv_produce_one_lead_and_one_lead_captured_event`
run three times consecutively, same discipline as M0.3's concurrent-claim test. All
three: PASSED (0.79s, 0.75s, 0.77s). No flake.

**Decision 2 — added `tests/golden/test_leadgen_sparse.py` + `tests/fixtures/leadgen_sparse.csv`,**
driving the real chain (`ManualCsvProvider` → `enrich_company` → `enrich_decision_maker`
→ `build_prospect_profile`, three real `complete_json()` calls) on a fixture row with
only the required CSV columns (`company_name`, `domain`, `contact_email`) plus a
content-free `source_note`.

**The fixture as first written was invalidated by its own company name.** The original
row used `Thornbridge Facilities Group` / `thornbridgefacilities-fixture.example` — the
literal word "Facilities" in both the name and domain is real, quotable context under
`enrich_company.md` rule 1, and rule 4 explicitly permits a capped-confidence inference
"from adjacent facts." The first real-model run returned `industry: "Facilities
Management"` (not null), failing the test's blanket assertion that `industry,
sub_industry, business_model, employee_band` are all null. Eight follow-up runs against
the original fixture confirmed this wasn't one-off: `industry` and `employee_band` stayed
null every time, but `business_model` was populated in 5 of 8 runs at confidence 0.4–0.6
(twice at 0.6, exceeding rule 4's stated 0.5 cap), always citing the company name text
itself as evidence. This is the model doing exactly what the prompt tells it to do —
extract from real context, cap confidence on an inference — not fabrication. The fixture,
not the model or the prompt, was the defect: a "genuinely sparse" fixture must not
smuggle in a real, checkable industry signal through the one field (company name) that's
always present. Renamed the fixture to `Thornbridge Meridian Group` /
`thornbridgemeridian-fixture.example` — a plausible but industry-neutral name — and kept
everything else (required-fields-only row, blank title, uninformative `source_note`)
unchanged.

**Stability evidence:** with the corrected fixture, five standalone runs of the
`enrich_company` call returned nulls across all six inferred fields
(`industry`/`sub_industry`/`business_model`/`employee_band`/`revenue_signal`/
`positioning_summary`) with `insufficient_context: true` every time. The full golden
test (all three chained calls, plus the V9 no-email-regex check and the
`personalization_anchors == []` / `insufficient_context: true` assertions on
`build_prospect_profile`) was then run three times consecutively: PASSED all three
(21.26s, 10.75s, 10.67s).

**Consequence:** `tests/golden/test_leadgen_sparse.py::test_sparse_csv_row_yields_no_fabricated_facts`
is the first real-model coverage of the anchor-fabrication risk on sparse CSV-only
input — the gap this fixture and test exist to close. Full suite re-verified after
both changes: 236 unit/contract/integration tests pass, 14/14 golden tests pass
(13 prior + this one), `ruff check`/`ruff format --check` clean. The confidence-cap
overshoot (0.6 against a stated 0.5 ceiling) observed on the original fixture is a
real, minor prompt-calibration finding, logged here rather than silently dropped —
worth a tightened wording pass on `enrich_company.md` rule 4 if it recurs elsewhere,
but out of scope for this fix since the corrected fixture no longer exercises that
inference path at all.

## 2026-08-26 — M1.2: Lead Qualification agent (deterministic + LLM hybrid scorer)

**Context:** implements agent-contracts.md §2 and entity-model.md §3.5. The plan was
approved with three corrections; this entry logs the resulting decisions.

**Correction 1 — disqualifiers the scorer cannot evaluate must fail the boot, not
sit silently.** `icp.disqualifiers[].rule` mixes clean `field == "value"` / `field in
[...]` comparisons with `matches` against unstructured/LLM-inferred text
(`positioning`, `industry`) that has no defined semantics anywhere in the docs.
Added `core/disqualifiers.py`: a small, closed grammar (`FIELD == "VALUE"` / `FIELD
in [...]`, `AND`-combined, over `employee_band`/`business_model`/`revenue_signal`
only). `core/config.py::load_config()` now calls `parse_rule()` on every
`icp.disqualifiers[]` entry not marked `enforcement: manual` and raises `ConfigError`
if it can't parse (`_assert_disqualifiers_evaluable`) — verified live: deleting
`enforcement: manual` from `regulated_health` in a scratch copy of the real pack
raises `ConfigError` citing the exact clause it can't parse.

**Chose `enforcement: manual` over implementing `matches`**, for both
`bespoke_creative` and `regulated_health`: a hand-rolled fuzzy-text matcher for a
PHI-compliance gate would still be a guess wearing a confident label — exactly the
"reads as protection that isn't there" failure mode this correction exists to catch,
just moved one layer down. `enforcement: manual` is also what the pack's own
pre-existing header comment already claimed this section should be ("Human-judgment
disqualifiers live in qualification.discovery_checklist... do NOT put them here").
Added `qualification.discovery_checklist` entries `bespoke_project_work` and
`regulated_health_data` so "the discovery checklist covers them" is literally true,
not aspirational — schemas/entities/industry_pack.json gained the optional
`enforcement` enum (`scored`/`manual`) on disqualifier entries.

**Correction 2 — engagement must count meetings, not just replies.**
`agents/qualification.py` now reads `meetings` directly (`repo.count_meetings`), not
only `messages` (`repo.count_inbound_messages`); `docs/agent-contracts.md` §2's Reads
row updated to add `meetings` (was previously incomplete against agent-contracts.md's
own prose, which already said "meetings" without the Reads row listing the table).
Points-per-signal (`reply: 1.0`, `meeting: 5.0`) and a saturation cap live in the pack
(`scoring.engagement_points` / `engagement_saturation`, new required pack keys) —
never literals in code (CLAUDE.md §3). `open`/`click` are absent from the formula
entirely, not present at weight 0: `messages` has no `opened_at`/`clicked_at` column,
so there is nothing to count yet.

**Correction 3 — the LLM sub-score combination formula must be config, not code.**
Added `scoring.llm_subscore_weights` (new required pack key: `buying_intent`,
`seniority_fit`, `narrative_fit`, defaulting to equal thirds — 0.3334/0.3333/0.3333
so they sum to exactly 1.0) and `_assert_llm_subscore_weights_sum_to_one`, asserted at
boot with the same `+/- 0.001` tolerance as `scoring.weights`. `_combine_llm_subscores`
reads it from the lead's PINNED pack (never `get_config()`), same as every other
scoring weight.

**Confirmed unchanged from the approved plan:** `load_config(industry_pack=lead.industry_pack)`
never `get_config()` (`_pinned_pack`, `@cache`d per pack name, mirroring
`get_config()`'s own process-lifetime caching); `icp_match` and `size_fit` kept
distinct (same "1-10" band scores 1.0 in one, 0.5 in the other — see
`test_icp_match_and_size_fit_do_not_double_count_the_same_band`); a disqualifier hit
forces `band=cold` but the numeric `total` is still stored for audit
(`test_disqualifier_hit_forces_cold_regardless_of_other_components` constructs a lead
that would otherwise score >60 on every other axis); no `orchestrator/router.py`
edits (`lead.enriched`/`reply.received` → `qualification.score` were already wired at
M0.3); no new migration (`lead_scores` already existed in migration 0001).

**A real precision bug found and fixed before the reconciliation test was written:**
independently rounding `total`, `deterministic_part`, and `llm_part` to 2 decimal
places from their underlying floats does not guarantee
`deterministic_part + llm_part == total` (each can round a different direction).
Fixed by rounding the two parts first and deriving `total` as their exact `Decimal`
sum, then using that same rounded total for band assignment and every emitted event
payload — never three independently-rounded numbers that can silently disagree by a
cent.

**Duplication, not shared refactor:** `_icp_definition_text` in
`agents/qualification.py` duplicates a same-purpose helper already in
`agents/leadgen.py`, rather than promoting both to a shared `IndustryPack` method in
`core/config.py`. CLAUDE.md §4's "minimum abstraction... until there are two real
implementations" would justify sharing now that a second caller exists, but this
milestone was scoped to the qualification agent only; touching M1.1's shipped
`leadgen.py` (even a pure, non-behaviour-changing refactor) was judged out of scope
rather than assumed in-scope. Worth revisiting if a third caller appears.

**`reply.received` still has no `schemas/events/reply.received.json`** — deliberately
out of this milestone's deliverable list, and no emitter for it exists yet (the sales
agent is M1.4). The re-score path is tested by enqueueing a `qualification.score` job
directly via `repo.enqueue_job()` with a `reply.received`-shaped payload
(event-catalog.md §3's documented shape) — jobs carry no schema, only events do, so
this needed no schema file. Where the test needed a real `events` row to satisfy
`agent_runs.trigger_event`'s foreign key, it used `repo.emit_event()` directly (the
repository primitive under `core/events.py::emit()`, which persists without schema
validation — schema validation is `core/events.py`'s job, not `repositories.py`'s).

**`leads.status` after scoring is always `SCORED`, regardless of band** — `QUALIFIED`
status was judged to more plausibly belong to a later, human-gated lifecycle step
(post discovery-call), not automated banding; revisit if a future milestone needs
otherwise.

**Tests:** `tests/unit/test_qualification_scoring.py` (16 tests, pure functions, no
DB) — protected: byte-identical determinism, band-threshold-exact (parametrized
across all four boundaries), disqualifier-forces-cold. Standard: absence-of-evidence
never disqualifies, manual-enforcement rules are skipped, icp_match/size_fit stay
distinct, two differently-weighted packs produce different totals, LLM sub-score
weights are honoured, meetings move the engagement score. `tests/unit/test_config.py`
gained 7 tests for the two new boot-time assertions (llm_subscore_weights sum,
disqualifier evaluability — both directions, plus a regression guard that the real
shipped pack still boots). `tests/integration/test_qualification.py` (5 tests) —
protected: inbound bypass emits both `lead.qualified.{band}` and
`lead.routed_to_human`. Standard: append-only re-scoring across a real
`lead.enriched` → `reply.received`-shaped re-score sequence (asserts the OLD row is
byte-unchanged), `deterministic_part + llm_part == total` reconciliation, a real
`lead.enriched → router → qualification.score job → handler → lead.scored →
lead.qualified.X` path through the actual worker (`run_worker.dispatch_one_event`/
`process_one_job`/`HANDLERS`), and a disqualifier hit banding a lead cold end-to-end
even under a maximally generous stub LLM response.

**Verification:** `scripts/migrate.py` (no-op, 4 previously applied — no new
migration was needed), `ruff check`/`ruff format --check` clean, `mypy --strict` on
`core/`+`db/` clean (and `agents/qualification.py` clean under plain `mypy` too, not
just strict-scoped), full suite 269 passed (was 236 before this milestone),
`pytest -m protected --collect-only` confirms every required protected test exists,
and the post-suite `schema_migrations` integrity check against the persistent
session-local `DATABASE_URL` Postgres (docker-compose, used throughout M0.1-M1.1 and
this milestone — not a freshly disposable instance) shows exactly the same 4 rows as
before the run.

**Consequence:** `scripts/score_distribution.py` is ready to report calibration
(median/quartiles/band counts of `llm_part` per prompt version) once real leads
exist, per phase1-llm-boundary.md §6 — the prompt itself was not touched, per the
explicit instruction not to tune it without real data.

## 2026-09-03 — M1.3: the approvals gate (human-in-the-loop)

**Context:** implements CLAUDE.md §1 non-negotiable 8, build-spec §0.7, agent-contracts.md
§0.4. The plan surfaced four conflicts between the task text and the binding docs /
already-shipped schema before any code was written; all four were resolved per explicit
instruction (see the plan-approval turn) rather than picked silently:

1. `approvals` already existed (migrations/0001_init.sql), pre-built to match
   event-catalog.md §7.1's action_type list exactly — migration 0005 `ALTER`s it
   (migrations are forward-only), never redefines it.
2. Kept the shipped `granted`/`denied` vocabulary (matches the CHECK constraint,
   event-catalog.md, and orchestrator/router.py's existing references) over the task
   text's `approved`/`rejected` phrasing.
3. `is_approved(conn, approval_id)` exactly as specified — no token parameter, even
   though CLAUDE.md/agent-contracts.md/build-spec all describe "an approval token."
   A `token` is still generated (`secrets.token_urlsafe(32)`) and stored on every row
   (satisfies "a token exists" descriptively; migration 0001's `token UNIQUE` column
   is finally populated), but the gate itself checks only `approval_id` + `status`.
   Tightening this to require the token later is a one-line change to `is_approved()`,
   not a migration — the column and the value are already there.
4. Autonomy gating direction: agent-contracts.md §0.4's real semantics (A0 = no
   external side effects to gate; A2 = approval required), not the task text's
   inverted phrasing. Config-driven via `config/thresholds.yaml`'s
   `approvals.autonomy_requires_approval` (default `[A2, A3]`) — `core/approvals.py`
   never hardcodes a level comparison.

**slack_sdk dependency — pinned, scoped down from the initially-resolved extra.**
`slack_sdk[optional]>=3.27` (the SDK's own documented way to get Socket Mode) resolved
to 22 packages including `boto3` and `SQLAlchemy` — unrelated optional installation-store
backends, not anything Socket Mode itself needs. Switched to `slack_sdk>=3.27` +
`aiohttp>=3.9` as two explicit, individually-justified dependencies (aiohttp is what
`slack_sdk.socket_mode.aiohttp.SocketModeClient` actually requires) — 7 packages instead
of 22, none of them AWS or database-adjacent. `slack_sdk`/`aiohttp` are used for Socket
Mode transport only and do not appear anywhere in `core/` or `db/` — verified by
`core/approvals.py` having zero Slack-related imports, which is what makes it fail
closed (see below).

**THE PROPERTY THAT MATTERS, architecturally:** `core/approvals.py::request_approval()`
never talks to Slack. It inserts the `pending` row and emits `approval.requested`;
`integrations/slack.py::handle_notify_approval_request` is a SEPARATE job, routed from
that event (`orchestrator/router.py`'s `approval.requested` moved from `UNCONSUMED` to
`ROUTES` this milestone), that posts to Slack. A Slack failure — down, unreachable,
misconfigured, or the app deleted entirely — can only ever fail that notification job
(retries, eventually dead-letters); it cannot touch the committed row. `is_approved()`
reads `approvals` and nothing else. Per the explicit note on the plan-approval turn, the
protected test proves this by asserting the row is still `pending` and `is_approved()`
still returns `False` after the Slack client raises on every call — not by asserting the
job dead-lettered, which would only prove the notification failed, not that nothing
became executable.

**Append-only enforced twice, deliberately redundant.** `resolve()`/`expire_stale()`'s
own `UPDATE ... WHERE status = 'pending'` guard is sufficient for every code path that
uses it (a second attempt affects zero rows — `ApprovalAlreadyDecidedError`, not a
re-decision). Migration 0005 also adds a `BEFORE UPDATE` trigger
(`approvals_forbid_redecision`) that rejects ANY update to an already-decided row,
including a raw `UPDATE` that bypasses `db/repositories.py` entirely — verified live
against a real Postgres instance (a raw `UPDATE approvals SET decided_by='hacker' ...`
after a grant raises `CheckViolationError`) before writing the corresponding protected
test. This is the milestone's own explicit design goal ("a future agent cannot route
around it even by accident"), not the WHERE-guard alone.

**Idempotent dedupe, not an error, on a duplicate pending request.** `dedupe_key`
(new column) + a partial unique index (`one_pending_approval_per_dedupe_key`, same
pattern as `one_active_lead_per_company`/`one_deferred_lead_per_contact`) — a second
`request_approval()` call for the same still-pending logical action returns the
EXISTING row rather than raising, matching this codebase's own established idiom
(`emit_event`'s idempotency_key, `create_lead`'s deferred-row handling), not a new
pattern invented for this milestone.

**`request_approval()` always returns a real row, even when autonomy doesn't require
approval.** A0/A1 actions get an immediately-`granted` row (`decided_by="system:auto"`),
not `None` or a skipped insert — every call site gets something to log/reference
regardless of level, and `is_approved()` never needs a special case for "this action
type doesn't gate." `approval.requested` is NOT emitted for the auto-grant path (nothing
needs a human); `approval.granted` is, for audit consistency.

**event-catalog.md §7.1's escalate branch genuinely never cancels.** For
`on_expiry: escalate` action_types (`proposal_send`, `pricing_discount`,
`campaign_launch`, `icp_update`, `crm_merge`, `record_delete`), `expire_stale()` leaves
`status='pending'` completely unchanged past the TTL — only `approval.expired`
(`action_taken: "escalated"`) fires, exactly once per approval ever (idempotency_key has
no attempt counter, so a later scheduler tick finding the same still-pending row is a
clean no-op, not a repeated notification). `record_delete`'s `ttl_hours: null` means it
is never swept at all — never autonomous, at any confidence, under any config.
The recurring "still waiting" safety net event-catalog.md §7.1 describes (the daily
digest) is `orchestrator/schedules.py`, explicitly not built this milestone.

**Scope boundary on router.py's `approval.denied`/`approval.expired`:** both stay
`UNCONSUMED`, reasons updated (no longer "Slack integration is M1.3, not built" — it now
is). `approval.denied`'s real-time notification already happens synchronously inside
`integrations/slack.py::handle_interaction_payload` (it updates the Slack message
directly as part of resolving the decision) — no separate routed job needed.
`approval.expired`'s one-time Slack post was judged out of scope: the digest is the
documented recurring mechanism, and building an ad hoc one-off notifier for expiry
alongside it would duplicate that eventual mechanism rather than lead into it.

**agent-contracts.md §2 (qualification) is unaffected — this entry is here, not there,
because M1.3 touches shared infrastructure (`core/config.py`, `db/repositories.py`) that
M1.2 also touches, not because M1.2's own contract changed.**

**Tests:** `tests/unit/test_approvals_gating.py` (7, pure `requires_approval()`/
`compute_expires_at()`, no DB — named `_gating` not `_approvals` to avoid a pytest
module-name collision with `tests/integration/test_approvals.py`, same convention as
M1.2's `test_qualification_scoring.py` vs `test_qualification.py`).
`tests/unit/test_slack_blocks.py` (6, pure Block Kit rendering, no I/O) — includes a
regression guard that normal-sized content is never summarised/truncated, and that an
oversized payload is flagged rather than silently cut (Slack's ~3000-char block limit is
a real, unsolved M1.4-era question this milestone surfaces rather than hides).
`tests/unit/test_config.py` gained 8 tests for `thresholds.yaml`'s boot-time validation
(missing action_type, invalid `on_expiry`, negative `ttl_hours`, invalid autonomy level,
null-ttl acceptance). `tests/integration/test_approvals.py` (10) — protected: nothing
executes without a committed granted row (tested by driving `is_approved()` directly
through pending/denied/expired states, not just the happy path), a resolved approval
rejected-by-the-database on redecision (both the typed-error path AND a raw SQL bypass
hitting the trigger directly), an expired approval unapprovable afterward. Standard:
dedupe idempotency, granted-exactly-once, expire_stale's cancel and escalate branches
(including the "fires once across repeated ticks" check), record_delete never swept,
auto-grant for non-gating levels. `tests/integration/test_slack.py` (8) — protected: the
Slack-client-raises property described above, PLUS a matching case for a misconfigured
`SLACK_APPROVAL_CHANNEL` (an equally real "misconfigured" manifestation, not just a
hypothetical), PLUS a Slack failure during the interaction-callback's message update not
blocking a resolve() that already committed. Standard: full path (request → stubbed
Slack post → button click → resolve → `approval.granted`, exercised through the real
module functions), reject path, skip-posting for an already-decided approval, and a
double-click on a decided approval updating the message instead of raising.

**Verification:** `scripts/migrate.py` applied migration 0005 cleanly against both
`DATABASE_URL` (dev) and `TEST_DATABASE_URL`, confirmed idempotent on a second run.
`ruff check`/`ruff format --check` clean. `mypy --strict` on `core/`+`db/` clean
(`core/approvals.py` is in that strict scope; `integrations/slack.py` passes plain
`mypy` too). Full suite: 311 passed (up from 269 before this milestone — +42: 7+6+8
new unit tests, 18 new integration tests, minus none removed). `pytest -m protected
--collect-only` — 70 protected tests (up from 62), confirmed every one this milestone's
plan required exists. Post-suite `schema_migrations` integrity check against the
persistent session-local `DATABASE_URL` Postgres shows exactly 5 rows (0001-0005) and
zero rows in `approvals` — nothing from this milestone's manual smoke-testing or
automated tests leaked into the dev database; all of it stayed on `TEST_DATABASE_URL`
(confirmed empty after the suite too).

**Consequence:** M1.4 (Sales agent, send path) is the first real caller of
`request_approval()`/`is_approved()`. Not started this milestone, per explicit
instruction — no sales agent, no send path, no `orchestrator/schedules.py` (the daily
digest), no "lead returns to prior status" logic (`core/approvals.py`/
`integrations/slack.py` never touch `leads`; that's Sales's job when it eventually
consumes `approval.expired`).


## 2026-09-15 — M1.4a: outreach drafting and the send path

**Context:** implements `docs/deliverability.md` (the specification), agent-contracts.md §3's
draft and send gates, and CLAUDE.md §1.8/§6. The plan surfaced eight decisions; all were
answered on the plan-approval turn and are recorded here with their consequences.

**The gate (core/sending.py).** Every send passes, immediately before the Gmail call and
against live database state: from-domain, dev sandbox, health (§6 pause), message state,
approval (`core.approvals.is_approved()` for the approval bound to *this* message, whose
approved subject/body/recipient/sender must equal the row being sent), §7 content,
suppression (address AND domain, plus `contacts.email_status`), caps (rolling 24h, rolling
60 minutes, `min_gap_seconds`), and the recipient-local send window. `can_send()` returns a
typed `SendDecision` naming the gate, reason, and disposition — never a bare bool. A gate that
cannot be evaluated (config absent, table missing, database error) is itself a refusal
(`gate=evaluation`); if even the refusal cannot be recorded, `authorize_send()` raises
`SendGateEvaluationError` and the job dead-letters. Nothing ever proceeds on "unknown".

**Race and double-send safety.** `authorize_send()` takes a transaction-scoped advisory lock
on the sending domain, evaluates every gate, and reserves the message (`drafted -> sending`) in
one commit; a reservation counts against the cap immediately. Verified by a test that runs two
`authorize_send()` calls concurrently on two connections against a cap of 1: exactly one is
authorized. A reservation that outlives `authorization_ttl_seconds` without a recorded outcome
is marked `send_unknown` and never resent automatically — a duplicate email is irreversible, a
stuck row is not. The send state machine is also enforced by a database trigger
(`messages_send_state_transition`): no transition leads back to `drafted` or out of `sent`.

**Stated plainly, not closed:** a suppression inserted in the milliseconds between the
reservation commit and the Gmail API call cannot be caught. No database lock can span an
external HTTP call. The window is bounded by `authorization_ttl_seconds` (60s maximum; in
practice the call follows the commit immediately), not eliminated.

**`integrations/gmail.py::send` accepts only a `SendAuthorization`**, which only
`authorize_send()` can mint (module-private sentinel), is single-use, and expires. The dev
sandbox redirect lives inside `send()` itself (CLAUDE.md §6): outside production, all mail goes
to `DEV_SANDBOX_EMAIL`, an unset `ENV` counts as not production, and an unset sandbox address
refuses. `send()` also refuses unless the authenticated Gmail account is the configured
`from_address`. httpx, not google-api-python-client (synchronous; CLAUDE.md §4) — no new
dependency.

**Decision 1 — mql.** Pack key `outreach.draft_bands`, `[sql]` in the shipped pack. The router's
`lead.qualified.*` now routes sql and mql to `sales.draft_outreach` (renamed from
`sales.start_sequence`: the sequence state machine is M1.4b); the handler no-ops unless the band
is listed. `lead.qualified.mql` was removed from `UNCONSUMED`; `tests/unit/test_router.py`'s two
assertions encoding the old routing were updated to the approved routing (neither was
protected).

**Decision 2 — `allowed_email_statuses: [valid]`, fail closed.** Every current lead is
`unverified` (`ManualCsvProvider.verify_email()` never returns `valid`), so **with this default
M1.4a sends nothing to real prospects until an email verification provider exists.** That is
correct behaviour, not a bug: bounces are what trigger the hard pause. Do not loosen the default
to work around it. `suppressed`, `bounced` and `invalid` are refused regardless of this list
(entity-model.md D6).

**Decision 3 — no anchors, no draft.** `handle_draft_outreach` skips before any LLM call and
records `outreach.blocked` (`reason=no_personalization_anchors`, `disposition=skipped`,
`draft_id=null`). A generic email with no specific reason to contact someone is the
low-quality outreach that generates complaints, and complaints kill the domain.
**Consequence:** the sparse-input golden test (2026-08-27) showed CSV-only leads produce zero
anchors, so **with current data nothing drafts at all.** The fix is better input at import time
(a real reason to contact each lead captured in the CSV or by enrichment), not relaxing this rule.
The draft path also skips, before spending a model call or a human's approval, when the contact's
email status is not allowed or the address/domain is already suppressed; each is re-checked at
send time regardless.

**Decision 4 — timezone.** `default_recipient_timezone: America/New_York`, deliberately not
`sender_timezone`: its job is the business hours to assume for a recipient who cannot be placed,
and the ICP is US/UK/EU. Resolution order: a valid contact `timezone` attribute, then
`companies.country` via `deliverability.country_timezones` (multi-zone countries map to their
most populous business zone), then the default. An unplaceable recipient falls back to the
default rather than being refused — refusing would block every CSV lead over an unknowable field.

**Decision 5 — rolling 24h daily cap.** A calendar day allows 5 sends at 23:59 and 5 more at
00:01; a rolling window never does.

**Decision 6 — health sample floor.** Below 50 sends in the 7-day window, rates do not evaluate;
2 hard bounces or 1 spam complaint pauses instead. At or above 50, §6's rates apply as written.
Floor and counts are config; `docs/deliverability.md` §6 was updated to match. Bounce metrics
count `hard_bounce` suppressions only (soft bounces are temporary), and only address-level rows,
so an unsubscribe that also writes a domain-wide row is not double-counted. Until M1.4b ships
bounce and unsubscribe detection, the only writers are `scripts/suppress.py` and future code —
the metrics will read near zero, and there is no automated complaint detection at all (Postmaster
Tools is a manual check).

**Decision 7 — events, scripts, warnings.** New `sending.paused`, `sending.resumed` and
`sending.health_warning` events (schemas, catalog entries, routed to
`slack.notify_sending_alert`). `sending.health_warning` is the one event beyond the approved list:
it is the trigger the approved once-daily Slack warning needs (idempotency key per metric per UTC
day). `outreach.blocked` extended additively (reasons, `gate`, `detail`, `disposition`,
`deferred_until`; `draft_id` nullable only for a draft-precondition skip). The pause is a
`sending_pauses` row with a one-open-pause index and a CHECK requiring a resumer and non-blank
reason. `scripts/resume_sending.py --by --reason` resumes and re-enqueues held drafts;
`scripts/suppress.py` adds manual address/domain suppressions. Suppressions are append-only by
trigger (§5: "never contactable again").

**Refusal dispositions.** `deferred` (cap/window/gap: a new send job for the retry time plus
jitter — not dropped, and not burned through retries into the dead-letter queue), `held` (health
pause, pending approval, sandbox or evaluation failure: the message stays `drafted` until
`resume_sending.py`), `blocked` (approval denied/mismatched, suppression, from-domain, content:
terminal).

**§7 content.** The opt-out sentence and physical address are config values appended at draft
time, so the approval payload — and the Slack message a human approves — contains exactly what
sends. `physical_address` is empty in the shipped config (pre-flight checklist item still open),
so the content gate refuses every send until it is set. The opt-out wording in config is a default
chosen for this milestone; edit it freely. The body word limit is enforced at draft time (V2);
approved content cannot change afterwards without failing the approval-match check.

**Also fixed while in core/approvals.py:** the comment claiming `approval.requested` is not
re-emitted on the dedupe path (the code does emit; the idempotency key makes it a no-op), and a
garbled `expire_stale` docstring example ("24h TTL — wait, escalates").

**Tests.** A suite-wide autouse fixture in `tests/conftest.py` makes the real Gmail transport
raise, so no test can consume the warmup account's 5/day even by forgetting to inject a stub.
The 9 protected send tests (8 required plus approval-content mismatch) each call the send handler
or gate directly, bypassing approval.granted, and assert refusal with the transport never called;
all 9 were confirmed to FAIL when the gate is forced to allow everything (mutation check), so
they depend on the gate rather than passing vacuously.

**Consequence — before any real prospect is emailed:** (1) an email verification provider
(decision 2); (2) import data that yields personalization anchors (decision 3); (3)
`physical_address` set; (4) M1.4b, because the opt-out sentence asks recipients to reply "stop"
and nothing processes replies yet — CAN-SPAM requires honouring opt-outs within 10 business days;
(5) the §10 pre-flight checklist. A domain-wide suppression of a free-mail domain (e.g.
`gmail.com`) would block every user of it — a write-side policy for M1.4b's unsubscribe handling.

## 2026-09-19 — M1.4a addendum: three email status tiers + the verification adapter

**Context:** `allowed_email_statuses: [valid]` refused catch-all domains outright, and B2B
firms at our ICP size frequently run catch-all — the flat list would have refused most
legitimate prospects. Catch-all nonetheless carries real bounce risk. Three tiers replace
the two-state allow-list, and the verifier that makes any of it reachable ships in the same
milestone: tiers with no verifier are inert, a verifier with no tiers has nothing to
interpret its answers, and building them apart means two milestones that each send nothing.

**Tiers (config/base.yaml, `deliverability.email_status_tiers`).** send: `valid`.
restricted: `catch_all` — sendable, sub-capped, bounces weighted double. never:
`unverified`, `risky`, `invalid`, `bounced`, `suppressed`, `disposable`, `role_based`.
`unverified` and `risky` sit in never-send because the first is "no verdict yet" and the
second is the verifier declining to confirm a mailbox — the same exposure as `invalid`
without the certainty. Boot validation asserts every `EmailStatus` member appears in
exactly one tier, so a status added later fails the boot rather than defaulting to
sendable by omission (the same posture as M1.2's disqualifier-evaluability check).
entity-model.md §3.2's enum row and deliverability.md §5/§6 were updated to match.

**`role_based` is never-send on two grounds, not one.** Those addresses carry bounce risk
AND they land in shared inboxes where cold email is deleted unread — the classification is
right for deliverability and for conversion independently. They are caught twice over: by
the verifier's `role` flag, and by a local-part check
(`deliverability.role_based_local_parts`) that spends no credit and works before any
verifier exists.

**The sub-cap is a share, not an absolute.** `catch_all_share: 0.4`, floored, minimum 1 —
2 of today's `daily_cap: 5`. `daily_cap` climbs 5 → 40 across the warmup ramp, and a
hardcoded 2 would quietly become absurdly restrictive at 40/day while nobody remembers it
exists. One number to update instead of two. No hourly sub-cap: `hourly_cap` and
`min_gap_seconds` already bind.

**Double-weighted catch-all bounces, accepted with their consequence.** Acceptance from a
catch-all domain never proved the mailbox existed, so a bounce there is the first real
evidence. Weighted by `messages.recipient_email_status` — the status snapshotted at
RESERVATION, not the contact's status now, which a bounce will since have changed to
`bounced`. Without that snapshot the weighting is unenforceable. Combined with the §6
sample floor (2 hard bounces pause below 50 sends), **one catch-all bounce in the first
fifty sends pauses sending outright.** Deliberate: a false pause costs a day and one
`scripts/resume_sending.py` run; a missed signal costs months of domain reputation.
`score` and `free` are stored as ordinary contact attributes and tiered on by nothing —
score is a vendor confidence number, and a founder at a 40-person agency legitimately using
a gmail.com address is squarely in the ICP.

**BLOCKING CONFLICT FOUND BEFORE IMPLEMENTING: `email_status` was being clobbered.**
`upsert_contact` overwrote `email_status` unconditionally on conflict (`= EXCLUDED`, unlike
every other column's COALESCE), and two callers passed the `UNVERIFIED` default:
`agents/leadgen.py` verified on every enrichment through `ManualCsvProvider.verify_email`
(syntax-only, never returns VALID), and `scripts/import_leads.py` re-imported without a
status. Enrichment runs *after* import, so the real sequence would have been: import spends
a credit and writes `valid` → enrichment immediately overwrites it with `unverified` → the
send gate refuses everything. It would have looked like a broken verifier rather than a
clobbered write, and the credits would have been spent either way. Resolved three ways:
(1) `upsert_contact` now preserves the stored status — the column is NOT NULL so this is a
plain assignment, not a COALESCE wrapper, which would have been a no-op dressed as a guard;
(2) `set_email_status()` is the only path that changes it, and refuses to set `unverified`,
so a verdict or a later bounce can never be undone by a re-import; (3) enrichment no longer
verifies at all — `lead.enriched` reports the stored verdict.

**`verify_email` removed from `ProspectingProvider`** (discovery-addendum.md §3/§4 updated).
Two interfaces both claiming to verify an address is precisely what produced the conflict
above; leaving it as dead code would have kept the trap for whoever builds the discovery
flow. `tests/integration/test_leadgen.py::test_manual_csv_provider_with_no_path_still_verifies_email`
tested the removed behaviour and was deleted, not weakened — its replacement is the new
verification test modules.

**Verification runs once, at import.** `integrations/email_verification.py` is an interface
plus one adapter (`EmailListVerifyProvider`), the same shape as `prospecting.py`. A contact
carrying any real verdict is skipped, so re-importing a CSV spends nothing; only
`unverified` contacts are looked up, which also means the contacts already in the database
get verified on their next import rather than being stranded — no backfill script needed.

**The JSON endpoint, flags before status.** EmailListVerify exposes `role`, `disposable`
and `accept_all` as booleans *orthogonal to* `status`: an address can be status `valid` AND
role true. A single-column lookup would have tiered that as `valid` and sent cold outreach
to `info@`. So flags are evaluated first (role → disposable → accept_all), then status
(`valid`→valid, `invalid`→invalid, `unknown`→risky). **Any unrecognised status word maps to
`unverified`** — never-send, and still eligible for a later lookup once the map is
corrected. `LEGACY_STATUS_MAP` records the older string endpoint's vocabulary so a future
fallback cannot silently mis-tier: `ok_for_all` is a SECOND catch-all spelling alongside
`accept_all`, and `email_disabled`/`dead_server`/`invalid_mx` all mean invalid.

**Every verification failure yields `unverified`, never `valid`** — API down, 402 out of
credits, timeout, or a malformed body. `EMAILLISTVERIFY_API_KEY` unset means
`default_provider()` returns None: imports still work and every contact stays unverified,
i.e. unsendable.

**Tests.** A second autouse guard in `tests/conftest.py` makes the real provider's
`verify()` raise — patched on the class, not on `default_provider`, because
`scripts/import_leads.py` binds that name into its own namespace at import and patching it
there would leave the real network path reachable from the one place it is actually called.
Mutation-checked, as for the send gate: forcing every status into the send tier fails the
sub-cap and never-send tests, and flattening the bounce weights fails the double-weight
test while leaving the valid-tier control passing — so both are load-bearing rather than
vacuously green.

**Consequence.** With a real `EMAILLISTVERIFY_API_KEY` and a re-import, addresses can now
reach the send tier for the first time — the send path stops being inert. The remaining
pre-send blockers are unchanged: personalization anchors at import (nothing drafts without
them), `physical_address` in config, and M1.4b for reply handling before any real prospect
is emailed.
