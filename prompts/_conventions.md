# Prompt File Conventions

Referenced by `docs/revenue-engine-build-spec.md` §6 and CLAUDE.md's file-placement
table across three milestones before this file existed. Written at M0.4, alongside
`core/llm.py::complete_json()` — the code that parses, renders and validates
everything described here. If the two ever disagree, the code is a bug; fix the code
to match this file, or fix this file and log why in `docs/decisions.md`, not the
other way around silently.

Derived from the 10 prompt files that exist today (`prompts/leadgen/`,
`prompts/qualification/`, `prompts/sales/`) plus the rules `complete_json()`
enforces at call time.

---

## 1. Shape

Every prompt file is Markdown with a YAML frontmatter block, delimited by `---`
lines, followed by the prompt body:

```markdown
---
id: sales/draft_initial_outreach
tier: standard                  # fast | standard | deep — resolved via config/base.yaml
output_schema: outputs/outreach_draft.json
max_tokens: 1200
variables: [account_brief, prospect_profile, sender_persona, voice_rules, constraints]
version: 1
---
# Role
You write first-touch emails for {{sender_persona}}...

# Context
{{prospect_profile}}
...

# Rules
- Under 120 words. One specific observation about their business. One clear CTA.
- Never invent facts not present in the context above.

# Output
Respond with JSON matching the output schema. No prose outside JSON.
```

## 2. Frontmatter keys (all required)

| Key | Type | Notes |
|---|---|---|
| `id` | string | Must equal the file's own path under `prompts/`, minus the `.md` extension — e.g. `prompts/sales/draft_initial_outreach.md` has `id: sales/draft_initial_outreach`. `complete_json()` checks this and raises if they disagree; a prompt file that was copied and renamed without updating its `id` fails loudly instead of silently misreporting itself in `agent_runs`. |
| `tier` | `fast \| standard \| deep` | **Never a model name.** `core/config.py` resolves a tier to a real model ID via `config/base.yaml`'s `models:` block. Changing models is a one-line config edit, not an edit to every prompt file that used it. |
| `output_schema` | string | Path relative to `schemas/`, e.g. `outputs/outreach_draft.json`. Must reference a file that exists — checked by `tests/contracts/`. Must also equal whatever `output_schema` the caller passes to `complete_json()`; a mismatch raises rather than silently trusting one over the other. |
| `max_tokens` | integer | Passed straight to the model call. |
| `variables` | list of strings | Every name is expected to appear as a `{{name}}` somewhere in the body (not mechanically enforced — see §4 on the render step, which checks the reverse direction). |
| `version` | integer | Bumped on any material change to the prompt's wording or instructions — not on typo fixes. `agent_runs.prompt_version` logs whichever version produced a given output, so prompt performance is comparable across versions over time. |

## 3. Rules

- **Never hardcode a model name in a prompt file.** `tier` is the only model-related
  field a prompt file may contain.
- **Industry voice and vocabulary come from the config pack via variables** —
  `{{sender_persona}}`, `{{voice_rules}}`, `{{scoring_guidance}}`, `{{objection_vocabulary}}`,
  `{{commercial_boundaries}}`, `{{service_catalogue}}`, `{{icp_definition}}`,
  `{{industry_pack_vocabulary}}`, `{{constraints}}`. A prompt body never names a
  vertical, a company type, or a tone directly — that would hardcode the one industry
  pack that happened to exist when it was written (CLAUDE.md §1 non-negotiable 9).
- **`additionalProperties: false` on every output schema.** Enums over free text
  wherever a downstream branch depends on the value. This is enforced in
  `schemas/outputs/*.json`, not here, but a prompt's `# Output` section should always
  say "Respond with JSON matching the output schema. No prose outside JSON." so the
  model isn't fighting its own instructions against what will actually be validated.
- **Every prompt has at least one golden test** (`tests/golden/`, `tests/fixtures/`).
- **Version bumps are not optional.** `agent_runs` is the only record of which
  wording produced which output; an unbumped version on a real change makes that
  record wrong, not just incomplete.

## 4. Variables: two kinds

Every `{{variable}}` a prompt body references falls into one of two buckets —
enumerated explicitly, per-variable, in `tests/contracts/test_prompts_valid.py`'s
`RUNTIME_VARIABLES` constant, not left to guesswork:

- **Config-sourced**: resolvable today from `config/base.yaml` plus the loaded
  industry pack (`sender_persona`, `voice_rules`, `icp_definition`,
  `objection_vocabulary`, `commercial_boundaries`, `service_catalogue`,
  `scoring_guidance`, `constraints`, `industry_pack_vocabulary`).
- **Runtime-supplied**: per-call data an agent assembles from the database at the
  moment it calls `complete_json()` — a prospect profile, a reply's text, a thread's
  history, an account's enrichment. No agent code exists yet (M0.4 builds only
  `complete_json()` and its machinery); these variables are documented as expected
  runtime inputs, not backed by anything today.

`complete_json()` itself does not care which bucket a variable came from — it only
enforces the render-time contract: **every `{{variable}}` actually present in the
body text must have a corresponding key in the `variables` dict the caller passes
in, or the call fails before any API request is made.** An unsupplied variable is
never rendered as an empty string.

## 5. Prompt/schema/tier cross-reference

`tests/contracts/test_prompts_valid.py` checks, for every file under `prompts/`:

- its `output_schema` names a file that exists under `schemas/outputs/`,
- its `tier` is one of `fast`, `standard`, `deep`,
- its `id` matches its own path, and
- every `{{variable}}` in its body is either config-resolvable or in the
  `RUNTIME_VARIABLES` allowlist (§4).

These are collection-time / CI-time checks — the same class of defect `complete_json()`
would otherwise only catch the first time a worker actually renders the file.
