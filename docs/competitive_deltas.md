# Competitive Findings → Build Deltas

**Source:** analysis of axeautomation.co, idealink.tech, hatchworks.com, 10clouds.com (2026-07-26).
**Scope:** only changes with a concrete implementation. Positioning observations that
don't produce code are in the conversation, not here.

**Competitive set (updated 2026-07-26 after second review):**

| Firm | Relationship | Weight |
|---|---|---|
| **Zack Shields** | Direct peer. Solo operator, personal brand, $2.5–15K builds, 2–6 weeks. Closest analogue to our model. | **Primary benchmark** |
| Axe Automation | Direct competitor one tier up. SMB/mid-market, Make/Monday/n8n. | High |
| Automatable | Content-led business (130K+ YouTube). Much of its social proof is from students building their own automation businesses, not clients. Lead-gen mechanics assume an audience we don't have. | Low — do not benchmark |
| Idea Link | Adjacent-aspirational. 20+ in-house, €20–40K projects. | Medium |
| HatchWorks / 10Clouds | Enterprise. SOC 2, analyst awards, 200-person delivery. Playbook does not transfer. | Do not imitate |

**Positioning consequence:** Zack's differentiation is operator credibility — years spent
running the businesses he now automates. Competing against that on technical claims alone
is the weakest available ground. Our equivalent asset is direct experience inside the
service-business / remote-ops world (incl. what RemoteKind surfaces about how these firms
actually run). Positioning should lead with that, not with build capability.

---

## D1 — Add `budget_band` to lead capture and scoring

**Evidence:** Idea Link qualifies budget in the contact form itself with four bands plus
a "not sure yet" option. Self-reported budget is the strongest available qualification
signal and currently has zero weight in our scorer.

**Entity change** — `leads`:
```
budget_band     enum nullable   -- 'unknown','under_5k','5k_15k','15k_40k','40k_plus'
budget_source   enum nullable   -- 'self_reported','inferred','discovery_call'
```

**Scoring change** — `config/industries/b2b-service-firms.yaml`:
```yaml
scoring:
  weights:
    icp_match: 0.30      # was 0.35
    intent: 0.22         # was 0.25
    engagement: 0.18     # was 0.20
    size_fit: 0.15       # was 0.20
    budget_fit: 0.15     # new
  budget_fit_map:
    "40k_plus": 1.0
    "15k_40k": 0.85
    "5k_15k": 0.5
    "under_5k": 0.1
    "unknown": 0.4       # neutral-ish; absence is not disqualifying
```
Deterministic component. `unknown` must not be penalised heavily — most outbound leads
will never self-report, and punishing that would bias the scorer toward inbound only.

**Note:** re-calibrate bands after this change. Adding a fifth weighted component
shifts the whole distribution; `sql: 78` is no longer the same threshold.

---

## D2 — Add `handoff` to the objection vocabulary

**Evidence:** all four competitors independently solve for "what happens when you
leave" — Idea Link with an ownership/IP-transfer guarantee, HatchWorks with training and
workshops, Axe by placing a trained engineer inside the client. Three different answers
to one fear means it is the dominant unspoken objection in this category.

Our vocabulary (`price, timing, trust, need, authority, competitor, other`) buries this
under `trust`, which makes it invisible to the Learning Agent's objection-frequency
analysis — the single metric most likely to tell us what to fix in positioning.

```yaml
objections:
  categories:
    handoff: "Who maintains this after you leave. Ownership, IP, lock-in, bus factor."
```

Requires: update `reply_classification.json` enum, `objection_response.json` enum, and
the `objection_vocabulary` variable. Add a golden fixture:
`reply_handoff_objection.txt` → "what happens if you two get hit by a bus" →
`objection_category: handoff`.

---

## D3 — Distinguish published price floor from negotiated price

**Evidence:** Idea Link publishes ranges openly (projects start €20–40K, growing to
€500K+) and still gates actual quotes behind a call. Our current rule — the model may
never state any price — conflates two different things.

Publishing a floor is **qualification**. Quoting a project is **negotiation**. The first
filters no-budget leads early and costs nothing; the second must stay human.

