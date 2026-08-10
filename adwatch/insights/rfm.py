"""Customer health from real purchase behaviour — and the win-back list.

This module exists because of one measurement: **Solarlux loses to competitors in
only 6.2% of lost opportunities.** The biggest single loss reason is "Kein Feedback
vom Kunden" at 23.5% (EUR 170.6M of pipeline), and "Kein Interesse mehr" adds
another 8.8%. A third of everything lost is lost to SILENCE, not to a rival.

That reframes what this app is for. Finding brand-new companies was never the
scarce thing — CRM already holds 46,000 accounts and 14,801 Interessenten. The
scarce thing is noticing, in time, that an existing relationship has gone quiet
while the customer is still visibly spending money in the market. Nothing else in
the Solarlux stack can see that, because it needs the ad signal from outside CRM.

So there are two rankings here, and they are different questions:

    health / overdue  -> "who has gone quiet against THEIR OWN rhythm?"
    winback_score     -> "...and of those, who is still visibly in-market?"

Three data facts drive every threshold below. Each was measured, not assumed:

1. A Beleg is not an order. 73,112 Belege = 54,534 order events; big dealers issue
   several documents per order, so a raw-Beleg cadence reads as 0-3 days. Cadence
   is therefore computed on CrmOrderEvent (company + day).

2. Roughly a quarter of Belege are 0 EUR and the median is EUR 194 — spare parts,
   samples, warranty. Meanwhile 1.6% of Belege carry 32% of revenue. So "did they
   buy" needs a materiality floor, or a EUR 0 gasket order counts as a customer.

3. A fixed "no order in 12 months = lapsed" rule is wrong here. Cadences range
   from days (a Bauelementehandel ordering constantly) to a year (a Wohnungs-
   wirtschaft doing one refurbishment). Overdue is measured RELATIVE to the
   company's own median interval.
"""
from __future__ import annotations

import datetime as dt
import logging
import statistics as st
from collections import defaultdict

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Company, CrmOrderEvent
from .. import scope

log = logging.getLogger("adwatch.rfm")

# A purchase counts as a real system sale from here up. Measured: EUR 2,000 keeps
# 97.4% of revenue while dropping two thirds of the documents.
MATERIAL_EUR = 2_000.0

# Cadence is only meaningful with enough events to have a median interval at all.
MIN_EVENTS_FOR_CADENCE = 4

# Overdue = this many times the company's own median gap...
OVERDUE_RATIO = 3.0
# ...but never flag inside this many days, or a fortnightly orderer alarms monthly.
OVERDUE_MIN_DAYS = 120

# Fallback for companies without a measurable cadence (1-3 events).
FALLBACK_LAPSED_DAYS = 365

HEALTH = ("aktiv", "beobachten", "gefährdet", "verloren", "einmalig", "nie")


def _median_interval(days: list[dt.date]) -> float | None:
    if len(days) < MIN_EVENTS_FOR_CADENCE:
        return None
    gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
    med = st.median(gaps)
    # Same-day events are already collapsed, but a company that ordered on four
    # consecutive days still has a median of 1 and would alarm after 120 days.
    # That is intended: 120 days of silence from a daily orderer IS the signal.
    return med if med >= 1 else 1.0


def classify(events: list[tuple[dt.date, float]], today: dt.date | None = None) -> dict:
    """Health for one company from its ordered [(date, amount)] events.

    Returns the health label, days since the last order, the measured cadence and
    an `overdue_factor` — how many of its own intervals have elapsed. The factor
    is what makes two very different companies comparable.
    """
    today = today or dt.date.today()
    if not events:
        return {"health": "nie", "days_since": None, "cadence_days": None,
                "overdue_factor": None, "events": 0, "material_events": 0,
                "value": 0.0}

    events = sorted(events)
    days = [d for d, _ in events]
    value = sum(a for _, a in events)
    material = [d for d, a in events if a >= MATERIAL_EUR]
    since = (today - days[-1]).days
    cadence = _median_interval(days)
    factor = (since / cadence) if cadence else None

    if not material:
        # Bought, but never anything material — spare parts only. Not a system
        # customer, and calling it one would poison every ICP trained on buyers.
        health = "einmalig"
    elif cadence is not None:
        if factor >= OVERDUE_RATIO and since >= OVERDUE_MIN_DAYS:
            health = "verloren" if factor >= OVERDUE_RATIO * 2 else "gefährdet"
        elif factor >= 1.5 and since >= 60:
            health = "beobachten"
        else:
            health = "aktiv"
    else:
        # 1-3 events: no rhythm to compare against, fall back to absolute age.
        if len(material) == 1 and since > FALLBACK_LAPSED_DAYS:
            health = "einmalig"
        elif since > FALLBACK_LAPSED_DAYS * 2:
            health = "verloren"
        elif since > FALLBACK_LAPSED_DAYS:
            health = "gefährdet"
        else:
            health = "aktiv"

    return {"health": health, "days_since": since, "cadence_days": cadence,
            "overdue_factor": round(factor, 2) if factor else None,
            "events": len(events), "material_events": len(material),
            "value": round(value, 2)}


