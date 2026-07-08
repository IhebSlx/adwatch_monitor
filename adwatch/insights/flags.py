"""PART 3c — weekly signals for the business-development team.

compute_flags() takes the dashboard's metric rows (latest week + previous
week context) and returns human-readable flags, most important first. Pure
function over already-stored data — no network, no DB."""
from __future__ import annotations

FLAG_ORDER = ["new_campaign", "first_seen", "biggest_mover", "most_active",
              "hiring_push", "went_quiet"]


def compute_flags(metrics: list[dict]) -> list[dict]:
    """`metrics` rows need: company, has_data, total_active_ads, delta_ads,
    new_ads, ads_by_category, prev_total (None if no previous week).
    Returns [{type, company, label, detail}] sorted by importance."""
    flags: list[dict] = []
    have = [m for m in metrics if m.get("has_data")]
    if not have:
        return flags

    for m in have:
        total = m.get("total_active_ads") or 0
        prev = m.get("prev_total")
        new = m.get("new_ads") or 0
        cats = m.get("ads_by_category") or {}
        hiring = cats.get("recruitment", 0)

        if new > 0:
            flags.append({"type": "new_campaign", "company": m["company"],
                          "label": "New campaign",
                          "detail": f"{new} ad{'s' if new != 1 else ''} launched in the last 7 days"})
        if prev is None and total > 0:
            flags.append({"type": "first_seen", "company": m["company"],
                          "label": "First activity",
                          "detail": f"first week tracked with {total} active ads"})
        if hiring >= 3 or (total > 0 and hiring / total >= 0.5 and hiring >= 2):
            flags.append({"type": "hiring_push", "company": m["company"],
                          "label": "Hiring push",
                          "detail": f"{hiring} of {total} active ads are recruitment"})
        if prev is not None and prev > 0 and total == 0:
            flags.append({"type": "went_quiet", "company": m["company"],
                          "label": "Went quiet",
                          "detail": f"had {prev} active ads last week, now none"})

    # "first activity" is only a signal for a NEWLY added company — when most of
    # the list is first-week (initial onboarding), it's noise; drop it then.
    first_seen = [f for f in flags if f["type"] == "first_seen"]
    if len(first_seen) > max(1, len(have) // 2):
        flags = [f for f in flags if f["type"] != "first_seen"]

    # single-winner flags
    movers = [m for m in have if (m.get("delta_ads") or 0) > 0]
    if movers:
        top = max(movers, key=lambda m: m["delta_ads"])
        if top["delta_ads"] >= 2:
            flags.append({"type": "biggest_mover", "company": top["company"],
                          "label": "Biggest mover",
                          "detail": f"+{top['delta_ads']} active ads vs last week"})

    active = [m for m in have if (m.get("total_active_ads") or 0) > 0]
    if active:
        top = max(active, key=lambda m: m["total_active_ads"])
        flags.append({"type": "most_active", "company": top["company"],
                      "label": "Most active",
                      "detail": f"{top['total_active_ads']} active ads this week"})

    flags.sort(key=lambda f: FLAG_ORDER.index(f["type"]))
    return flags