```yaml
commercial_boundaries:
  published_price_floor:
    enabled: true
    statement: "Engagements start at <AMOUNT>."   # human-authored, verbatim only
  never_state_autonomously:
    - any price beyond the verbatim published_price_floor statement
    - any range, quote, discount, or percentage
    - contract terms, payment terms, guarantees
```

**Implementation guard:** the model may emit the floor statement *only* as an exact
string match against config. Anything else still trips V4 (`pricing_present`) and
routes to approval. Do not let the model paraphrase a price.

---

## D4 — Structured proof records, retrievable by industry and workflow

**Evidence:** case studies across all four are titled by metric, not client — Axe leads
with capacity and speed multiples; 10Clouds with a 79% faster credit-assessment figure
and a tender-screening throughput number; HatchWorks with onboarding-speed and
email-automation percentages. The metric is the headline.

Our `account_brief.proof_to_reference` accepts `{claim, source_ref}` but nothing
structures proof for retrieval. Add a knowledge-base entry shape:

```yaml
# knowledge/global/proof/<slug>.md frontmatter
metric_name: "onboarding time"
before: "14 days"
after: "5 days"
delta_headline: "2.4x faster onboarding"
industry: agency
workflow: client_onboarding
client_type: "30-person marketing agency"
verified: true          # ONLY verified:true is retrievable for outreach
permission_to_name: false
```

`memory.semantic_search` filters on `verified: true` for any retrieval feeding
`research_account` or drafting. Unverified proof must be physically excluded from the
retrievable set — not merely flagged — or it will eventually be asserted as fact.

**Reality check:** you have zero proof records today. This schema exists so the first
client outcome is captured in the right shape, not so it can be populated now.

---

## D5 — Response-time SLA as a tracked metric

**Evidence:** Idea Link and 10Clouds both commit publicly to a one-business-day
response. It is a cheap trust signal in a category where slow, vague vendors are the norm.

Our system can beat this trivially, which makes it a positioning asset — but only if
it is measured rather than assumed.

```yaml
sla:
  inbound_first_response_hours: 4
  alert_on_breach: true
```
Track `first_response_latency` on inbound leads (`lead.routed_to_human` → first
outbound message). Emit `sla.breached` to the ops notifier. This is also a metric worth
putting in front of clients later: we respond in X, industry norm is Y.

---

## D6 — Assessment tool as a lead-capture source

**Evidence:** HatchWorks runs an Agentic Opportunity Finder as a free tool; Idea Link
runs a cost estimator on a dedicated subdomain. Both are self-qualifying: completing one
signals intent, and the output demonstrates capability.

For an AI automation business this does triple duty — lead magnet, capability demo, and
enrichment source. A completed assessment tells you their workflows, tools, team size,
and stated pains in their own words.

**Architecture:** it is a separate static/serverless app, NOT part of this repo. It
posts to the existing lead-capture webhook:

```
POST /webhooks/lead
  source: "assessment"
  payload: { answers: {...}, generated_report_id, email, budget_band }
→ lead.captured
→ (R2 inbound rule) scored, then routed to human regardless of band
```

**The high-value part:** assessment answers become `personalization_anchors` directly —
self-reported, verifiable, and specific. This solves the anchor-scarcity problem that
makes cold outreach thin, for every inbound lead. Add `source: webform:assessment` to
the anchor provenance vocabulary.

Build order: after Phase 1 is sending. Not before.

---

## Deliberately not adopted

| Pattern | Why not |
|---|---|
| Podcast / newsletter / course stack | 12-month payoff, requires an audience we don't have |
| SOC 2 / compliance certification | Enterprise procurement requirement; our buyer doesn't ask |
| Published research reports ("State of AI") | Requires proprietary data we don't have |
| Analyst awards and badge collection | Follows revenue, doesn't precede it |
| Placed-engineer delivery model (Axe Phase 3) | Requires recruiting capacity; revisit if RemoteKind and this venture merge funnels |

**Worth doing outside this repo, near-term:** platform partner programs (n8n, Make,
Monday all run them) and a Clutch profile with reviews from the first two clients.
Both are credibility that can be earned in weeks rather than years, and every competitor
analysed leads with exactly these signals.


---

## D7 — Adopt a four-dimension opportunity score

