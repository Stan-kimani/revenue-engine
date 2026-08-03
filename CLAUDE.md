# CLAUDE.md — Operating Rules for This Repository

You are implementing the AI Revenue Engine. This file is the behavioural contract for
working in this repo. The architectural source of truth is the design set in `docs/`:
- **Never rename or relocate a file you were given.** Use the exact path and
  filename specified. Documents, configs, prompts, and schemas use hyphens;
  only Python modules use underscores. If a path seems wrong, ask — do not
  normalise it.
  
| Document | Covers |
|---|---|
| `docs/revenue-engine-build-spec.md` | Phases, milestones, repo layout, integrations |
| `docs/entity-model.md` | Domain entities, provenance, multi-currency |
| `docs/event-catalog.md` | Every event, payload, emitter and consumer |
| `docs/agent-contracts.md` | Per-agent triggers, LLM tasks, autonomy, gates |
| `docs/phase1-llm-boundary.md` | Prompt/schema pairs and cross-field validations |
| `docs/discovery-addendum.md` | Prospect discovery design |

All are binding. Where any two conflict, stop and ask — do not pick one silently.
Later documents supersede earlier ones; the build spec is the oldest.

---

## 1. Non-negotiable architecture rules

Violating any of these is a bug, even if the code works.

1. **No agent frameworks.** No LangChain, CrewAI, AutoGen, LlamaIndex, or similar.
   Direct API calls only. If you believe a framework is needed, stop and ask.
2. **All LLM calls go through `core/llm.py::complete_json()`.** Never call the
   Anthropic/OpenAI SDK directly from an agent, route, or script.
3. **No prompt text in Python files.** Prompts live in `prompts/**.md` only.
   No f-string prompt assembly, no inline system messages, no exceptions.
4. **All application SQL lives in `src/revenue_engine/db/repositories.py`.**
   No inline SQL in agents, routes, or orchestrator code. Exempt: migration
   files (`migrations/*.sql`) and the migration runner (`scripts/migrate.py`),
   which are schema tooling, not data access. Nothing else is exempt.
5. **Agents never import other agents.** Agents communicate only by emitting
   events. `agents/` may import `core/`, `db/`, and `integrations/` interfaces.
6. **Every LLM output is validated against a JSON Schema in `schemas/outputs/`.**
   Never parse freeform model text. Never use regex on a model response.
7. **Every event payload is validated against a schema in `schemas/events/`**
   before it is emitted.
8. **Human-in-the-loop gates are enforced in code, not prompts.** Any send,
   proposal, pricing, discount, or delete path checks for a valid approval token
   at the function level before executing.
9. **No industry, vertical, ICP, or brand voice is hardcoded.** These come from
   `config/industries/*.yaml` via `core/config.py`. Core code stays vertical-agnostic.
10. **No MCP servers in runtime code or `pyproject.toml`.** MCP is dev-time only.
11. **This file overrides any installed skill.** Where a skill's guidance conflicts
    with these rules — especially 3, 4, 6 and 7 — these rules win. Structural
    separation is not over-engineering, and "fewer lines" never justifies inlining
    SQL, prompts, or schema validation.
12. **Money is `numeric`, never float.** No arithmetic across currencies without
    converting through `base_currency`. A closed deal's `fx_rate_to_base` is
    immutable — enforce it in `close_deal()`, not by convention.

---

## 2. Workflow rules

- **Work one milestone at a time.** Milestones are defined in build-spec §10.
  Implement only the milestone you were asked for. Stop at its end and report.
  Do not start the next milestone unprompted.
- **Plan before implementing.** For any milestone, state the files you will
  create or modify and wait for confirmation before writing code.
- **Log judgment calls** in `docs/decisions.md`, append-only, dated, format:
  `## YYYY-MM-DD — <decision>` / `Context:` / `Decision:` / `Consequence:`.
- **Ask instead of assuming.** If the spec is ambiguous, ask. Do not invent
  behaviour and note it later.
- **Never weaken a test to make it pass.** If a test is wrong, say so and explain.
- **Do not add dependencies without asking.** Every new package needs a reason.

---

## 3. Where things go

| Kind of thing | Location |
|---|---|
| Prompt text | `prompts/<agent>/<task>.md` |
| LLM output contract | `schemas/outputs/<task>.json` |
| Event contract | `schemas/events/<event.name>.json` |
| SQL | `db/repositories.py` |
| Row types | `db/models.py` |
| Agent handler | `agents/<agent>.py` |
| Event → handler mapping | `orchestrator/router.py` (the only routing table) |
| External API client | `integrations/<service>.py` (no business logic) |
| Tunable numbers, weights, caps | `config/*.yaml` — never literals in code |
| Migration | `migrations/NNNN_<name>.sql` (plain SQL, forward-only) |

---

## 4. Code standards

- Python 3.12+, `uv` for dependency management.
- Type hints everywhere. `mypy --strict` passes on `core/` and `db/`.
- `ruff` clean. Format with `ruff format`.
- Async by default for I/O (`httpx`, `asyncpg`). No blocking calls in handlers.
- Dataclasses or Pydantic models for row types. **No ORM.**
- Errors: raise typed exceptions from `core/errors.py`. Never swallow exceptions
  silently. Failed jobs must dead-letter with the error recorded, never disappear.
- Idempotency: every handler must be safe to run twice on the same event.
  Use natural keys and upserts, not blind inserts.
- Minimum abstraction. Do not add a base class, factory, or interface until there
  are two real implementations. Delete code rather than commenting it out.
- `agents/sales.py` is deliberately one file covering outbound and conversation.
  If it exceeds ~400 lines, split by lifecycle stage (outbound vs conversation),
  never by task. Do not split it pre-emptively.

---

## 5. Testing expectations

- Every scoring, classification, or state-transition rule gets a unit test.
- Every prompt gets a golden test (fixture in → assert schema + key fields).
- Every gate gets a negative test: cap exceeded blocks, missing approval blocks,
  unsubscribe suppresses, duplicate import dedupes.
- Integration tests run against a real test Postgres with mocked integrations.
- Golden tests cost money — mark them `@pytest.mark.golden`, excluded by default.

---

## 6. Safety rails while developing

- When `ENV != production`, `integrations/gmail.py` routes ALL outbound mail to
  `DEV_SANDBOX_EMAIL`. This check lives in the send function itself, not config.
- Never commit secrets. Never write real credentials into `.env.example`.
- Never run destructive SQL against a database you did not create in this session.
- Seed data only via `scripts/seed_dev.py`.

---

## 7. Commands

```bash
make dev        # docker compose up (postgres + pgvector)
make migrate    # apply migrations in order
make worker     # start queue workers
make test       # unit + contract + integration
make golden     # golden tests (costs money)
make check      # ruff + mypy
```
