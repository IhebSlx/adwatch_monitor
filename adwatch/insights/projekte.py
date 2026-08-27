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

from sqlalchemy import or_, select

from ..db import SessionLocal
from ..models import Company, CrmOpportunity

log = logging.getLogger("adwatch.projekte")

# Project outcome, in precedence order: one win makes the project won, no
# matter how many sibling attempts died on the way.
WON, OPEN, LOST = "gewonnen", "offen", "verloren"


def specifying_architect(o: CrmOpportunity) -> str | None:
    """The architect crm_id of a Verkaufschance — only when it is a THIRD PARTY.

    `architect_crm_id` mirrors `slx_executingarchitect_accountid`, the AUSFÜHRENDER
    Architekt: whoever did the planning. When a dealer plans the job itself it
    enters itself, which is correct in the CRM and misleading in every count.

    Measured 2026-08-10 over the 7.331 Verkaufschancen with the field set:

        Architekt == Käufer   4.447 (60,7%)  -> Handel 2.295 · Verarbeiter 912 ·
                                                Baudienstleister 834 · Architekten 54
        Architekt != Käufer   2.884 (39,3%)  -> Architekten 2.589 · Baudienstleister 136

    So "12,7% of Verkaufschancen name an architect" — the figure in models.py and
    in the first pass of this analysis — is wrong. A genuinely third-party
    architect appears on 2.884 of 57.776, i.e. 5,0%.

    Use this wherever an architect is COUNTED or LABELLED. `detail()` deliberately
    does not: showing a dealer in its own Architekt role is the truth about that
    deal, and hiding it would make the drawer disagree with Dynamics.
    """
    a = (o.architect_crm_id or "").strip()
    if not a:
        return None
    p = (o.parent_account_crm_id or "").strip()
    return None if p and a.lower() == p.lower() else a

# Member closures that MEAN "the project was won by someone else's VC" — they
# must never count as project losses.
_WON_ELSEWHERE = "Zugehörige VC gewonnen"


# Grouping 57.776 opportunities takes ~1,3 s and resolving 48k company names
# another ~1 s, and BOTH ran twice per request (overview + list). Opportunities
# only change on a CRM import, so the result is cached against a cheap
# fingerprint — a row count and the newest sync stamp. A stale cache is
# impossible without an import, and an import moves the fingerprint.
_CACHE: dict[str, object] = {"key": None, "groups": None, "names": None}


def _fingerprint(s) -> tuple:
    from sqlalchemy import func as _f
    return (s.scalar(select(_f.count(CrmOpportunity.id))),
            str(s.scalar(select(_f.max(CrmOpportunity.synced_at)))),
            s.scalar(select(_f.count(Company.id))))


def _project_rows() -> dict[str, list[CrmOpportunity]]:
    with SessionLocal() as s:
        key = _fingerprint(s)
        if _CACHE["key"] == key and _CACHE["groups"] is not None:
            return _CACHE["groups"]
        rows = list(s.scalars(select(CrmOpportunity)))
        names = {(c.crm_id or "").lower(): c.name
                 for c in s.execute(select(Company.crm_id, Company.name))
                 if c.crm_id}
    groups: dict[str, list[CrmOpportunity]] = defaultdict(list)
    for o in rows:
        gkey = o.project_id or o.opportunity_guid or o.crm_id
        groups[gkey].append(o)
    _CACHE.update({"key": key, "groups": groups, "names": names})
    return groups


def _company_names() -> dict[str, str]:
    """crm_id -> name, from the same cached pass as the grouping."""
    _project_rows()
    return _CACHE["names"] or {}


def invalidate_cache() -> None:
    """Call after any import that touches opportunities or companies."""
    _CACHE.update({"key": None, "groups": None, "names": None})


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


# How many Verkaufschancen hang on one Objekt is the strongest project-level
# signal measured so far — projects with more than one win 39,0% against 19,3%
# overall. It is also readable BEFORE the outcome, which is what makes it usable
# for an Ideal-Project-Profile: at the moment a second dealer registers the same
# building, the odds change. So the buckets are always computed over ALL groups,
# independent of whatever filter the table is showing, and serve as the
# reference table the filter is chosen from.
MEMBER_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1 VC", 1, 1),
    ("2 VCs", 2, 2),
    ("3–4 VCs", 3, 4),
    ("5–9 VCs", 5, 9),
    ("10+ VCs", 10, None),
)


