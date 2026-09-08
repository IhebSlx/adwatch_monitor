"""Wo im Zielland wird gebaut? — die Orte auf die Karte, nicht die Büros.

ZWEI KARTEN, ZWEI FRAGEN.
Die vorhandene Firmenkarte zeigt, WO DIE BÜROS SITZEN. Mit dem Filter
`active_country=ES` zeigt sie 301 Nadeln — in München, Hamburg, London. Das ist
richtig und beantwortet „wer".

Diese hier beantwortet „wo". Gepinnt werden die ORTE, die auf den Websites
stehen: Barcelona 100, Madrid 88, Mallorca 38, Málaga 17. Die Nadel sitzt am
Ort, ihre Größe ist die Zahl der Büros, die ihn nennen — und dahinter hängt die
Liste dieser Büros.

Für Solarlux ist das die interessantere Karte: Großstädte sind Volumen, aber
die Gruppe um Mallorca und die Costa del Sol ist die, in der große Glasflächen
tatsächlich verbaut werden.

WOHER DIE KOORDINATEN KOMMEN.
Aus `plz_geo`, derselben Tabelle, aus der die Ortsnamen erkannt wurden — 94 %
der 324 spanischen Orte stehen dort mit Koordinate. Die restlichen 19 sind
Inseln und Regionen (Mallorca, Ibiza, Cataluña, Tenerife), die keine
Postleitzahl haben, weil sie kein Ort sind. Für die gibt es unten eine kleine
Tabelle von Hand — sie decken 79 der 324 Nennungen ab, also den mit Abstand
größten Einzelposten (Mallorca allein 38).
"""
from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import text as _sql

from .db import SessionLocal

# Inseln und Regionen: kein Ort, also keine PLZ, aber häufig genannt. Die
# Koordinate ist bewusst der Mittelpunkt der Fläche — die Nadel sagt „hier in
# der Gegend", nicht „an dieser Adresse", und die Kartenlegende sagt das auch.
_FLAECHEN: dict[str, tuple[float, float]] = {
    "mallorca": (39.60, 3.02), "majorca": (39.60, 3.02),
    "menorca": (39.95, 4.11), "ibiza": (38.98, 1.43),
    "formentera": (38.70, 1.44), "baleares": (39.60, 3.02),
    "balearen": (39.60, 3.02),
    "tenerife": (28.29, -16.63), "gran canaria": (27.96, -15.60),
    "lanzarote": (29.05, -13.59), "fuerteventura": (28.36, -14.05),
    "cataluna": (41.82, 1.87), "catalonia": (41.82, 1.87),
    "galicia": (42.75, -7.87), "andalucia": (37.54, -4.73),
    "costa brava": (41.90, 3.10), "costa del sol": (36.55, -4.70),
    "saragossa": (41.65, -0.89), "santa eulalia": (38.98, 1.53),
    "santa eulàlia": (38.98, 1.53), "canas": (42.42, -2.79),
}


def _koordinaten(land: str, namen: set[str]) -> dict[str, tuple[float, float]]:
    """Ortsname -> (lat, lng). Erst `plz_geo`, dann die Flächentabelle."""
    aus: dict[str, tuple[float, float]] = {}
    with SessionLocal() as s:
        for name in namen:
            r = s.execute(_sql(
                "SELECT AVG(lat), AVG(lng) FROM plz_geo "
                "WHERE country = :l AND lower(place) = lower(:p) "
                "AND lat IS NOT NULL"), {"l": land, "p": name}).first()
            if r and r[0] is not None:
                aus[name] = (float(r[0]), float(r[1]))
                continue
            # Akzente falten: die Flaechentabelle steht ohne, die Website
            # schreibt "Santa Eulàlia". Ohne diese Zeile faellt genau der
            # eine Ort durch, der einen Akzent traegt.
            schluessel = name.strip().lower()
            for a, b in (("à", "a"), ("á", "a"), ("è", "e"), ("é", "e"),
                         ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
                schluessel = schluessel.replace(a, b)
            flaeche = _FLAECHEN.get(schluessel) or _FLAECHEN.get(name.strip().lower())
            if flaeche:
                aus[name] = flaeche
    return aus


def orte(land: str = "ES", min_stufe: int = 0, nur_warm: bool = False,
         filters: dict | None = None) -> dict:
    """Die genannten Orte eines Landes als Kartennadeln.

    `min_stufe` filtert auf die Beziehungsstufe des NENNENDEN Büros — so lässt
    sich fragen „wo bauen die Büros, mit denen wir schon gearbeitet haben?",
    was etwas anderes ist als „wo wird überhaupt gebaut".

    `filters` ist DASSELBE Filterobjekt wie im Firmen-Explorer
    (`customers._apply_filters`). Ohne diesen Durchstich war die Karte ein
    Fremdkörper: Iheb hatte die Spaltenfilter über der Karte gesetzt, die Zahl
    oben sprang auf 151 — und die Karte zeigte unbeirrt alle 324 Orte. Die
    Filterleiste steht in dieser Ansicht sichtbar da, also MUSS sie wirken;
    eine sichtbare Bedienung, die nichts tut, ist schlimmer als keine.

    Die Grundmenge bleibt trotzdem eingegrenzt: gezeigt werden nur
    Architekturbüros mit erkannten Orten. Ein Filter kann diese Menge
    verkleinern, aber nicht über sie hinausgreifen.
    """
    from sqlalchemy import select

    from .customers import _apply_filters
    from .models import Company

    land = (land or "ES").upper()
    with SessionLocal() as s:
        stmt = select(Company.id, Company.name, Company.city, Company.country,
                      Company.website_domain, Company.active_cities,
                      Company.relation_level).where(
            Company.segment == "Architekten",
            Company.sub_segment == "Architekturbüro",
            Company.duplicate_of.is_(None),
            Company.active_cities.is_not(None),
            Company.active_cities != "{}")
        if filters:
            stmt = _apply_filters(stmt, filters)
        rows = s.execute(stmt).all()

    je_ort: dict[str, list[dict]] = defaultdict(list)
    for cid, name, stadt, sitz, web, roh, stufe in rows:
        stufe = stufe or 0
        if stufe < min_stufe or (nur_warm and stufe < 3):
            continue
        # Ueber die ORM-Spalte kommt bereits ein dict zurueck, ueber rohes SQL
        # ein String. Beides zulassen statt sich auf eine Herkunft zu verlassen.
        staedte = roh if isinstance(roh, dict) else json.loads(roh or "{}")
        for ort in (staedte.get(land) or []):
            je_ort[ort].append({"id": cid, "name": name, "sitz": stadt or "",
                                "land": sitz or "", "website": web or "",
                                "stufe": stufe})

    koord = _koordinaten(land, set(je_ort))
    pins, ohne = [], []
    for ort, bueros in je_ort.items():
        if ort not in koord:
            ohne.append(ort)
            continue
        lat, lng = koord[ort]
        bueros.sort(key=lambda b: (-b["stufe"], b["name"]))
        pins.append({
            "ort": ort, "lat": round(lat, 5), "lng": round(lng, 5),
            "bueros": len(bueros),
            "warm": sum(1 for b in bueros if b["stufe"] >= 3),
            "flaeche": ort.strip().lower() in _FLAECHEN,
            "liste": bueros[:40],
        })
    pins.sort(key=lambda p: -p["bueros"])
    return {"land": land, "pins": pins, "orte": len(pins),
            "bueros": len({b["id"] for v in je_ort.values() for b in v}),
            "ohne_koordinate": sorted(ohne)}
