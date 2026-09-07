"""Wie weit sind wir mit diesem Büro schon gekommen? — eine Leiter, kein Ja/Nein.

Iheb wollte Architekturbüros bevorzugen, „mit denen wir schon gearbeitet haben".
Der naheliegende Weg wäre ein Häkchen „Bestandskunde". Bei Architekten geht das
nicht: sie KAUFEN nichts. Sie planen, schreiben aus und empfehlen. Ein Büro, mit
dem Solarlux drei gewonnene Objekte gebaut hat, hat oft keinen einzigen Beleg
und keine SAP-Nummer — die hat der ausführende Fachbetrieb.

Die Beziehung steht deshalb nicht in einer Spalte, sondern verteilt über fünf
Stellen im CRM. Gemessen 2026-09-07 über 20.722 Architekten:

    Stufe 5  auf einer GEWONNENEN Verkaufschance als Architekt benannt     167
    Stufe 4  auf einer Verkaufschance benannt, nicht gewonnen            1.586
    Stufe 3  E-Mail-Verkehr ans Büro angehängt                          2.237
    Stufe 2  SAP-Debitor angelegt                                       6.630
    Stufe 1  als Lead erfasst                                           8.761
    Stufe 0  nur Stammdaten                                                Rest

Die Stufen sind kumulativ zu lesen: wer auf einer gewonnenen VC steht, steht
meist auch in den Leads. Vergeben wird immer die HÖCHSTE erreichte Stufe.

WARUM DIE REIHENFOLGE SO IST. Sie folgt dem, wie belastbar der Kontakt ist,
nicht wie häufig. Eine gewonnene Verkaufschance ist ein gemeinsames Bauwerk —
das stärkste, was es gibt. Ein angehängter Schriftwechsel ist ein Mensch, der
mit dem Büro gesprochen hat. Eine SAP-Nummer heißt nur, dass irgendwann jemand
einen Debitor angelegt hat (6.630 Architekten haben eine, aber nur 67 je einen
Beleg) — deshalb steht sie UNTER dem Schriftwechsel, obwohl sie häufiger ist.
Ein Lead ist der schwächste Fall: er sagt, dass der Name im System steht.

WAS DIESE ZAHL NICHT IST. Sie misst nicht, wie gut das Büro zu Solarlux passt —
dafür gibt es `solarlux_relevance`. Sie misst, wie kalt ein Anruf wäre.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, or_, select, text

from ..db import SessionLocal
from ..models import Company, CrmEmail, CrmLead, CrmOpportunity

logger = logging.getLogger("adwatch.beziehung")

STUFEN: dict[int, str] = {
    5: "gemeinsames Objekt gewonnen",
    4: "auf einer Verkaufschance benannt",
    3: "Schriftverkehr vorhanden",
    2: "als Debitor angelegt",
    1: "als Lead erfasst",
    0: "nur Stammdaten",
}


def berechnen(nur_architekten: bool = True, apply: bool = False) -> dict:
    """Beziehungsstufe je Firma bestimmen — kostet nichts, jederzeit wiederholbar.

    Läuft in Mengenabfragen statt Zeile für Zeile: 20.722 Einzelabfragen mal
    fünf wären rund 100.000 Roundtrips gewesen. So sind es fünf.
    """
    with SessionLocal() as s:
        grund = select(Company.id, Company.crm_id)
        if nur_architekten:
            grund = grund.where(Company.segment == "Architekten")
        firmen = {cid: (guid or "") for cid, guid in s.execute(grund).all()}
        guid_zu_id = {g.lower(): c for c, g in firmen.items() if g}

        # Stufe 5 / 4 — als Architekt auf einer Verkaufschance benannt.
        # Der Join geht über die GUID, nicht über company_id: Verkaufschancen
        # zeigen auf `crm_id`.
        vc_alle: set[int] = set()
        vc_gewonnen: set[int] = set()
        for guid, zustand in s.execute(
                select(CrmOpportunity.architect_crm_id, CrmOpportunity.state)
                .where(CrmOpportunity.architect_crm_id.is_not(None),
                       CrmOpportunity.architect_crm_id != "")).all():
            cid = guid_zu_id.get((guid or "").lower())
            if cid is None:
                continue
            vc_alle.add(cid)
            if zustand == "gewonnen":
                vc_gewonnen.add(cid)

        mit_mail = {r[0] for r in s.execute(
            select(CrmEmail.company_id).where(CrmEmail.company_id.is_not(None))
            .group_by(CrmEmail.company_id)).all()}
        mit_lead = {r[0] for r in s.execute(
            select(CrmLead.company_id).where(CrmLead.company_id.is_not(None))
            .group_by(CrmLead.company_id)).all()}
        mit_sap = {r[0] for r in s.execute(
            select(Company.id).where(Company.sap_number.is_not(None),
                                     Company.sap_number != "")).all()}

        verteilung = {k: 0 for k in STUFEN}
        for cid in firmen:
            if cid in vc_gewonnen:
                stufe = 5
            elif cid in vc_alle:
                stufe = 4
            elif cid in mit_mail:
                stufe = 3
            elif cid in mit_sap:
                stufe = 2
            elif cid in mit_lead:
                stufe = 1
            else:
                stufe = 0
            verteilung[stufe] += 1
            if apply:
                s.execute(text("UPDATE companies SET relation_level = :s, "
                               "relation_why = :w WHERE id = :i"),
                          {"s": stufe, "w": STUFEN[stufe], "i": cid})
        if apply:
            s.commit()

    return {"firmen": len(firmen), "applied": apply,
            "verteilung": {f"{k} — {STUFEN[k]}": v
                           for k, v in sorted(verteilung.items(), reverse=True)},
            "warm": sum(v for k, v in verteilung.items() if k >= 3)}


def rangliste(land: str | None = None, min_stufe: int = 4,
              limit: int = 100) -> dict:
    """Die wärmsten Büros — wahlweise gefiltert auf ein Tätigkeitsland.

    `land` prüft gegen `active_countries` (Stufe „sicher"), also gegen das, WO
    das Büro baut — nicht gegen seine Postadresse. Genau darum geht es: ein
    Düsseldorfer Büro mit Projekten auf Mallorca ist für Spanien relevant, ein
    spanisches Büro ohne Website ist es nicht.
    """
    with SessionLocal() as s:
        stmt = (select(Company)
                .where(Company.segment == "Architekten",
                       Company.duplicate_of.is_(None),
                       func.coalesce(Company.relation_level, 0) >= min_stufe)
                .order_by(Company.relation_level.desc(),
                          func.coalesce(Company.arch_won_value, 0).desc())
                .limit(min(limit, 500)))
        zeilen = []
        for c in s.scalars(stmt):
            aktiv = c.active_countries or []
            if land and land.upper() not in [a.upper() for a in aktiv]:
                continue
            zeilen.append({
                "id": c.id, "name": c.name, "city": c.city, "country": c.country,
                "website": c.website_domain,
                "stufe": c.relation_level, "warum": c.relation_why,
                "objekte": c.arch_projects or 0, "gewonnen": c.arch_won or 0,
                "gewonnener_wert": round(c.arch_won_value or 0, 2),
                "aktiv_in": aktiv,
                "belege": (c.active_countries_evidence or {}).get(land.upper()) if land else None,
            })
    return {"rows": zeilen, "returned": len(zeilen),
            "filter": {"land": land, "min_stufe": min_stufe}}
