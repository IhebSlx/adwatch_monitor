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


def bueros(land: str = "ES", min_stufe: int = 0, nur_warm: bool = False,
           filters: dict | None = None) -> dict:
    """EIN BÜRO JE ZEILE — die Orte sind eine Spalte, nicht die Gliederung.

    Die erste Fassung dieser Liste war nach Orten gegliedert, weil sie aus der
    Karte entstanden ist. Iheb hat widersprochen, und zwar zu Recht: gesucht
    werden Büros, die in einem Land bauen. Der Ort ist eine EIGENSCHAFT des
    Büros. Nach Orten gegliedert steht dasselbe Büro in zwanzig Zeilen, und
    man kann weder abhaken noch anrufen.

    Dieselbe Auswahl wie `orte()`, nur andersherum aufgeschlüsselt — beide
    holen sich ihre Grundmenge über denselben Filter, damit die Zahlen
    zusammenpassen.
    """
    roh = _orte_roh(land=land, min_stufe=min_stufe, nur_warm=nur_warm,
                    filters=filters)

    je_buero: dict[int, dict] = {}
    for pin in roh["pins"]:
        # `pin["liste"]` ist hier die VOLLE Liste. `orte()` kuerzt sie fuer die
        # Karte auf 40 -- und genau daraus hat diese Funktion frueher gelesen.
        # Gemessen 2026-09-08: Barcelona nennt 92 Bueros, Madrid 83, also
        # fielen 95 Listeneintraege weg und die Spanien-Liste zeigte 213 statt
        # 231 Bueros. Ein Buero, dessen einzige spanische Orte Barcelona und
        # Madrid sind und das dort auf Platz 41 stand, kam in der Arbeitsliste
        # ueberhaupt nicht vor -- und damit auch nicht in der Excel fuer
        # Daniel. Eine Kuerzung fuer die ANZEIGE darf nie die DATENMENGE
        # beschneiden.
        for b in pin["liste"]:
            zeile = je_buero.get(b["id"])
            if zeile is None:
                zeile = je_buero[b["id"]] = {
                    "id": b["id"], "name": b["name"], "sitz": b["sitz"],
                    "land": b["land"], "website": b["website"],
                    "stufe": b["stufe"], "orte": [],
                }
            zeile["orte"].append(pin["ort"])

    zeilen = list(je_buero.values())
    # Rolle und Projekthistorie dazu — sie stehen in `companies`, und ohne sie
    # ist die Liste eine Adressliste statt einer Arbeitsliste.
    if zeilen:
        with SessionLocal() as s:
            ids = ",".join(str(z["id"]) for z in zeilen)
            zusatz = {r[0]: r for r in s.execute(_sql(
                "SELECT id, decision_role, relation_why, "
                "COALESCE(arch_projects,0), COALESCE(arch_won,0), "
                "COALESCE(arch_won_value,0) "
                f"FROM companies WHERE id IN ({ids})")).all()}
        for z in zeilen:
            r = zusatz.get(z["id"])
            z["rolle"] = (r[1] if r else None) or ""
            z["warum"] = (r[2] if r else None) or ""
            z["objekte"] = r[3] if r else 0
            z["gewonnen"] = r[4] if r else 0
            z["gewonnener_wert"] = round(r[5], 2) if r else 0.0
            z["orte"] = sorted(set(z["orte"]))

    zeilen.sort(key=lambda z: (-z["stufe"], -len(z["orte"]), z["name"] or ""))
    return {"land": land, "rows": zeilen, "bueros": len(zeilen),
            "orte": roh["orte"], "ohne_koordinate": roh["ohne_koordinate"]}


