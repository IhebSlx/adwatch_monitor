"""PART 3c — the DIVERGENCE score: the gap between what a partner buys from us
(internal revenue) and how alive their marketing is (external ad data).

    Divergenz = M × G
      M  (0–100)  Marketing-Aktivität  — active ads, campaign recency, momentum,
                  platform breadth, hiring signal
      G  (0–1)    Umsatz-Lücke         — how far buying has fallen off

A partner who stopped buying but runs fresh campaigns is alive, investing in
lead generation — just not with us. That's a win-back call. The score exists to
separate those from healthy partners (high ads but no gap → low score) and
from truly dormant ones (gap but no marketing → zero) across the whole base.

Deliberately simple and explainable: every score decomposes into a German
one-liner (`reason`) a BD colleague can read — no black box, or the ranked
list won't be trusted. Companies never fetched get NO score ("unbewertet"):
unknown must never look like inactive.

Ranking: score desc, then best prior-year revenue desc — a lapsed €150k
partner outranks a lapsed €5k partner at equal divergence, because that's
where the recoverable money is.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Ad, CollectionRun

# G — the revenue-gap factor per buying state
_GAP = {
    "lapsed": 1.0,    # bought in prior years, nothing this year — the win-back case
    "steep": 0.8,     # this year < 30% of best prior year
    "never": 0.6,     # dormant relationship — never bought
    "mild": 0.4,      # this year < 70% of best prior year
    "healthy": 0.15,  # stable, growing, or a fresh new buyer
}

_STATE_LABEL_DE = {
    "lapsed": "Kein Umsatz akt. Jahr",
    "steep": "Umsatz stark rückläufig",
    "never": "Bisher kein Umsatz",
    "mild": "Umsatz rückläufig",
    "healthy": "Umsatz gesund",
}


def _eur(v: float | None) -> str:
    return "-" if v is None else f"€{v:,.0f}".replace(",", ".")


def revenue_gap(y0: float | None, prior: list[float | None],
                elapsed: float = 1.0) -> tuple[float, str, float]:
    """(G, buying_state, best_prior_revenue) from the imported Umsatz columns.
    NULL revenue counts as €0 — same rule as the Companies Explorer filters.

    `elapsed` is the fraction of the current year already passed: revenue_y0
    is a PARTIAL year, so it's annualized (y0/elapsed) before comparing against
    full prior years — otherwise every healthy partner looks "declining" in
    July. Floored at 0.15 so January doesn't explode small numbers."""
    y0 = y0 or 0
    best_prior = max((p or 0) for p in prior) if prior else 0
    annualized = y0 / max(elapsed, 0.15)
    if y0 <= 0 and best_prior <= 0:
        state = "never"
    elif y0 <= 0:
        state = "lapsed"
    elif annualized < 0.30 * best_prior:
        state = "steep"
    elif annualized < 0.70 * best_prior:
        state = "mild"
    else:
        state = "healthy"
    return _GAP[state], state, best_prior


def marketing_score(total_ads: int, newest_start: dt.date | None, new_ads: int,
                    meta_ads: int, google_ads: int, hiring: int,
                    today: dt.date | None = None) -> int:
    """M (0–100). Recency-weighted on purpose: 3 fresh campaigns beat one ad
    that has been running unchanged for two years."""
    if not total_ads:
        return 0
    today = today or dt.date.today()
    m = min(total_ads, 10) * 5                      # volume, capped at 50
    if newest_start:
        days = (today - newest_start).days
        if days <= 30:
            m += 25                                  # campaign started this month
        elif days <= 90:
            m += 15                                  # started this quarter
    if new_ads:
        m += 10                                      # momentum this week
    if meta_ads and google_ads:
        m += 10                                      # investing on both platforms
    if hiring:
        m += 5                                       # hiring = growth signal
    return min(m, 100)


def _label(state: str, m: int, newest_start: dt.date | None, new_ads: int,
           today: dt.date) -> str:
    """The quadrant name shown next to the score."""
    if state in ("lapsed", "steep") and m >= 25:
        return "Win-back"
    if state == "never" and m >= 25:
        return "Neupotenzial"
    if (newest_start and (today - newest_start).days <= 30 and new_ads
            and state in ("healthy", "mild")):
        return "Aufsteiger"
    return ""


