"""The FBI file: everything the system knows about ONE company, assembled.

Iheb's requirement, verbatim in spirit: "when you click on it in our app, you
see everything that is linked to it, whatever it was" — CRM master data, the
Beleg history, every Verkaufschance in every ROLE the company plays on it,
the projects those group into, quotes vs invoiced, enrichment, identity
evidence, marketing behaviour, the colleague's research notes — and a short
profile synthesised from all of it.

Design decisions that matter:

* ROLES ARE EXPLICIT. The same company can appear on a Verkaufschance as the
  executing firm, the architect, or the end customer — and which role it plays
  changes what the row MEANS (an architect's 'lost' VC is not a lost sale).
  The dossier therefore never mixes roles into one list.

* THE KURZPROFIL IS DETERMINISTIC, not an LLM call. It renders on every drawer
  open; at 48,000 companies an LLM sentence per view would be slow, costly and
  unauditable. Every clause traces to a column. (The LLM-written description
  from enrichment is shown alongside — that one was paid for once and stored.)

* Numbers that need context carry it: win rates are per decided VCs, "quiet
  for N days" always ships with the company's OWN cadence.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from sqlalchemy import select

from .db import SessionLocal
from .models import Company, CrmOpportunity, CrmOrderEvent

log = logging.getLogger("adwatch.dossier")

_ROLE_COLS = (("kaeufer", "parent_account_crm_id"),
              ("architekt", "architect_crm_id"),
              ("endkunde", "end_customer_crm_id"))


def _beleg_block(s, cid: int) -> dict:
    events = list(s.execute(
        select(CrmOrderEvent.order_date, CrmOrderEvent.amount,
               CrmOrderEvent.beleg_count, CrmOrderEvent.channel)
        .where(CrmOrderEvent.company_id == cid)
        .order_by(CrmOrderEvent.order_date.desc())))
    if not events:
        return {"events": 0}
    by_year: dict[str, float] = defaultdict(float)
    for d, amt, _, _ in events:
        by_year[str(d.year)] += float(amt or 0)
    return {
        "events": len(events),
        "total": round(sum(float(a or 0) for _, a, _, _ in events), 2),
        "first": events[-1][0].isoformat(), "last": events[0][0].isoformat(),
        "by_year": {y: round(v, 2) for y, v in sorted(by_year.items())},
        "recent": [{"date": d.isoformat(), "amount": float(a or 0),
                    "belege": n, "channel": ch}
                   for d, a, n, ch in events[:12]],
    }


def _vc_row(o: CrmOpportunity) -> dict:
    return {"number": o.number, "name": o.project_name or o.name,
            "state": o.state, "lost_reason": o.lost_reason,
            "type_of_use": o.type_of_use, "origin": o.origin,
            "vc_type": o.vc_type, "dealer_status": o.dealer_status,
            "created": o.created_on.date().isoformat() if o.created_on else None,
            "order_value": o.order_value, "estimated_value": o.estimated_value,
            "invoiced_value": o.invoiced_value, "quoted_value": o.quoted_value,
            "sap_orders": o.sap_order_numbers,
            # The BUILDING SITE, not the customer's registered address. Imported
            # separately because no Excel export ever carried it.
            "city": o.city, "postal_code": o.postal_code, "street": o.street,
            "project_id": o.project_id}


def _role_block(rows: list[CrmOpportunity]) -> dict:
    won = [o for o in rows if o.state == "gewonnen"]
    lost = [o for o in rows if o.state == "verloren"]
    decided = len(won) + len(lost)
    reasons: dict[str, int] = defaultdict(int)
    for o in lost:
        if o.lost_reason and o.lost_reason != "Zugehörige VC gewonnen":
            reasons[o.lost_reason] += 1
    uses: dict[str, int] = defaultdict(int)
    for o in rows:
        if o.type_of_use and o.type_of_use != "Sonstige/nicht bekannt":
            uses[o.type_of_use] += 1
    origins: dict[str, int] = defaultdict(int)
    for o in rows:
        if o.origin:
            origins[o.origin] += 1
    recent = sorted(rows, key=lambda o: (o.created_on or dt.datetime.min),
                    reverse=True)[:10]
    return {
        "vcs": len(rows), "won": len(won), "lost": len(lost),
        "open": len(rows) - decided,
        "win_rate": round(len(won) / decided, 3) if decided else None,
        "won_value": round(sum(float(o.order_value or 0) for o in won), 2),
        "invoiced_value": round(sum(float(o.invoiced_value or 0) for o in rows), 2),
        "quoted_value": round(sum(float(o.quoted_value or 0) for o in rows), 2),
        "lost_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5]),
        "building_types": dict(sorted(uses.items(), key=lambda kv: -kv[1])[:5]),
        "origins": dict(sorted(origins.items(), key=lambda kv: -kv[1])[:5]),
        # The ten newest, for the drawer's summary. `vcs` above is the true
        # count and the UI has to show both — one company carries 1.266
        # Verkaufschancen, and a list of ten that does not say so reads as the
        # whole history. verkaufschancen() below serves the rest, paged.
        "recent": [_vc_row(o) for o in recent],
    }


def verkaufschancen(company_id: int, role: str | None = None,
                    limit: int = 100, offset: int = 0) -> dict:
    """Every Verkaufschance this company appears on, newest first, paged.

    `_role_block.recent` caps at ten because it feeds a summary. This is the
    full history behind it: "everything that has to do with this firm" is not a
    sample, and the distribution is skewed enough that the difference matters —
    58% of companies have exactly one Verkaufschance and 159 have fifty or more.

    Roles are kept apart for the same reason the dossier keeps them apart: the
    same company can be Käufer, Architekt and Endkunde (1.850 firms are all
    three), and a lost Verkaufschance means something different in each. `role`
    filters to one; without it every role is returned and each row says which.
    """
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        gid = (c.crm_id or "").strip().lower() if c else ""
        if not gid:
            return {"rows": [], "total": 0, "by_role": {}, "returned": 0}

        by_role: dict[str, int] = {}
        seen: dict[str, dict] = {}          # opportunity guid -> row
        for r, col in _ROLE_COLS:
            if role and r != role:
                continue
            rows = list(s.scalars(select(CrmOpportunity).where(
                getattr(CrmOpportunity, col) == gid)))
            by_role[r] = len(rows)
            for o in rows:
                key = o.opportunity_guid or o.crm_id
                # One Verkaufschance, not one row per role: a firm that is
                # Käufer AND Endkunde on the same deal is on ONE deal. Same rule
                # the Objekt drawer already applies to its firm list.
                entry = seen.get(key)
                if entry is None:
                    entry = _vc_row(o)
                    entry["roles"] = []
                    seen[key] = entry
                entry["roles"].append(r)

    out = sorted(seen.values(), key=lambda v: (v["created"] or "0000"), reverse=True)
    return {"rows": out[offset:offset + limit], "total": len(out),
            "returned": len(out[offset:offset + limit]),
            "by_role": {k: v for k, v in by_role.items() if v}}


def _kurzprofil(c: Company, beleg: dict, roles: dict) -> str:
    """One paragraph, every clause traceable to a column. German, for the team."""
    bits: list[str] = []
    seg = c.sub_segment or c.segment
    where = c.city or c.country
    lead = f"{seg or 'Firma'} in {where}" if where else (seg or "Firma")
    if c.positioning:
        lead += f", Positionierung {c.positioning}"
    if c.own_fabrication:
        lead += ", eigene Fertigung"
    bits.append(lead + ".")
    if beleg.get("events"):
        bits.append(f"Seit {beleg['first'][:4]} {beleg['events']} Bestellungen "
                    f"über {beleg['total']:,.0f} € (zuletzt {beleg['last']}).")
        if c.health and c.health not in ("aktiv",):
            bits.append(f"Status: {c.health}.")
    elif c.customer_state == "never" or not beleg.get("events"):
        bits.append("Bisher kein Kauf auf Beleg-Ebene.")
    k = roles.get("kaeufer") or {}
    if k.get("vcs"):
        wr = f", Gewinnrate {k['win_rate']:.0%}" if k.get("win_rate") is not None else ""
        bits.append(f"{k['vcs']} Verkaufschancen als Käufer{wr}.")
        if k.get("origins"):
            top_o = max(k["origins"], key=k["origins"].get)
            bits.append(f"Chancen überwiegend {top_o}.")
    a = roles.get("architekt") or {}
    if a.get("vcs"):
        bits.append(f"Als Architekt an {a['vcs']} Projekten beteiligt "
                    f"({a['won']} gewonnen).")
    if c.competitor_brands:
        bits.append("Führt " + ", ".join(c.competitor_brands[:3]) + ".")
    if c.quote_sum and c.conversion_rate is not None:
        bits.append(f"Angebote: {c.quote_sum:,.0f} € "
                    f"(Konversion {c.conversion_rate:.0%}).")
    return " ".join(bits)


def _product_block(s, company_id: int) -> dict | None:
    """What this company actually asks Solarlux for, by product family.

    From slx_product (see crm_import.import_products). The euros are QUOTED
    across won and lost deals alike, never invoiced revenue, so the drawer must
    label them as such — a dealer who asks for EUR 2 Mio and buys EUR 200k is a
    completely different conversation from one who asks for EUR 200k and buys it.
    """
    from .models import CrmCompanyProduct
    rows = list(s.scalars(select(CrmCompanyProduct)
                          .where(CrmCompanyProduct.company_id == company_id)))
    if not rows:
        return None
    rows.sort(key=lambda r: (-(r.value or 0), -r.positions))
    dates = [d for r in rows for d in (r.first_seen, r.last_seen) if d]
    return {
        "families": [{"family": r.family, "positions": r.positions,
                      "value": r.value,
                      "first": r.first_seen.isoformat() if r.first_seen else None,
                      "last": r.last_seen.isoformat() if r.last_seen else None}
                     for r in rows],
        "positions": sum(r.positions for r in rows),
        "value_quoted": round(sum(r.value or 0 for r in rows), 2) or None,
        "first": min(dates).isoformat() if dates else None,
        "last": max(dates).isoformat() if dates else None,
    }


def build(company_id: int) -> dict | None:
    """Everything linked to one company. Pure read, no network."""
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if c is None:
            return None
        beleg = _beleg_block(s, company_id)
        produkte = _product_block(s, company_id)

        roles: dict[str, dict] = {}
        all_pids: set[str] = set()
        gid = (c.crm_id or "").strip().lower()
        if gid:
            for role, col in _ROLE_COLS:
                rows = list(s.scalars(select(CrmOpportunity).where(
                    getattr(CrmOpportunity, col) == gid)))
                if rows:
                    roles[role] = _role_block(rows)
                    all_pids.update(o.project_id for o in rows if o.project_id)

        # the Objekte this company touches, resolved with names and outcomes so
        # the drawer can show "arbeitet an: <Adresse> (offen, 3 VCs)" directly
        projects: list[dict] = []
        if gid:
            # From ALL Verkaufschancen, not from the ten in `recent`. Deriving
            # the project ids from the truncated list meant a company with 1.266
            # Verkaufschancen showed only the Objekte touched by its ten newest,
            # with nothing saying so.
            pids = sorted(all_pids)[:20]
            if pids:
                members = list(s.scalars(select(CrmOpportunity).where(
                    CrmOpportunity.project_id.in_(pids))))
                grouped: dict[str, list[CrmOpportunity]] = defaultdict(list)
                for o in members:
                    grouped[o.project_id].append(o)
                from .insights.projekte import _outcome
                for pid, ms in grouped.items():
                    primary = next((m for m in ms
                                    if (m.opportunity_guid or "") == pid), ms[0])
                    projects.append({
                        "project_id": pid,
                        "name": primary.project_name or primary.name,
                        "status": _outcome(ms), "members": len(ms),
                        "type_of_use": primary.type_of_use,
                        "value": sum(float(m.order_value or 0) for m in ms) or None,
                    })
                projects.sort(key=lambda p: -(p["value"] or 0))

    return {
        "belege": beleg,
        "produkte": produkte,
        "rollen": roles,
        "projekte": projects,
        # how many Objekte exist behind the (capped) list above, so the drawer
        # can say "20 von 137" instead of implying it shows everything
        "projekte_total": len(all_pids),
        "kurzprofil": _kurzprofil(c, beleg, roles),
    }
