"""Modelled ad-spend as a LOW–HIGH interval. Meta does not publish real spend for
ordinary commercial ads, so this is an educated estimate — see spend_assumptions.yaml.
Two methods, chosen per ad depending on whether reach data is present."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import load_spend_assumptions


@dataclass
class SpendEstimate:
    low: float
    high: float
    method: str          # reach | count | mixed
    model_version: str


def estimate_spend(ads: list) -> SpendEstimate:
    """`ads` is a list of objects/dicts with `.reach` (int|None)."""
    a = load_spend_assumptions()
    version = a.get("model_version", "unknown")
    window = a.get("window_days", 7)
    cpm_lo, cpm_hi = a["cpm_low_eur"], a["cpm_high_eur"]
    day_lo, day_hi = a["daily_cost_per_ad_low_eur"], a["daily_cost_per_ad_high_eur"]

    low = high = 0.0
    methods: set[str] = set()

    for ad in ads:
        reach = getattr(ad, "reach", None) if not isinstance(ad, dict) else ad.get("reach")
        if reach:  # Method A: reach-based
            low += reach * cpm_lo / 1000.0
            high += reach * cpm_hi / 1000.0
            methods.add("reach")
        else:       # Method B: count-based fallback
            low += day_lo * window
            high += day_hi * window
            methods.add("count")

    if not methods:
        method = "none"
    elif len(methods) == 1:
        method = methods.pop()
    else:
        method = "mixed"

    return SpendEstimate(round(low, 2), round(high, 2), method, version)