def _je_buero_einmal(bueros: list[dict]) -> list[dict]:
    """Ein BÜRO je Zeile, nicht eine CRM-Zeile je Zeile.

    Iheb: „in Büros (die wärmsten zuerst) there is a lot of repetition."
    Stimmte. Ein Büro steht im CRM oft mehrfach — mit derselben Website:

        bofill.com          Bofill Architects · Ricardo Bofill- Taller de …  (3×)
        gmp-architekten.de  GMP Architekten … · GMP Van Gerkan … · GMP von … (3×)
        mateoclosa.com      Estudio Closa-Godoy · Esudio Closa- Godoy  (Tippfehler)
        hofmandujardin.nl   Hofman Dujardin Holding B.V. · HofmanDujardin

    Die Dublettenprüfung fängt das nicht: `Esudio` gegen `Estudio` ist ein
    Tippfehler, `Herzog & de Meuron Basel` ein Standort. Beides sind echte,
    getrennte CRM-Konten — nur eben EIN Büro, und in einer Ortsliste liest
    sich dieselbe Firma dreimal wie ein Fehler.

    Zusammengefasst wird deshalb hier, in der ANZEIGE, über die Domain. Die
    Konten bleiben unangetastet; gezeigt wird die Zeile mit der höchsten
    Beziehungsstufe, bei Gleichstand die mit dem längeren Namen (er trägt
    meist die Rechtsform und ist der vollständigere).
    """
    beste: dict[str, dict] = {}
    ohne_domain: list[dict] = []
    for b in bueros:
        schluessel = (b.get("website") or "").strip().lower()
        if not schluessel:
            ohne_domain.append(b)       # ohne Domain kein Zusammenfassen
            continue
        alt = beste.get(schluessel)
        if alt is None or (b["stufe"], len(b["name"] or "")) > (alt["stufe"], len(alt["name"] or "")):
            beste[schluessel] = b
    return list(beste.values()) + ohne_domain


# Wie viele Büros je Nadel im Kartenaufruf mitfahren. Die Sprechblase zeigt
# ohnehin nur die ersten paar Zeilen, und Barcelona mit 92 Büros an 324
# Nadeln wäre unnötig viel Leitung. NUR eine Anzeigegrenze: `_orte_roh` gibt
# die vollständigen Listen zurück, und `bueros()` liest von dort.
_LISTE_JE_NADEL = 40


def orte(land: str = "ES", min_stufe: int = 0, nur_warm: bool = False,
         filters: dict | None = None) -> dict:
    """Die genannten Orte eines Landes als Kartennadeln — für die Leitung.

    Identisch zu `_orte_roh`, nur mit gekürzten Bürolisten je Nadel. Wer die
    Daten braucht und nicht die Karte, nimmt `_orte_roh`.
    """
    d = _orte_roh(land=land, min_stufe=min_stufe, nur_warm=nur_warm,
                  filters=filters)
    d["pins"] = [{**p, "liste": p["liste"][:_LISTE_JE_NADEL]} for p in d["pins"]]
    return d


def _orte_roh(land: str = "ES", min_stufe: int = 0, nur_warm: bool = False,
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
        bueros = _je_buero_einmal(bueros)
        bueros.sort(key=lambda b: (-b["stufe"], b["name"]))
        pins.append({
            "ort": ort, "lat": round(lat, 5), "lng": round(lng, 5),
            "bueros": len(bueros),
            "warm": sum(1 for b in bueros if b["stufe"] >= 3),
            "flaeche": ort.strip().lower() in _FLAECHEN,
            "liste": bueros,
        })
    pins.sort(key=lambda p: -p["bueros"])
    # ZWEI WAHRHEITEN FUER DIESELBE MENGE, und das war die falsche.
    # Gezaehlt wurden bisher die rohen CRM-Zeilen (243) -- also genau die
    # Mehrfachnennungen, die Iheb in der Liste geaergert haben ("there is a
    # lot of repetition") und die `_je_buero_einmal` deshalb zusammenfasst.
    # Die Kartenlegende sagte 243, die Liste darunter 213. Gezaehlt wird jetzt,
    # was auch gezeigt wird: zusammengefasste Bueros, und nur die, deren Ort
    # eine Koordinate hat und damit ueberhaupt auf der Karte landet.
    return {"land": land, "pins": pins, "orte": len(pins),
            "bueros": len({b["id"] for p in pins for b in p["liste"]}),
            "ohne_koordinate": sorted(ohne)}
