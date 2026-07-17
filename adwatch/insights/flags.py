"""PART 3c — weekly signals for the business-development team.

compute_flags() takes the dashboard's metric rows (latest week + previous
week context) and returns human-readable flags, most important first. Pure
function over already-stored data — no network, no DB.

Signal-to-noise rules (the dashboard must read in one glance, not scroll):
  • ONE flag per company — its most important one wins, so a busy advertiser
    never stacks three near-identical cards.
  • Mass onboarding is not news: when a batch of companies gets its first
    tracked week at once, all their "first activity" cards collapse into a
    single aggregate line.
  • Hard cap on the total (default 6) — most important first, rest dropped.
"""
from __future__ import annotations

# importance order — used both to pick a company's single flag and to sort
FLAG_ORDER = ["went_quiet", "hiring_push", "biggest_mover", "most_active",
              "new_campaign", "first_seen"]


def compute_flags(metrics: list[dict], cap: int = 6) -> list[dict]:
    """`metrics` rows need: company, has_data, total_active_ads, delta_ads,
    new_ads, ads_by_category, prev_total (None if no previous week).
    Returns AT MOST `cap` flags [{type, company, label, detail}], one per
    company, most important first."""
    flags: list[dict] = []
    have = [m for m in metrics if m.get("has_data")]
    if not have:
        return flags

    first_seen: list[dict] = []
    for m in have:
        total = m.get("total_active_ads") or 0
        prev = m.get("prev_total")
        new = m.get("new_ads") or 0
        cats = m.get("ads_by_category") or {}
        hiring = cats.get("recruitment", 0)

        if prev is not None and prev > 0 and total == 0:
            flags.append({"type": "went_quiet", "company": m["company"],
                          "label": "Went quiet",
                          "detail": f"had {prev} active ads last week, now none"})
        if hiring >= 3 or (total > 0 and hiring / total >= 0.5 and hiring >= 2):
            flags.append({"type": "hiring_push", "company": m["company"],
                          "label": "Hiring push",
                          "detail": f"{hiring} of {total} active ads are recruitment"})
        if new > 0:
            flags.append({"type": "new_campaign", "company": m["company"],
                          "label": "New campaign",
                          "detail": f"{new} ad{'s' if new != 1 else ''} launched in the last 7 days"})
        if prev is None and total > 0:
            first_seen.append({"type": "first_seen", "company": m["company"],
                               "label": "First activity",
                               "detail": f"first week tracked with {total} active ads"})

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

    # Mass onboarding: twenty "first week tracked" cards say nothing — one
    # aggregate line does. Individual cards only when it's a genuine handful.
    if len(first_seen) > 3:
        names = ", ".join(f["company"] for f in first_seen[:3])
        flags.append({"type": "first_seen",
                      "company": f"{len(first_seen)} companies",
                      "label": "First activity",
                      "detail": f"newly tracked this week — {names}, …"})
    else:
        flags.extend(first_seen)

    # ONE flag per company — its most important one wins — then cap the total
    rank = {t: i for i, t in enumerate(FLAG_ORDER)}
    flags.sort(key=lambda f: rank[f["type"]])
    seen: set[str] = set()
    out = []
    for f in flags:
        if f["company"] in seen:
            continue
        seen.add(f["company"])
        out.append(f)
    return out[:cap]
