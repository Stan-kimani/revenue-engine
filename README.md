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
| [`docs/architecture.md`](docs/architecture.md) | Diagrams + data flow *(populated as agents land)* |
| [`docs/deliverability.md`](docs/deliverability.md) | Email warmup, caps, domain strategy *(populated in M1.4)* |
| [`docs/runbook.md`](docs/runbook.md) | Ops: deploy, rotate keys, recover from failures *(populated pre-launch)* |

## Running locally

Requires [`uv`](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env        # fill in ANTHROPIC_API_KEY, DEV_SANDBOX_EMAIL at minimum
make setup                  # uv sync
make dev                    # docker compose up — Postgres 16 + pgvector
make migrate                # apply migrations/*.sql, tracked in schema_migrations
make worker                 # start queue workers
```

Other commands:

```bash
make test    # unit + contract + integration tests
make golden  # golden tests — calls a real LLM, costs money
make check   # ruff + mypy --strict (core/, db/)
```

## Status

Phase 0 skeleton, milestone M0.2 (`migrations/0001_init.sql`, `db/models.py`,
`db/repositories.py` for companies/contacts/leads/events/jobs). No agent or
orchestrator code exists yet — see build-spec §10 for the full milestone order.
