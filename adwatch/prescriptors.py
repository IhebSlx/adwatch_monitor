"""Score companies on the projects they INFLUENCE, not the revenue they pay us.

The problem this solves, in one measurement: all 808 architect accounts converted
at 0% in the backtest. Scoring them on "did this company buy?" is structurally
guaranteed to say no, because architects specify and dealers order. Their 0% then
made the ICP's headline lift (148x) look like signal when it was really just
"architects aren't dealers" — true, already known, and useless as a ranking.

An architect's value to Solarlux is the project volume they specify. That fact
lives on the OPPORTUNITY (`slx_executingarchitect_accountid`), never on their
account — which is why no amount of account enrichment could ever have found it.

So a company gets TWO different outcome measures, and which one applies depends on
its role:

    buyer role       -> revenue_y0..y4        (already in Company)
    prescriptor role -> influenced projects   (computed here)

Both can be non-zero: a Verarbeiter who also gets specified into projects has both.
Nothing here overwrites the revenue columns.

Why this population may rank where the dealer population cannot: the dealer base
rate is 87%, so there is almost nothing to discriminate against. Most architects
influence ZERO Solarlux projects and a few influence many — real variance in the
outcome, which is exactly what a profile needs.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from sqlalchemy import select

from .db import SessionLocal
from .models import Company, CrmOpportunity

log = logging.getLogger("adwatch.prescriptors")

# Roles a company can play on a project. Kept explicit because the same company
# can appear in more than one, and the CRM sometimes puts the architect and the
# end customer on the same account.
ROLES = ("architect", "orderer", "end_customer")

WON = "gewonnen"


def _stats() -> dict[str, dict]:
    """crm_id -> influence stats, over every role it plays on any project."""
    out: dict[str, dict] = defaultdict(lambda: {
        "projects": 0, "won": 0, "lost": 0, "open": 0,
        "value_won": 0.0, "value_total": 0.0,
        "roles": set(), "building_types": set(),
        "first": None, "last": None,
    })

    with SessionLocal() as s:
        for o in s.scalars(select(CrmOpportunity)):
            # Collect the roles per company FIRST. The CRM sometimes lists the
            # same account as both architect and end customer, and then the
            # project must count ONCE while keeping BOTH roles — counting twice
            # would inflate that company's influence, dropping the second role
            # would hide what it actually does.
            roles_here: dict[str, set[str]] = defaultdict(set)
            for gid, role in ((o.architect_crm_id, "architect"),
                              (o.parent_account_crm_id, "orderer"),
                              (o.end_customer_crm_id, "end_customer")):
                gid = (gid or "").strip().lower()
                if gid:
                    roles_here[gid].add(role)

            for gid, roles in roles_here.items():
                st = out[gid]
                st["projects"] += 1
                st["roles"].update(roles)
                value = float(o.order_value or 0)
                st["value_total"] += value
                if o.state == WON:
                    st["won"] += 1
                    st["value_won"] += value
                elif o.state == "verloren":
                    st["lost"] += 1
                else:
                    st["open"] += 1
                if o.building_type:
                    st["building_types"].add(o.building_type)
                for key, when in (("first", o.created_on), ("last", o.created_on)):
                    if when is None:
                        continue
                    cur = st[key]
                    if cur is None or (when < cur if key == "first" else when > cur):
                        st[key] = when
    return out


def influence_for(crm_id: str | None) -> dict:
    """Influence stats for one company (empty shape when it has none), so callers
    never have to special-case a company with no projects."""
    if not crm_id:
        return _empty()
    return _shape(_stats().get(crm_id.strip().lower()))


def _empty() -> dict:
    return {"projects": 0, "won": 0, "lost": 0, "open": 0, "win_rate": None,
            "value_won": 0.0, "value_total": 0.0, "roles": [],
            "building_types": [], "first": None, "last": None}


def _shape(st) -> dict:
    if not st:
        return _empty()
    decided = st["won"] + st["lost"]
    return {
        "projects": st["projects"], "won": st["won"], "lost": st["lost"],
        "open": st["open"],
        # None, not 0, when nothing is decided yet — an untested architect is not
        # a losing one, and a 0 here would rank them below a real 10% performer
        "win_rate": round(st["won"] / decided, 3) if decided else None,
        "value_won": round(st["value_won"], 2),
        "value_total": round(st["value_total"], 2),
        "roles": sorted(st["roles"]), "building_types": sorted(st["building_types"]),
        "first": st["first"].date().isoformat() if st["first"] else None,
        "last": st["last"].date().isoformat() if st["last"] else None,
    }


def overview() -> dict:
    """How much of the prescriptor picture we actually hold — so nobody builds a
    profile on 12 projects without knowing it."""
    stats = _stats()
    with SessionLocal() as s:
        total_opps = s.scalar(select(CrmOpportunity).with_only_columns(
            CrmOpportunity.id).limit(1))
        n_opps = len(list(s.scalars(select(CrmOpportunity.id))))
        by_seg: dict[str, dict] = defaultdict(lambda: {"companies": 0, "with_projects": 0})
        for c in s.scalars(select(Company).where(Company.crm_id.is_not(None))):
            seg = c.segment or "(ohne)"
            by_seg[seg]["companies"] += 1
            if (c.crm_id or "").lower() in stats:
                by_seg[seg]["with_projects"] += 1
    return {
        "opportunities": n_opps,
        "companies_with_projects": len(stats),
        "by_segment": {k: v for k, v in sorted(
            by_seg.items(), key=lambda kv: -kv[1]["with_projects"])},
        "usable": n_opps > 0,
    }


def prescriptor_targets(min_projects: int = 1) -> list[dict]:
    """Companies ranked by influenced project value — the prescriptor equivalent
    of the buyer call list. Architects with many specified projects but little or
    no direct revenue are precisely the relationships worth investing in, and they
    are invisible to a revenue-only ranking."""
    stats = _stats()
    rows = []
    with SessionLocal() as s:
        names = {(c.crm_id or "").lower(): c for c in
                 s.scalars(select(Company).where(Company.crm_id.is_not(None)))}
    for gid, st in stats.items():
        if st["projects"] < min_projects:
            continue
        c = names.get(gid)
        if c is None:
            continue
        shaped = _shape(st)
        rows.append({
            "company_id": c.id, "name": c.name, "segment": c.segment,
            "country": c.country, "revenue_y0": c.revenue_y0,
            **shaped,
        })
    rows.sort(key=lambda r: (-(r["value_won"] or 0), -r["projects"]))
    return rows