def _projekt_schaetzwert(primary: CrmOpportunity,
                         members: list[CrmOpportunity]) -> float | None:
    """Was ist dieses Bauvorhaben wert? Der Wert der PRIMÄREN Verkaufschance.

    Vorher wurden die Schätzwerte aller Verkaufschancen eines Objekts addiert.
    Das ist falsch, und zwar erheblich: an einem Gebäude bekommen mehrere
    Händler und Generalunternehmer dasselbe Gewerk angeboten — gewinnen kann
    nur einer. Karlsruhe, Rheinstraße 91 stand deshalb mit 14,7 Mio EUR in der
    Liste; derselbe Betrag von 2.293.202 lag dort viermal, 1.277.564 dreimal.

    Welche Regel stimmt, ist messbar: bei GEWONNENEN Objekten kennen wir den
    tatsächlichen Auftragswert. Gemessen an 581 solchen Objekten, Verhältnis
    Formel zu Auftrag:

        Summe aller Schätzwerte     Median 2,41x   28 % im Bereich 0,5–2,0
        größter Schätzwert          Median 1,21x   82 %
        PRIMÄRE Verkaufschance      Median 1,01x   85 %      <- praktisch unverzerrt

    Die primäre Verkaufschance trägt den Projektwert also bereits; sie musste
    nur gelesen statt aufsummiert werden. Verfügbar für 52.576 von 52.796
    Objekten — für den Rest bleibt der größte Einzelwert als Rückfall, weil er
    von den verbleibenden Regeln am wenigsten danebenliegt.
    """
    p = float(primary.estimated_value or 0)
    if p > 0:
        return p
    werte = [float(m.estimated_value or 0) for m in members]
    return max(werte) or None


def _in_bucket(n: int, lo: int, hi: int | None) -> bool:
    return n >= lo and (hi is None or n <= hi)


def overview(min_members: int = 1, max_members: int | None = None) -> dict:
    """The corrected Objektvertrieb picture: projects, not Verkaufschancen.

    `min_members`/`max_members` must match the list the KPIs sit above, or the
    header claims 8.189 won projects while the table shows only the 3.580 that
    have more than one Verkaufschance. Counts of two different populations on
    one screen.
    """
    groups = _project_rows()
    counts = {WON: 0, OPEN: 0, LOST: 0}
    multi = 0
    won_value = 0.0
    buckets = [{"label": lab, "min": lo, "max": hi,
                "projects": 0, WON: 0, LOST: 0, OPEN: 0}
               for lab, lo, hi in MEMBER_BUCKETS]
    for members in groups.values():
        n = len(members)
        out = _outcome(members)
        for b in buckets:                       # unfiltered: reference table
            if _in_bucket(n, b["min"], b["max"]):
                b["projects"] += 1
                b[out] += 1
                break
        if n < min_members or (max_members is not None and n > max_members):
            continue
        counts[out] += 1
        if n > 1:
            multi += 1
        if out == WON:
            won_value += sum(float(m.order_value or 0) for m in members)
    for b in buckets:
        dec = b[WON] + b[LOST]
        b["win_rate"] = round(b[WON] / dec, 4) if dec else None
    total = sum(counts.values())
    decided = counts[WON] + counts[LOST]
    return {
        "projects": total,
        "multi_vc_projects": multi,
        "all_projects": len(groups),
        **counts,
        "won_value": round(won_value, 2),
        # the number the VC-level analysis got wrong: win rate at PROJECT level
        "project_win_rate": round(counts[WON] / decided, 4) if decided else None,
        "member_buckets": buckets,
    }


