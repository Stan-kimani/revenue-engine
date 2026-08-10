# AI Revenue Engine

An autonomous multi-agent revenue system: lead generation, qualification, sales
outreach, meeting intelligence, CRM sync, a learning loop, and a marketing brain —
built as deterministic orchestration around single-purpose, schema-validated LLM
calls. No agent frameworks. No microservices. One Postgres database is the CRM.

See [`CLAUDE.md`](CLAUDE.md) for the binding operating rules and
[`docs/revenue-engine-build-spec.md`](docs/revenue-engine-build-spec.md) for the
full architecture.

## How it works, in one paragraph

Agents are roles, not services: a directory of prompts, output schemas, and a
handler function, all running in one Python process pool. Agents never call each
other — they emit typed events to a Postgres outbox, a router maps event types to
jobs, and workers pull jobs off a `SELECT ... FOR UPDATE SKIP LOCKED` queue. Every
LLM call goes through one function, `core/llm.py::complete_json()`, and its output
is always validated against a JSON Schema. Human approval gates for sends, pricing,
and deletes are enforced in code, not prompts.

## Documentation map

| Doc | Covers |
|---|---|
| [`docs/revenue-engine-build-spec.md`](docs/revenue-engine-build-spec.md) | Phases, milestones, repo layout, integrations |
| [`docs/entity-model.md`](docs/entity-model.md) | Domain entities, provenance, multi-currency |
| [`docs/event-catalog.md`](docs/event-catalog.md) | Every event, payload, emitter and consumer |
| [`docs/agent-contracts.md`](docs/agent-contracts.md) | Per-agent triggers, LLM tasks, autonomy, gates |
| [`docs/phase1-llm-boundary.md`](docs/phase1-llm-boundary.md) | Prompt/schema pairs and cross-field validations |
| [`docs/discovery-addendum.md`](docs/discovery-addendum.md) | Prospect discovery design |
| [`docs/competitive-deltas.md`](docs/competitive-deltas.md) | Competitive research → concrete build deltas |
| [`docs/decisions.md`](docs/decisions.md) | Append-only decision log |
| [`docs/verification-loop.md`](docs/verification-loop.md) | The verification checklist and report format every milestone follows |
| [`docs/architecture.md`](docs/architecture.md) | Diagrams + data flow *(populated as agents land)* |
| [`docs/deliverability.md`](docs/deliverability.md) | Email warmup, caps, domain strategy *(populated in M1.4)* |
| [`docs/runbook.md`](docs/runbook.md) | Ops: deploy, rotate keys, recover from failures *(populated pre-launch)* |

## Running locally

Requires [`uv`](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env        # fill in ANTHROPIC_API_KEY, DEV_SANDBOX_EMAIL at minimum
make setup                  # uv sync
make dev                    # docker compose up — Postgres 16 + pgvector
make migrate                # apply migrations/*.sql to DATABASE_URL, tracked in schema_migrations
make worker                 # start queue workers
```

Other commands:

```bash
make test    # unit + contract + integration tests
make golden  # golden tests — calls a real LLM, costs money
make check   # ruff + mypy --strict (core/, db/)
```

**`make test` runs against `TEST_DATABASE_URL`, never `DATABASE_URL`.** They
must be two different databases on the same Postgres server (`.env.example`
sets `TEST_DATABASE_URL` to `..._test` by default) — the suite refuses to run
if they're unset or identical, since integration test fixtures are allowed to
do things to their database that would corrupt whatever `DATABASE_URL` points
at (see `docs/runbook.md`). `TEST_DATABASE_URL`'s database is created and
migrated automatically on first test run; no separate setup step needed.

## Status

Phase 0 skeleton, milestone M0.3 (`core/events.py`, `core/queue.py`,
`orchestrator/router.py`, `scripts/run_worker.py`). No agent code exists yet
— see build-spec §10 for the full milestone order.
