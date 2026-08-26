"""Deterministic evaluator for config/industries/*.yaml's
icp.disqualifiers[].rule strings (agent-contracts.md §2, entity-model.md §1).

A small, closed grammar only — deliberately not a general expression
language:

    <clause> ::= FIELD == "VALUE"  |  FIELD in [V1, V2, ...]
    <rule>   ::= <clause> (AND <clause>)*

FIELD must be one of `_KNOWN_FIELDS` below — a real, structured signal the
deterministic scorer can actually read (a company column, or an
attribute-envelope value the LLM already wrote with its own confidence gate
at leadgen time). `OR` and any other operator (most notably `matches`,
used in the pack against unstructured/LLM-inferred free text like
`positioning` or `industry`) are deliberately unsupported: there is no
defined semantics for fuzzy text matching anywhere in the docs, and
inventing one here would bake in a guess for exactly the kind of rule
(`regulated_health`) that exists because of a real compliance gap (no PHI
posture) — a wrong guess there is worse than an honest "cannot evaluate."

`core/config.py::load_config()` calls `parse_rule()` on every disqualifier
NOT marked `enforcement: manual` and fails the boot (`ConfigError`) if it
can't parse (M1.2 Correction 1, docs/decisions.md): an unenforceable
disqualifier must never sit silently in the pack looking like protection
that isn't there. A pack author marks a rule `enforcement: manual` instead
of writing it in a form this module can parse, and
`qualification.discovery_checklist` is expected to cover it on the human
call instead — `agents/qualification.py` never evaluates a manual-enforcement
rule at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import DisqualifierRuleError

# Real, structured fields the deterministic scorer can read at qualification
# time: employee_band is a company column; business_model/revenue_signal are
# attribute-envelope values leadgen already wrote (and confidence-gated) at
# enrichment time. Adding a field here means agents/qualification.py must
# also know how to supply it to evaluate_rule() — see its `_KNOWN_FIELDS`
# assertion in tests/unit/test_qualification_scoring.py.
KNOWN_FIELDS = frozenset({"employee_band", "business_model", "revenue_signal"})

_EQ_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*==\s*"([^"]*)"$')
_IN_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s+in\s+\[(.*)\]$")


@dataclass(frozen=True)
class RuleClause:
    field: str
    values: tuple[str, ...]
    """`==` becomes a one-item tuple; `in` carries the full list. Either way,
    a clause is true when the field's actual value is a member of `values`."""


@dataclass(frozen=True)
class ParsedDisqualifierRule:
    clauses: tuple[RuleClause, ...]
    """AND-combined — a rule is true only when every clause is true."""


def parse_rule(rule: str) -> ParsedDisqualifierRule:
    """Raises DisqualifierRuleError if `rule` is not exactly `AND`-joined
    `FIELD == "VALUE"` / `FIELD in [...]` clauses over `KNOWN_FIELDS`."""
    raw_clauses = re.split(r"\s+AND\s+", rule.strip())
    clauses: list[RuleClause] = []
    for raw in raw_clauses:
        raw = raw.strip()
        eq_match = _EQ_RE.match(raw)
        in_match = _IN_RE.match(raw)
        if eq_match:
            field, value = eq_match.groups()
            clauses.append(RuleClause(field=field, values=(value,)))
        elif in_match:
            field, list_body = in_match.groups()
            values = tuple(_strip_value(v) for v in list_body.split(",") if v.strip())
            clauses.append(RuleClause(field=field, values=values))
        else:
            raise DisqualifierRuleError(
                rule, f"unrecognised clause (unimplemented operator or form): {raw!r}"
            )

    for clause in clauses:
        if clause.field not in KNOWN_FIELDS:
            raise DisqualifierRuleError(
                rule,
                f"unknown field {clause.field!r} — scorer can only read {sorted(KNOWN_FIELDS)}",
            )

    return ParsedDisqualifierRule(clauses=tuple(clauses))


def _strip_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def evaluate_rule(
    parsed: ParsedDisqualifierRule,
    *,
    employee_band: str | None,
    business_model: str | None,
    revenue_signal: str | None,
) -> bool:
    """AND across all clauses. A field with no known value — not yet
    enriched, or the LLM's confidence for it fell below
    `min_confidence_to_store` and it was never written — makes its clause
    false, never true: absence of evidence must never trigger a
    disqualifier."""
    actual_by_field = {
        "employee_band": employee_band,
        "business_model": business_model,
        "revenue_signal": revenue_signal,
    }
    return all(
        actual_by_field[clause.field] is not None and actual_by_field[clause.field] in clause.values
        for clause in parsed.clauses
    )
