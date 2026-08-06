"""Objekte: Verkaufschancen grouped into the projects they belong to.

The Besonderheit im Objektvertrieb, from Iheb: several Verkaufschancen share one
`sl_primary_opportunityid` — the primary VC IS the Objekt (its name is the
building address), the members are the per-firm attempts to win it. **A project
is WON when any member wins.** The CRM itself encodes this: sibling VCs get
closed as "Zugehörige VC gewonnen" — 1,355 of them since 2023 — and counting
those as losses (as the first loss analysis did) overstates failure.

So the honest unit of analysis for Objektvertrieb is the PROJECT, and this
module is the read model for it: group, decide the project outcome, collect the
roles (executing firm, architect, end customer) across all members. This is the
data structure a future Ideal-Project-Profile will train on; for now it powers
the Objekte tab so the team sees projects instead of loose Verkaufschancen.

Market note recorded here because it changes interpretation, not code: in SPAIN
architects hold real decision power (can effectively award the Auftrag — treat
as Kunden); in GERMANY they consult but rarely decide. Any per-market weighting
of the architect role must respect that asymmetry.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Company, CrmOpportunity

log = logging.getLogger("adwatch.projekte")

# Project outcome, in precedence order: one win makes the project won, no
# matter how many sibling attempts died on the way.
WON, OPEN, LOST = "gewonnen", "offen", "verloren"

# Member closures that MEAN "the project was won by someone else's VC" — they
# must never count as project losses.
_WON_ELSEWHERE = "Zugehörige VC gewonnen"


def _project_rows() -> dict[str, list[CrmOpportunity]]:
    with SessionLocal() as s:
        rows = list(s.scalars(select(CrmOpportunity)))
    groups: dict[str, list[CrmOpportunity]] = defaultdict(list)
    for o in rows:
        key = o.project_id or o.opportunity_guid or o.crm_id
        groups[key].append(o)
    return groups


def _outcome(members: list[CrmOpportunity]) -> str:
    if any(m.state == "gewonnen" for m in members):
        return WON
    if any((m.lost_reason or "") == _WON_ELSEWHERE for m in members):
        # no won member in OUR window, but the CRM says a related VC won —
        # the winning member may predate the loaded window. Still a won project.
        return WON
    if any(m.state == "offen" for m in members):
        return OPEN
    return LOST


def overview() -> dict:
    """The corrected Objektvertrieb picture: projects, not Verkaufschancen."""
    groups = _project_rows()
    counts = {WON: 0, OPEN: 0, LOST: 0}
    multi = 0
    won_value = 0.0
    for members in groups.values():
        out = _outcome(members)
        counts[out] += 1
        if len(members) > 1:
            multi += 1
        if out == WON:
            won_value += sum(float(m.order_value or 0) for m in members)
    total = sum(counts.values())
    decided = counts[WON] + counts[LOST]
    return {
        "projects": total,
        "multi_vc_projects": multi,
        **counts,
        "won_value": round(won_value, 2),
        # the number the VC-level analysis got wrong: win rate at PROJECT level
        "project_win_rate": round(counts[WON] / decided, 4) if decided else None,
    }


def list_projects(status: str | None = None, min_members: int = 1,
                  limit: int = 200) -> list[dict]:
    """Projects, most valuable first, with their members and roles resolved."""
    groups = _project_rows()
    with SessionLocal() as s:
        by_crm = {(c.crm_id or "").lower(): c for c in
                  s.scalars(select(Company).where(Company.crm_id.is_not(None)))}

    def name_of(gid):
        c = by_crm.get((gid or "").lower())
        return c.name if c else None

    out = []
    for key, members in groups.items():
        if len(members) < min_members:
            continue
        outcome = _outcome(members)
        if status and outcome != status:
            continue
        primary = next((m for m in members if (m.opportunity_guid or "") == key),
                       members[0])
        value = sum(float(m.order_value or 0) for m in members) or None
        est = sum(float(m.estimated_value or 0) for m in members) or None
        firms = sorted({n for n in (name_of(m.parent_account_crm_id)
                                    for m in members) if n})
        archs = sorted({n for n in (name_of(m.architect_crm_id)
                                    for m in members) if n})
        out.append({
            "project_id": key,
            "name": primary.project_name or primary.name or "(ohne Namen)",
            "status": outcome,
            "members": len(members),
            "won_members": sum(1 for m in members if m.state == "gewonnen"),
            "order_value": value, "estimated_value": est,
            "channel": primary.sales_channel,
            "created": min((m.created_on for m in members if m.created_on),
                           default=None),
            "firms": firms[:6], "architects": archs[:4],
            "lost_reasons": sorted({m.lost_reason for m in members
                                    if m.lost_reason
                                    and m.lost_reason != _WON_ELSEWHERE}),
        })
    out.sort(key=lambda p: -(p["order_value"] or p["estimated_value"] or 0))
    for p in out:
        if p["created"]:
            p["created"] = p["created"].date().isoformat()
    return out[:limit]
