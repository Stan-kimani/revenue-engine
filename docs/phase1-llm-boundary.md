# Phase 1 — Prompts & Output Schemas

**Status:** architecture phase, deliverable 5 (Phase 1 scope only).
Copy `schemas/outputs/*.json` and `prompts/**` into the repo at those exact paths.

10 prompts, 9 schemas (`outreach_draft.json` is shared by initial outreach and follow-up).
Phases 2–4 (meeting_intel, crm, learning, market_intel, marketing) are deliberately
not written yet — their contracts may shift once Phase 1 has run against real data.

---

## 1. Two reconciliations against earlier docs

**Model tier replaces model name in frontmatter.** Build-spec §6 showed
`model: claude-sonnet-4-6`. These files use `tier: fast|standard|deep`, resolved
through `config/base.yaml`. Changing models is then a one-line config change instead
of editing ten prompt files. Log in `docs/decisions.md`.

**`qualification/classify_intent.md` does not exist.** Build-spec §2 listed it; agent
contract §2 folded intent into `score_lead` as a sub-score. One call instead of two,
and the intent judgment can see fit context. Log this too.

---

## 2. The design principle behind every schema

**Strict validation causes hallucination unless "I don't know" is expressible.**

If `industry` is a required string, the model invents one rather than fail validation.
So every inferred field is `{value, confidence, evidence}` with `value: null` explicitly
permitted, and each prompt states that null is the correct answer when evidence is
absent. Code then drops anything below `enrichment.min_confidence` rather than storing
a guess with provenance that makes it look trustworthy.

The model supplies `value`, `confidence`, `evidence`. Code adds `source`, `run_id`,
`observed_at` when writing the attribute envelope (entity model §2). The model must
never generate a `run_id` or timestamp.

---

## 3. The anchor contract (the anti-fabrication spine)

`build_prospect_profile` emits `personalization_anchors`, each with an `anchor_id`,
a checkable `fact`, and a `source`. Downstream:

- `research_account.supporting_anchors` → must reference existing anchor_ids
- `draft_initial_outreach.facts_asserted[].anchor_id` → must reference existing anchor_ids
- `draft_followup` → same

This makes "the email claimed something we never knew" a **validation failure**, not a
thing you discover from an embarrassed prospect. An empty anchor array is explicitly
allowed and honest — it routes the account to manual research instead of producing a
confidently wrong email.

---

## 4. Cross-field validations code MUST perform

JSON Schema cannot express these. Implement in `core/llm.py` as post-validation hooks
or in the calling agent, and fail the same way a schema failure fails.

| # | Check | Applies to | On failure |
|---|---|---|---|
| V1 | every `facts_asserted[].anchor_id` exists in the source profile | outreach_draft | reject draft, retry once, then dead-letter |
| V2 | code-computed real word count of `body` is within the prompt's stated limit (`word_count` is no longer model-supplied — `complete_json` computes and injects it; see docs/decisions.md 2026-08-14) | outreach_draft | reject |
| V3 | every `supporting_anchors[]` exists in profile | account_brief | reject |
| V4 | `pricing_present` is true if body text contains any currency/number pattern | proposal_draft | force `requires_approval`, log honesty violation |
| V5 | `precedents_used[]` ⊆ ids actually retrieved and passed in | objection_response | strip unknown ids, log |
| V6 | any sub-score > 0.2 has non-empty `evidence` | lead_subscores | reject |
| V7 | `objection_category` non-null only when `intent == "objection"` | reply_classification | reject |
| V8 | `requested_resume_date` non-null only when `intent == "not_now"` | reply_classification | null it, log |
| V9 | no email address regex present anywhere in output text fields | contact_enrichment | reject |

V4 matters most: the approval gate for proposals keys off `pricing_present`, so the
model self-reporting it honestly is load-bearing. Verify it in code rather than trusting it.

---

## 5. Golden test fixtures required (`tests/fixtures/`)

Minimum set before M1.x is considered done:

- `company_rich.json` / `company_sparse.json` — sparse must produce nulls and
  `insufficient_context: true`, not invented values
- `contact_founder.json` / `contact_ambiguous_title.json`
- `profile_no_anchors.json` — must produce an outreach draft with empty `facts_asserted`
- `reply_price_objection.txt` → `intent: objection`, `objection_category: price`
- `reply_angry.txt` → `escalation_signals` contains `anger`
- `reply_ooo.txt` → `out_of_office`, not `not_now`
- `reply_polite_unsubscribe.txt` → `unsubscribe` ("please take me off your list, thanks!")
- `reply_cryptic.txt` ("k") → `unclear` with low confidence
- `reply_referral.txt` → `referral`, no invented email address

The sparse-input and no-anchor fixtures are the important ones. They test the failure
behaviour that determines whether this system embarrasses you in front of a prospect.

---

## 6. Calibration warning

`score_lead` will drift generous. Models default to agreeableness, and "this seems
like a decent fit" becomes 0.7. If your first run produces a median above 0.6, the
scorer is not discriminating and the SQL band is meaningless.

Fix by tightening the prompt's rule 6 and re-running the same fixtures — not by moving
the band thresholds. Moving thresholds hides the problem; the scores still cannot rank
leads against each other.

Track median and distribution of `llm_part` per prompt version from the first day.
