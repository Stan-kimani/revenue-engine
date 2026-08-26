"""Calibration report for qualification/score_lead.md (phase1-llm-boundary.md
§6, M1.2 calibration note): median, quartiles, and band counts of
`lead_scores.llm_part`, grouped by `prompt_version` — so a human can check
whether the LLM sub-scores are drifting generous ("if your first run
produces a median above 0.6, the scorer is not discriminating") once real
leads exist.

This script only reads and reports. It never tunes the prompt or moves band
thresholds itself (phase1-llm-boundary.md §6: fix drift in the prompt, not by
moving thresholds) — that stays a human decision informed by this report.

Usage: uv run python scripts/score_distribution.py
Env: DATABASE_URL (required).
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import NamedTuple

import asyncpg
from dotenv import load_dotenv


class _Row(NamedTuple):
    prompt_version: int | None
    llm_part: float
    band: str


def _quartiles(values: list[float]) -> tuple[float, float, float]:
    """(Q1, median, Q3). statistics.quantiles needs at least 2 data points;
    a single value is reported as (v, v, v) rather than raising."""
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, _q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, statistics.median(values), q3


def _print_group(label: str, rows: list[_Row]) -> None:
    llm_parts = [r.llm_part for r in rows]
    q1, median, q3 = _quartiles(llm_parts)
    band_counts = Counter(r.band for r in rows)
    print(f"\n{label} — {len(rows)} score(s)")
    print(f"  llm_part: median={median:.3f}  Q1={q1:.3f}  Q3={q3:.3f}")
    if median > 60.0:  # llm_part is on the same 0-100 scale as `total`
        print(
            "  ! median llm_part is above 60 — phase1-llm-boundary.md §6: "
            "the scorer may not be discriminating. Fix by tightening "
            "qualification/score_lead.md's rule 6, not by moving band "
            "thresholds."
        )
    bands_ordered = ["sql", "mql", "warm", "cold"]
    counts_text = "  ".join(f"{b}={band_counts.get(b, 0)}" for b in bands_ordered)
    print(f"  bands: {counts_text}")


async def run() -> int:
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(database_url)
    try:
        records = await conn.fetch(
            "SELECT prompt_version, llm_part, band FROM lead_scores WHERE llm_part IS NOT NULL"
        )
    finally:
        await conn.close()

    if not records:
        print("No lead_scores rows yet — nothing to report.")
        return 0

    rows = [
        _Row(prompt_version=r["prompt_version"], llm_part=float(r["llm_part"]), band=r["band"])
        for r in records
    ]

    by_version: dict[int | None, list[_Row]] = defaultdict(list)
    for row in rows:
        by_version[row.prompt_version].append(row)

    print(f"qualification/score_lead.md calibration report — {len(rows)} total score(s)")
    _print_group("All prompt versions", rows)
    for version in sorted(by_version, key=lambda v: (v is None, v)):
        _print_group(f"prompt_version={version}", by_version[version])

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