def list_projects(status: str | None = None, min_members: int = 1,
                  limit: int = 200, q: str | None = None,
                  min_value: float = 0.0, lost_reason: str | None = None,
                  max_members: int | None = None) -> dict:
    """Projects, most valuable first, with their members and roles resolved.

    Returns {"rows": [...], "total": n, "returned": len(rows)} — the total is the
    count that MATCHED, before the limit. Without it the UI can only report "34 of
    300 rows" and a reader reasonably concludes 300 is the whole set, when there
    are 52.796 projects and 8.189 won ones. Filtering happens here, over all of
    them; only the slice sent to the browser is capped.""" 
    groups = _project_rows()
    names = _company_names()

    def name_of(gid):
        return names.get((gid or "").lower())

    # Two passes. The first is cheap and runs over all 52.796 projects to decide
    # what MATCHES and how it ranks; the second builds the display dict only for
    # the page. Building all 52.796 dicts — resolving firms and architects for
    # each — to then return 300 cost 4,4 s per request on its own.
    candidates = []
    for key, members in groups.items():
        if len(members) < min_members:
            continue
        if max_members is not None and len(members) > max_members:
            continue
        outcome = _outcome(members)
        if status and outcome != status:
            continue
        # Sortierung und der Filter „Mindestwert" müssen denselben Wert
        # benutzen wie die Anzeige — sonst steht ein Objekt weiter oben, als
        # seine eigene Zahl rechtfertigt.
        _prim = next((m for m in members if (m.opportunity_guid or "") == key),
                     members[0])
        rank = (sum(float(m.order_value or 0) for m in members)
                or _projekt_schaetzwert(_prim, members) or 0)
        if min_value and rank < min_value:
            continue
        if lost_reason and not any((m.lost_reason or "") == lost_reason
                                   for m in members):
            continue
        candidates.append((rank, key, members, outcome))

    if q:
        needle = q.strip().lower()

        def hit(key, members):
            primary = next((m for m in members
                            if (m.opportunity_guid or "") == key), members[0])
            if needle in ((primary.project_name or primary.name or "").lower()):
                return True
            return any(needle in (name_of(g) or "").lower()
                       for m in members
                       for g in (m.parent_account_crm_id, m.architect_crm_id))
        candidates = [c for c in candidates if hit(c[1], c[2])]

    total = len(candidates)
    candidates.sort(key=lambda c: -c[0])

    out = []
    for _rank, key, members, outcome in candidates[:limit]:
        primary = next((m for m in members if (m.opportunity_guid or "") == key),
                       members[0])
        value = sum(float(m.order_value or 0) for m in members) or None
        est = _projekt_schaetzwert(primary, members)
        firms = sorted({n for n in (name_of(m.parent_account_crm_id)
                                    for m in members) if n})
        # third-party architects only — see specifying_architect(). The column
        # otherwise repeats the Käufer under an "Architekten" heading on 6 of
        # every 10 rows that have the field at all.
        archs = sorted({n for n in (name_of(specifying_architect(m))
                                    for m in members) if n})
        out.append({
            "project_id": key,
            "name": primary.project_name or primary.name or "(ohne Namen)",
            "status": outcome,
            "members": len(members),
            "won_members": sum(1 for m in members if m.state == "gewonnen"),
            "order_value": value, "estimated_value": est,
            "channel": primary.sales_channel,
            # When the FIRST Verkaufschance on this building was created in the
            # CRM — the oldest created_on among the members. It is the start of
            # the pipeline for this Objekt, and the only date available before
            # anything is decided. Explicitly NOT: the building's construction
            # date, the quote date, or the closing date (closed_on, per member,
            # is in `timeline`). Shown as its own column because putting it under
            # the name made it read as part of the name.
            "created": min((m.created_on for m in members if m.created_on),
                           default=None),
            "firms": firms[:6], "architects": archs[:4],
            "lost_reasons": sorted({m.lost_reason for m in members
                                    if m.lost_reason
                                    and m.lost_reason != _WON_ELSEWHERE}),
        })
    for p in out:
        if p["created"]:
            p["created"] = p["created"].date().isoformat()
    return {"rows": out, "total": total, "returned": len(out)}