def winback_score(cls: dict, value: float, *, advertising: bool = False) -> float:
    """0-100 priority for re-engaging a quiet customer.

    Deliberately NOT a probability — there is no labelled win-back outcome in the
    data yet, so calling it one would be dishonest. It is a ranking built from
    three things a human would weigh anyway, and it is monotonic in each:

        how much they were worth  x  how clearly they have gone quiet  x  are
        they still visibly spending in the market

    The ad signal is a multiplier rather than an additive bonus because a lapsed
    customer who is demonstrably still buying advertising is a categorically
    different prospect from one who may simply have stopped trading.
    """
    if cls["health"] in ("aktiv", "nie"):
        return 0.0
    # Value: log-scaled. Revenue spans EUR 0 to EUR 30M+ and 50% of it sits with
    # 66 companies, so a linear term would make the list nothing but whales.
    import math
    v = math.log10(max(value, 1.0)) / 7.0          # ~0..1 over 1 .. 10M EUR
    f = min((cls["overdue_factor"] or 1.0) / 6.0, 1.0)   # 0..1, saturating at 6x
    base = 100.0 * (0.65 * min(v, 1.0) + 0.35 * f)
    if advertising:
        base *= 1.5
    return round(min(base, 100.0), 1)


def _events_by_company() -> dict[int, list[tuple[dt.date, float]]]:
    out: dict[int, list[tuple[dt.date, float]]] = defaultdict(list)
    with SessionLocal() as s:
        for cid, d, amt in s.execute(
                select(CrmOrderEvent.company_id, CrmOrderEvent.order_date,
                       CrmOrderEvent.amount)):
            out[cid].append((d, float(amt or 0)))
    for v in out.values():
        v.sort()
    return out


def recompute(today: dt.date | None = None) -> dict:
    """Store health + winback_score on every Company. Idempotent."""
    today = today or dt.date.today()
    events = _events_by_company()
    counts: dict[str, int] = defaultdict(int)

    with SessionLocal() as s:
        # Which companies are currently advertising — the signal only AdWatch has.
        advertising = {
            cid for (cid,) in s.execute(
                select(Company.id).where(Company.page_id.is_not(None),
                                         Company.monitored.is_(True)))
        }
        for c in s.scalars(select(Company)):
            cls = classify(events.get(c.id, []), today)
            c.health = cls["health"]
            # `health` is a FACT about the row and stays even for companies out of
            # scope — a Private Endkunde really did buy, and scope.py keeps that
            # history on purpose. `winback_score` is a POSITION IN A CALL LIST and
            # must not exist for someone we will never call. This loop had no
            # scope check at all, so 1.449 consumers carried a win-back score;
            # overdue_customers() filtered them out of the view, which is exactly
            # what kept it invisible. Anything reading the column directly — a
            # report, an export, a future query — got them.
            # `is_intercompany` is checked separately because scope.py covers
            # consumers and competitors only — deliberately, since an own-group
            # company still belongs in the Firmen tab. It just must never be on a
            # list of people to win back. overdue_customers() already excluded
            # them from the VIEW; the stored column did not.
            if scope.is_in_scope(c.segment, c.is_competitor) and not c.is_intercompany:
                c.winback_score = winback_score(
                    cls, cls["value"], advertising=c.id in advertising)
            else:
                c.winback_score = None
            counts[cls["health"]] += 1
        s.commit()
    log.info("rfm.recompute: %s", dict(counts))
    return dict(counts)


def overdue_customers(limit: int = 200, min_value: float = 0.0,
                      today: dt.date | None = None,
                      with_total: bool = False):
    """Companies quiet against their own rhythm, worst first.

    `with_total=True` returns {"rows", "total"} instead of a bare list, so the
    caller can report how many MATCHED, not just how many it sent.

    This is the list that did not exist before: 670 companies carrying EUR 35.5M
    of historic revenue have gone at least 3 of their own intervals silent, and
    the largest of them ordered every few days for years.
    """
    today = today or dt.date.today()
    events = _events_by_company()
    rows = []
    with SessionLocal() as s:
        comps = {c.id: c for c in s.scalars(scope.apply(select(Company)))}
    for cid, evs in events.items():
        c = comps.get(cid)
        if c is None or c.is_intercompany:
            continue
        cls = classify(evs, today)
        if cls["health"] not in ("gefährdet", "verloren", "beobachten"):
            continue
        if cls["value"] < min_value:
            continue
        rows.append({
            "company_id": cid, "name": c.name, "segment": c.segment,
            "sub_segment": c.sub_segment, "country": c.country,
            "city": c.city, "kv": c.kv,
            "advertising": bool(c.page_id),
            "winback_score": c.winback_score,
            **cls,
        })
    rows.sort(key=lambda r: (-(r["winback_score"] or 0), -r["value"]))
    return ({"rows": rows[:limit], "total": len(rows)}
            if with_total else rows[:limit])


def summary(today: dt.date | None = None) -> dict:
    """Counts and euro exposure per health state, for the dashboard."""
    today = today or dt.date.today()
    events = _events_by_company()
    out: dict[str, dict] = {h: {"companies": 0, "value": 0.0} for h in HEALTH}
    with SessionLocal() as s:
        comps = {c.id: c for c in s.scalars(scope.apply(select(Company)))}
    for cid, c in comps.items():
        if c.is_intercompany:
            continue
        cls = classify(events.get(cid, []), today)
        b = out[cls["health"]]
        b["companies"] += 1
        b["value"] += cls["value"]
    for b in out.values():
        b["value"] = round(b["value"], 2)
    return out
