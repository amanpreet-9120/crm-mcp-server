"""Deterministic business rules.

Everything here is plain Python on purpose: pipeline math, staleness rules and
priority scoring must give the same answer every time and be unit-testable.
The model's job is to *reason about* these results and write for humans,
not to compute them.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import date

STAGES = ["lead", "qualified", "proposal", "negotiation", "won", "lost"]
OPEN_STAGES = ["lead", "qualified", "proposal", "negotiation"]

# Probability used for weighted forecast.
STAGE_PROBABILITY = {"lead": 0.10, "qualified": 0.25, "proposal": 0.50, "negotiation": 0.75, "won": 1.0, "lost": 0.0}

# A deal is "stale" after this many days without activity. Later stages get shorter limits.
STALE_AFTER_DAYS = {"lead": 21, "qualified": 14, "proposal": 10, "negotiation": 7}

HIGH_VALUE = 25_000


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def days_between(earlier: str | date, later: date) -> int:
    if isinstance(earlier, str):
        earlier = date.fromisoformat(earlier)
    return (later - earlier).days


def weighted(value: int, stage: str, probability: float | None = None) -> int:
    """Value x win probability. A backend may supply the probability (HubSpot: from the deal's
    pipeline stage); otherwise the playbook default for the stage is used."""
    return round(value * (STAGE_PROBABILITY[stage] if probability is None else probability))


@dataclass
class Attention:
    reasons: list[str]
    priority: str  # "high" | "medium" | "low"
    score: int


def assess_deal(deal: dict, today: date) -> Attention:
    """Return why an open deal needs attention (empty reasons = healthy)."""
    reasons: list[str] = []
    score = 0
    stage = deal["stage"]
    if stage not in OPEN_STAGES:
        return Attention([], "low", 0)

    idle = days_between(deal["last_activity_at"], today)
    limit = STALE_AFTER_DAYS[stage]
    if idle > limit:
        reasons.append(f"No activity for {plural(idle, 'day')} (limit for '{stage}' is {limit})")
        score += 2 + (idle - limit) // 7

    overdue_close = days_between(deal["close_date"], today)
    if overdue_close > 0:
        reasons.append(f"Expected close date passed {plural(overdue_close, 'day')} ago - update the date or the stage")
        score += 3

    if not deal.get("next_step"):
        reasons.append("No next step scheduled")
        score += 2
    elif deal.get("next_step_date"):
        late = days_between(deal["next_step_date"], today)
        if late > 0:
            reasons.append(f"Next step '{deal['next_step']}' is {plural(late, 'day')} overdue")
            score += 2

    if reasons and deal["value"] >= HIGH_VALUE:
        score += 3

    if not reasons:
        priority = "low"
    elif score >= 7 or (deal["value"] >= HIGH_VALUE and len(reasons) >= 2):
        priority = "high"
    else:
        priority = "medium"
    return Attention(reasons, priority, score)


def pipeline_summary(deals: list[dict], probabilities: dict[str, float] | None = None) -> dict:
    by_stage = {s: {"count": 0, "value": 0, "weighted_value": 0} for s in STAGES}
    for d in deals:
        row = by_stage[d["stage"]]
        row["count"] += 1
        row["value"] += d["value"]
        row["weighted_value"] += weighted(d["value"], d["stage"], d.get("probability"))
    open_rows = [by_stage[s] for s in OPEN_STAGES]
    won, lost = by_stage["won"]["count"], by_stage["lost"]["count"]
    return {
        "by_stage": by_stage,
        "open_deals": sum(r["count"] for r in open_rows),
        "open_pipeline_value": sum(r["value"] for r in open_rows),
        "weighted_forecast": sum(r["weighted_value"] for r in open_rows),
        "won_value": by_stage["won"]["value"],
        "win_rate": round(won / (won + lost), 2) if (won + lost) else None,
        "stage_probabilities": probabilities or STAGE_PROBABILITY,
    }


def suggest(name: str, candidates: list[str], n: int = 3) -> list[str]:
    """Close-match suggestions so a typo gets a useful error, not a dead end."""
    lowered = {c.lower(): c for c in candidates}
    hits = difflib.get_close_matches(name.lower(), list(lowered), n=n, cutoff=0.4)
    contains = [c for c in candidates if name.lower() in c.lower()]
    out: list[str] = []
    for c in contains + [lowered[h] for h in hits]:
        if c not in out:
            out.append(c)
    return out[:n]
