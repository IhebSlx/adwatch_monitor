"""Angebot → Auftrag: was von dem, was wir anbieten, tatsächlich ankommt.

WARUM DAS FEHLTE, OBWOHL DIE APP VOLLER MODELLE IST.
Jedes Profil in AdWatch ist auf GEWINNEN trainiert — „wird aus dieser
Verkaufschance eine gewonnene". Das war lange das Einzige, was messbar war.
Mit dem SAP-Beleg-Join (`crm_import.import_opportunity_links`) liegt seit dem
Projektwert-Umbau erstmals daneben, was FAKTURIERT wurde: 7.909 Verkaufschancen
tragen einen echten Belegwert. Ausgewertet hat das nie jemand.

Der Unterschied ist nicht akademisch. Eine gewonnene Verkaufschance über
20.000 € und eine über 400.000 € zählen in jeder Gewinnrate gleich viel. Die
Frage des Vertriebs ist aber nicht „wo gewinnen wir oft", sondern „wo kommt
Geld an".

ZWEI MASSE, WEIL EINES ALLEIN LÜGT.

1. `gewinnrate` = gewonnen / (gewonnen + verloren), auf Ebene der
   Verkaufschance. Hängt an KEINEM Beleg und ist deshalb das ehrliche Maß für
   die Rangfolge. Offene Chancen fallen heraus — sie sind noch nichts.

2. `euro_quote` = fakturiert / angeboten. Näher an der eigentlichen Frage, aber
   systematisch nach UNTEN verzerrt: nur 19 % der Verkaufschancen tragen
   überhaupt einen Beleglink, und eine gewonnene ohne Link zählt mit 0 €. Die
   Quote ist damit eine Untergrenze, keine Quote. `beleg_deckung` sagt, wie
   groß der blinde Fleck ist.

Beide zeigten in der ersten Messung (2026-09-02) in dieselbe Richtung. Bei
einer Grundlinie von 21,3 %:

    Wohnungswirtschaft   37,5 %  (29,3–46,4)   n=120    Euro-Quote 28,5 %
    Gebäudebetreiber     30,9 %  (22,6–40,7)   n=97     Euro-Quote 15,9 %
    Handel               21,6 %  (20,9–22,3)   n=13.453 Euro-Quote 11,2 %
    Verarbeiter          21,2 %  (20,3–22,1)   n=8.309  Euro-Quote 10,2 %
    Baudienstleister     17,4 %  (14,8–20,3)   n=714    Euro-Quote  9,1 %
    Architekten           5,9 %   (2,7–12,2)   n=102    Euro-Quote  0,8 %

Dass beide Maße dieselbe Rangfolge ergeben, macht die Spur belastbarer als
eines allein. Es macht sie nicht zur Aussage — Wohnungswirtschaft steht auf 120
entschiedenen Verkaufschancen, und darum trägt jede Zeile ein Intervall.
Architekten unten ist die bekannte Architekten-Falle: sie schreiben aus, sie
kaufen nicht. Dass sie hier sauber am unteren Ende landet, spricht dafür, dass
das Maß sich vernünftig verhält.

WARUM WILSON UND NICHT DER NACKTE ANTEIL.
37,5 % von 120 und 21,6 % von 13.453 sehen nebeneinander aus wie ein klarer
Unterschied. Das Wilson-Intervall (Wilson 1927; robuster als die
Normalapproximation bei kleinem n und Anteilen nahe 0 oder 1) macht sichtbar,
dass das erste ±8 Punkte wackelt und das zweite ±0,7. Erst wenn ein Intervall
die Grundlinie nicht mehr enthält, wird eine Zeile als `ueber_basis` bzw.
`unter_basis` markiert — die Behauptung steht dann nicht auf der
Punktschätzung, sondern auf ihrem Rand.
"""
from __future__ import annotations

import math

from sqlalchemy import case, func, select

from ..db import SessionLocal
from ..models import Company, CrmOpportunity

# Unter dieser Zahl entschiedener Verkaufschancen wird eine Zeile als
# `belastbar: False` markiert. Nicht versteckt — versteckte Zeilen sucht
# irgendwann jemand von Hand wieder zusammen —, aber gekennzeichnet.
MIN_ENTSCHIEDEN = 100

# Die Gruppierungen, die es gibt. Firmenmerkmale kommen aus `companies`,
# Projektmerkmale aus der Verkaufschance selbst.
DIMENSIONEN: dict[str, tuple] = {
    "segment": (Company.segment, "Segment"),
    "sub_segment": (Company.sub_segment, "Untersegment"),
    "land": (Company.country, "Land"),
    "vertriebsweg": (Company.sales_channel, "Vertriebsweg"),
    "nutzung": (CrmOpportunity.type_of_use, "Gebäudenutzung"),
    "vc_art": (CrmOpportunity.vc_type, "Art der Verkaufschance"),
    "herkunft": (CrmOpportunity.origin, "Herkunft der Chance"),
}


def wilson(treffer: int, versuche: int, z: float = 1.96) -> tuple[float, float]:
    """Konfidenzintervall für einen Anteil (95 %, Wilson).

    Gegenüber der Normalapproximation der ehrlichere Kandidat: bei kleinem n
    oder Anteilen nahe 0/1 läuft letztere aus dem Intervall [0,1] heraus und
    behauptet Genauigkeit, die nicht da ist.
    """
    if versuche <= 0:
        return (0.0, 1.0)
    p = treffer / versuche
    nenner = 1 + z * z / versuche
    mitte = (p + z * z / (2 * versuche)) / nenner
    rand = (z / nenner) * math.sqrt(p * (1 - p) / versuche
                                    + z * z / (4 * versuche * versuche))
    return (max(0.0, mitte - rand), min(1.0, mitte + rand))