def detail(project_id: str) -> dict | None:
    """Everything ever linked to one Objekt.

    An Objekt is not a record in the CRM — it is a GROUP of Verkaufschancen that
    share sl_primary_opportunityid. So there is no single row to open; the drawer
    has to assemble the project from its members, and that assembly is the whole
    point: one win among five losses is a WON project, and only this view shows
    why the other four were lost.

    Returns the members with their full detail, every company in every role with
    what it did, the product mix specified on the site, the SAP orders that came
    out of it, and a dated timeline.
    """
    from ..models import Company, CrmOpportunityProduct

    with SessionLocal() as s:
        # The group key is `project_id OR opportunity_guid OR crm_id` (see
        # _project_rows) — a Verkaufschance with no project id forms a project of
        # one under its own guid. Matching only project_id 404s on every one of
        # those, which is most single-VC projects.
        members = list(s.scalars(select(CrmOpportunity).where(
            CrmOpportunity.project_id == project_id)))
        if not members:
            members = [o for o in s.scalars(select(CrmOpportunity).where(
                or_(CrmOpportunity.opportunity_guid == project_id,
                    CrmOpportunity.crm_id == project_id)))
                if not o.project_id]
        if not members:
            return None

        primary = next((m for m in members
                        if (m.opportunity_guid or "") == project_id), members[0])
        gids = {g.lower() for m in members for g in
                (m.parent_account_crm_id, m.architect_crm_id, m.end_customer_crm_id) if g}
        by_crm = {(c.crm_id or "").lower(): c for c in s.scalars(
            select(Company).where(Company.crm_id.is_not(None)))} if gids else {}

        guids = [m.opportunity_guid.lower() for m in members if m.opportunity_guid]
        prod_rows = list(s.scalars(select(CrmOpportunityProduct).where(
            CrmOpportunityProduct.opportunity_guid.in_(guids)))) if guids else []

    # --- companies, one entry per firm with every role it played -------------
    firms: dict[str, dict] = {}
    for m in members:
        for role, gid in (("kaeufer", m.parent_account_crm_id),
                          ("architekt", m.architect_crm_id),
                          ("endkunde", m.end_customer_crm_id)):
            if not gid:
                continue
            key = gid.lower()
            c = by_crm.get(key)
            e = firms.setdefault(key, {
                "company_id": c.id if c else None,
                "name": c.name if c else "(nicht in unseren Stammdaten)",
                "city": c.city if c else None, "segment": c.segment if c else None,
                "roles": set(), "_vcs": set(), "_won": set(), "value": 0.0})
            e["roles"].add(role)
            # DISTINCT Verkaufschancen, not role occurrences. A firm that is
            # Käufer, Architekt and Endkunde on the same deal is on ONE deal —
            # counting roles reported 9 VCs on a project that has 4.
            e["_vcs"].add(m.opportunity_guid or m.crm_id)
            if m.state == "gewonnen":
                e["_won"].add(m.opportunity_guid or m.crm_id)
                e["value"] += float(m.order_value or 0)
    for e in firms.values():
        e["roles"] = sorted(e["roles"])
        e["vcs"] = len(e.pop("_vcs"))
        e["won"] = len(e.pop("_won"))
        e["value"] = round(e["value"], 2) or None

    # --- product mix across every member ------------------------------------
    fam: dict[str, list] = {}
    for r in prod_rows:
        slot = fam.setdefault(r.family, [0, 0.0])
        slot[0] += r.positions
        slot[1] += float(r.value or 0)
    produkte = sorted(({"family": f, "positions": n, "value": round(v, 2) or None}
                       for f, (n, v) in fam.items()),
                      key=lambda p: -(p["value"] or 0))

    # --- timeline: one dated entry per member, oldest first ------------------
    timeline = sorted(
        ({"date": (m.created_on.date().isoformat() if m.created_on else None),
          "closed": (m.closed_on.date().isoformat() if m.closed_on else None),
          "number": m.number, "state": m.state, "lost_reason": m.lost_reason,
          "value": m.order_value or m.estimated_value,
          "firm": (by_crm.get((m.parent_account_crm_id or "").lower()).name
                   if by_crm.get((m.parent_account_crm_id or "").lower()) else None)}
         for m in members),
        key=lambda x: (x["date"] or "9999"))

    sap = sorted({n for m in members for n in (m.sap_order_numbers or [])})
    won = [m for m in members if m.state == "gewonnen"]
    # A project can be won WITHOUT a won member here: the CRM marks the siblings
    # "Zugehörige VC gewonnen" while the winning Verkaufschance itself sits
    # outside the window we imported. Without saying so, the drawer shows
    # "gewonnen · 0 gewonnene VCs · kein Wert" and reads like a bug.
    won_elsewhere = any((m.lost_reason or "") == _WON_ELSEWHERE for m in members)
    won_via = ("zugehörige VC ausserhalb des geladenen Zeitraums"
               if not won and won_elsewhere else None)
    return {
        "project_id": project_id,
        "name": primary.project_name or primary.name or "(ohne Namen)",
        "status": _outcome(members),
        "address": " ".join(x for x in (primary.street, primary.postal_code,
                                        primary.city) if x) or None,
        "type_of_use": primary.type_of_use,
        "channel": primary.sales_channel,
        "origin": primary.origin,
        "members": len(members),
        "won_members": len(won),
        "won_via": won_via,
        "order_value": round(sum(float(m.order_value or 0) for m in members), 2) or None,
        # Schätzwert des Objekts = primäre Verkaufschance, nicht Summe der
        # Geschwister — siehe _projekt_schaetzwert().
        "estimated_value": _projekt_schaetzwert(primary, members),
        "won_value": round(sum(float(m.order_value or 0) for m in won), 2) or None,
        "first": timeline[0]["date"] if timeline else None,
        "last": max((t["closed"] or t["date"] or "") for t in timeline) or None,
        "firms": sorted(firms.values(), key=lambda f: (-(f["value"] or 0), f["name"])),
        "produkte": produkte,
        "sap_orders": sap,
        "lost_reasons": sorted({m.lost_reason for m in members
                                if m.lost_reason and m.lost_reason != _WON_ELSEWHERE}),
        "timeline": timeline,
    }