def _reason_de(state: str, y0: float, best_prior: float, total_ads: int,
               newest_start: dt.date | None, new_ads: int,
               meta_ads: int, google_ads: int, hiring: int,
               today: dt.date) -> str:
    """The German one-liner that makes the score explainable."""
    if state == "lapsed":
        buying = f"Kein Umsatz akt. Jahr (früher bis {_eur(best_prior)})"
    elif state == "steep":
        buying = f"Umsatz stark rückläufig ({_eur(y0)} vs. früher {_eur(best_prior)})"
    elif state == "mild":
        buying = f"Umsatz rückläufig ({_eur(y0)} vs. früher {_eur(best_prior)})"
    elif state == "never":
        buying = "Bisher kein Umsatz"
    else:
        buying = f"Umsatz gesund ({_eur(y0)})"

    marketing = [f"{total_ads} aktive Anzeige{'n' if total_ads != 1 else ''}"]
    if newest_start:
        days = (today - newest_start).days
        if days <= 1:
            marketing.append("neueste gestern/heute gestartet")
        elif days < 14:
            marketing.append(f"neueste vor {days} Tagen")
        elif days <= 120:
            marketing.append(f"neueste vor {days // 7} Wochen")
    if new_ads:
        marketing.append(f"{new_ads} neue diese Woche")
    if meta_ads and google_ads:
        marketing.append("Meta + Google")
    if hiring:
        marketing.append("Personalsuche aktiv")
    return f"{buying} — {', '.join(marketing)}"


def _newest_active_ad_starts() -> dict[int, dt.date]:
    """company_id -> start date of its newest ACTIVE ad, across all runs."""
    with SessionLocal() as s:
        rows = s.execute(
            select(CollectionRun.company_id, func.max(Ad.start_date))
            .join(Ad, Ad.run_id == CollectionRun.id)
            .where(Ad.is_active, Ad.start_date.is_not(None))
            .group_by(CollectionRun.company_id)).all()
    return {cid: d for cid, d in rows if d}


def compute_divergence(company_ids: list[int] | None = None) -> dict:
    """The ranked divergence list. Only companies with fetched ad data get a
    score; the rest are counted as `unrated` ("unbewertet"), never scored 0.
    Returns {rows, rated, unrated} with rows sorted by score desc, then best
    prior revenue desc."""
    from ..services import latest_metrics
    metrics = latest_metrics(company_ids)
    newest = _newest_active_ad_starts()
    today = dt.date.today()

    rows, unrated = [], 0
    for m in metrics:
        if not m["has_data"]:
            unrated += 1
            continue
        # revenue_y0 is a PARTIAL-year figure frozen at Excel-import time — so
        # annualize against how much of the year had elapsed WHEN IT WAS
        # IMPORTED, not today. Using today's day-of-year silently inflates
        # win-back false positives as the import ages (a healthy partner's
        # frozen July revenue looks tiny by November). Falls back to today only
        # when no import date is recorded.
        imp = m.get("imported_at")
        ref = dt.date.fromisoformat(imp[:10]) if imp else today
        elapsed = ref.timetuple().tm_yday / 365
        g, state, best_prior = revenue_gap(
            m.get("revenue_y0"),
            [m.get(f"revenue_y{i}") for i in (1, 2, 3, 4)], elapsed)
        cats = m.get("ads_by_category") or {}
        hiring = cats.get("recruitment", 0)
        total = m.get("total_active_ads") or 0
        new_ads = m.get("new_ads") or 0
        meta_ads = m.get("meta_active_ads") or 0
        google_ads = m.get("google_active_ads") or 0
        start = newest.get(m["company_id"])
        ms = marketing_score(total, start, new_ads, meta_ads, google_ads, hiring, today)
        score = round(ms * g)
        rows.append({
            "company_id": m["company_id"],
            "company": m["company"],
            "divergence": score,
            "marketing_score": ms,
            "gap": g,
            "state": state,
            "state_label": _STATE_LABEL_DE.get(state, state),
            "label": _label(state, ms, start, new_ads, today),
            "reason": _reason_de(state, m.get("revenue_y0") or 0, best_prior, total,
                                 start, new_ads, meta_ads, google_ads, hiring, today),
            "best_prior_revenue": best_prior,
            "revenue_y0": m.get("revenue_y0") or 0,
            "total_active_ads": total,
            "newest_ad_start": start.isoformat() if start else None,
            "week_start": m.get("week_start"),
        })

    rows.sort(key=lambda r: (-r["divergence"], -(r["best_prior_revenue"] or 0)))
    return {"rows": rows, "rated": len(rows), "unrated": unrated}