def nach(dimension: str = "segment", land: str | None = None,
         min_entschieden: int = 1) -> dict:
    """Angebot → Auftrag je Gruppe, absteigend nach Gewinnrate.

    Grundgesamtheit sind Verkaufschancen mit einem Angebotswert (`quoted_value`)
    — ohne Angebot gibt es nichts zu konvertieren. Private Endkunden und
    Konzern-Töchter fallen wie überall heraus.
    """
    spalte, titel = DIMENSIONEN.get(dimension, DIMENSIONEN["segment"])

    with SessionLocal() as s:
        stmt = (
            select(
                spalte.label("gruppe"),
                func.count(CrmOpportunity.id).label("vcs"),
                func.sum(func.coalesce(CrmOpportunity.quoted_value, 0)).label("angeboten"),
                func.sum(func.coalesce(CrmOpportunity.invoiced_value, 0)).label("fakturiert"),
                func.sum(case((CrmOpportunity.state == "gewonnen", 1), else_=0)).label("gewonnen"),
                func.sum(case((CrmOpportunity.state == "verloren", 1), else_=0)).label("verloren"),
                func.sum(case(
                    (func.coalesce(CrmOpportunity.invoiced_value, 0) > 0, 1), else_=0)).label("mit_beleg"),
            )
            .join(Company, Company.crm_id == CrmOpportunity.parent_account_crm_id)
            .where(
                func.coalesce(CrmOpportunity.quoted_value, 0) > 0,
                spalte.is_not(None), spalte != "",
                Company.segment != "Private Endkunden",
                func.coalesce(Company.is_intercompany, False).is_(False),
            )
            .group_by(spalte)
        )
        if land:
            stmt = stmt.where(func.upper(Company.country) == land.strip().upper())
        roh = s.execute(stmt).all()

    zeilen = []
    for gruppe, vcs, angeboten, fakturiert, gewonnen, verloren, mit_beleg in roh:
        entschieden = (gewonnen or 0) + (verloren or 0)
        if entschieden < min_entschieden:
            continue
        rate = (gewonnen / entschieden) if entschieden else None
        lo, hi = wilson(gewonnen or 0, entschieden) if entschieden else (None, None)
        zeilen.append({
            "gruppe": gruppe,
            "vcs": vcs,
            "entschieden": entschieden,
            "gewonnen": gewonnen or 0,
            "gewinnrate": round(rate, 4) if rate is not None else None,
            "gewinnrate_lo": round(lo, 4) if lo is not None else None,
            "gewinnrate_hi": round(hi, 4) if hi is not None else None,
            "angeboten": round(angeboten or 0, 2),
            "fakturiert": round(fakturiert or 0, 2),
            "euro_quote": round((fakturiert or 0) / angeboten, 4) if angeboten else None,
            "beleg_deckung": round((mit_beleg or 0) / vcs, 4) if vcs else None,
            "belastbar": entschieden >= MIN_ENTSCHIEDEN,
        })

    zeilen.sort(key=lambda z: (-(z["gewinnrate"] or 0), -z["entschieden"]))

    # Die Gesamtlinie ist der Bezugspunkt: eine Gewinnrate ohne Grundlinie
    # daneben ist eine Zahl, die jeder in die Richtung liest, die ihm passt.
    ges_entschieden = sum(z["entschieden"] for z in zeilen)
    ges_gewonnen = sum(z["gewonnen"] for z in zeilen)
    ges_angeboten = sum(z["angeboten"] for z in zeilen)
    ges_fakturiert = sum(z["fakturiert"] for z in zeilen)
    basis = (ges_gewonnen / ges_entschieden) if ges_entschieden else None

    for z in zeilen:
        # „Wie viel besser als der Durchschnitt" — aber nur dort behauptet, wo
        # das Intervall die Grundlinie gar nicht mehr enthält.
        z["ueber_basis"] = (
            bool(basis is not None and z["gewinnrate_lo"] is not None
                 and z["gewinnrate_lo"] > basis))
        z["unter_basis"] = (
            bool(basis is not None and z["gewinnrate_hi"] is not None
                 and z["gewinnrate_hi"] < basis))

    return {
        "dimension": dimension, "titel": titel, "land": land,
        "zeilen": zeilen,
        "basis_gewinnrate": round(basis, 4) if basis is not None else None,
        "basis_euro_quote": (round(ges_fakturiert / ges_angeboten, 4)
                             if ges_angeboten else None),
        "entschieden": ges_entschieden,
        "angeboten": round(ges_angeboten, 2),
        "fakturiert": round(ges_fakturiert, 2),
        "beleg_deckung_gesamt": round(
            sum(z["vcs"] * (z["beleg_deckung"] or 0) for z in zeilen)
            / sum(z["vcs"] for z in zeilen), 4) if zeilen else None,
        "min_entschieden": MIN_ENTSCHIEDEN,
        "dimensionen": {k: v[1] for k, v in DIMENSIONEN.items()},
    }