**Evidence:** Zack Shields publishes a qualification framework scoring work on frequency,
friction, error exposure, and revenue sensitivity, with the best candidates at the
intersection of all four. It is the most directly adaptable artifact found in this review.

Our scorer answers "is this company a fit." It does not answer **"do they have a workflow
worth automating"** — which is the actual buying trigger and the thing a discovery call
must establish.

**Where it goes (three places, one framework):**

1. **Discovery checklist** — add a scored opportunity assessment alongside the existing
   pass/fail disqualifiers:
```yaml
qualification:
  opportunity_score:
    dimensions:
      frequency:          "How often does this run? daily > weekly > monthly"
      friction:           "How painful/manual is it today?"
      error_exposure:     "What does a mistake cost?"
      revenue_sensitivity: "Does it touch money, claims, compliance, or lead response?"
    scale: 1-5
    threshold_to_scope: 14   # of 20; below this, decline or defer
    require_all_above: 2     # a zero on any dimension disqualifies the workflow
```
2. **Assessment tool (D6)** — these four dimensions become the questionnaire's spine.
3. **`buying_intent` sub-score** — when a prospect describes a workflow (inbound form,
   reply, or call notes), score it on these dimensions rather than judging intent from
   general enthusiasm.

**Why `require_all_above` matters:** a high-frequency, high-friction workflow with zero
revenue sensitivity and zero error cost is a time-saver nobody will pay to fix. The
intersection is the point, not the total.

---

## D8 — Require a free-text problem statement at capture

**Evidence:** Zack's intake form makes "what process are you trying to fix?" a required
field, alongside optional team-size and pain-point dropdowns. Automatable requires
monthly revenue.

This is the cheapest anchor source available. A prospect describing their own broken
workflow in their own words produces a `personalization_anchor` that is specific,
checkable, and self-reported — no enrichment required.

**Capture payload additions:**
```
problem_statement   text NOT NULL      -- required free text
pain_category       enum nullable      -- manual_data_entry | slow_followup |
                                       -- reporting_visibility | intake | reconciliation | other
team_size_band      enum nullable      -- solo | 2_10 | 11_50 | 50_plus
budget_band         enum nullable      -- see D1
```

`problem_statement` feeds `build_prospect_profile` as a distinct variable and is written
as an anchor with `source: webform:self_reported, confidence: 1.0`. Self-reported facts
are the only ones that warrant full confidence.

This closes the anchor-scarcity gap for every inbound lead.

---

## D9 — Publish the disqualifiers

**Evidence:** Zack runs a "who is this not for" FAQ naming three anti-profiles — wanting a
strategy deck with no implementation, wanting a generic chatbot demo, wanting help
managing many disconnected tools.

We already have this list (industry pack `qualification.discovery_checklist` and the
"avoid" segments). It is currently internal only. Published, it does two jobs: it filters
before the call, and it signals judgment — which is itself the product in this category.

**Action:** derive the public "not a fit" copy directly from the discovery checklist so
the two never drift. One source, two audiences.

No code change. Website copy, when the site exists.

---

## D10 — Publish a price range, not just a floor

**Evidence:** Zack publishes a three-tier structure — free review, project build at a
stated typical range, hourly consulting — with the range visible before any contact.
Idea Link publishes bands in the contact form. Axe publishes nothing.

D3 proposed a published *floor*. Zack shows the stronger version: publish the **typical
range** for the core offer. It qualifies harder and earlier, and removes the most common
reason a good-fit prospect doesn't book (not knowing if they can afford it).

```yaml
service_catalogue:
  core:
    published_range: "<LOW> to <HIGH>"      # human-authored, verbatim only
```
Same guard as D3: the model may emit this string only as an exact config match. Any
paraphrase, any other figure, still trips V4 and routes to approval.

---

## D11 — Deferred: programmatic SEO

Zack runs industry × service × location landing pages (hospitality, healthcare, real
estate; voice agents, workflow automation; Orlando). This is a real long-term inbound
channel and clearly deliberate.

**Deferred, not rejected.** It is a website project requiring content depth we don't have,
and it pays off over quarters. Revisit after the first two case studies exist — at which
point industry pages have something real to say.