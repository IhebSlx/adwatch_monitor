"""Geokodierung — billig zuerst, ehrlich immer.

Zwei Präzisionsstufen, bewusst getrennt:

  1. PLZ-Zentroid (dieses Modul): die GeoNames-Postleitzahltabellen (CC-BY 4.0,
     https://download.geonames.org/export/zip/) liefern für jede PLZ einen
     Ortsmittelpunkt. Ein lokaler Join pinnt damit die GESAMTE Firmenbasis auf
     Stadt-Genauigkeit — offline, kostenlos, in Sekunden. Für eine Marktkarte
     mit Clustern ist das die richtige Auflösung.
  2. Straßengenau (später, eigener Job): Nominatim erlaubt ~1 Anfrage/s — das
     ist ein fortsetzbarer Job über das bestehende Job-System, kein Join, und
     wird nur für Firmen bezahlt (in Zeit), bei denen es einen Unterschied
     macht.

Jede Koordinate trägt ihren Präzisionsgrad (`geocode_precision`), und die Karte
zeichnet ungefähre Pins anders als bewiesene — dieselbe Regel wie belegt vs.
KI-Einschätzung im Steckbrief. Eine 'manual' oder 'street' Koordinate wird vom
Zentroid-Lauf nie überschrieben.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select, update

from .db import SessionLocal
from .models import Company, PlzGeo

logger = logging.getLogger(__name__)

# GeoNames zip-Format: tab-getrennt, ohne Kopfzeile.
# 0 country, 1 postal, 2 place, ... 9 lat, 10 lng, 11 accuracy
_COL_CC, _COL_PLZ, _COL_PLACE, _COL_LAT, _COL_LNG = 0, 1, 2, 9, 10


def _norm_plz(cc: str, raw: str | None) -> str | None:
    """Eine PLZ so normalisieren, dass CRM-Schreibweise und GeoNames-Schreibweise
    aufeinandertreffen. Leerzeichen/Bindestriche raus, Großschreibung an; NL
    ("1234 AB" -> Ziffernteil "1234") und GB (Outward-Code vor dem Leerzeichen)
    tragen ihre Genauigkeit im Präfix."""
    if not raw:
        return None
    p = re.sub(r"[\s\-]", "", str(raw)).upper()
    if not p:
        return None
    if cc == "NL":
        m = re.match(r"^(\d{4})", p)
        return m.group(1) if m else p
    if cc == "GB":
        # GeoNames GB.zip führt Outward-Codes ("SW1A"); volle Codes kürzen
        m = re.match(r"^([A-Z]{1,2}\d[A-Z\d]?)", p)
        return m.group(1) if m else p
    return p


def import_geonames(folder: str | Path) -> dict:
    """Alle CC.zip-Dateien eines Ordners in die plz_geo-Tabelle laden.

    Mehrere GeoNames-Zeilen je PLZ (Ortsteile) werden zum Mittelwert
    zusammengezogen — der Zentroid IST die gewollte Aussage. Idempotent:
    vorhandene (Land, PLZ)-Paare werden ersetzt, nicht dupliziert."""
    folder = Path(folder)
    agg: dict[tuple[str, str], list] = defaultdict(lambda: [0.0, 0.0, 0, None])
    files = sorted(folder.glob("*.zip"))
    for zf_path in files:
        cc = zf_path.stem.upper()[:2]
        with zipfile.ZipFile(zf_path) as zf:
            # GB liefert die Daten als GB_full.txt-Variante nicht — Standard ist
            # <CC>.txt; readme.txt liegt daneben und wird übersprungen.
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")
                     and not n.lower().startswith("readme")]
            for name in names:
                with zf.open(name) as fh:
                    reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"),
                                        delimiter="\t")
                    for row in reader:
                        if len(row) <= _COL_LNG:
                            continue
                        try:
                            lat, lng = float(row[_COL_LAT]), float(row[_COL_LNG])
                        except ValueError:
                            continue
                        plz = _norm_plz(cc, row[_COL_PLZ])
                        if not plz:
                            continue
                        a = agg[(cc, plz)]
                        a[0] += lat; a[1] += lng; a[2] += 1
                        a[3] = a[3] or (row[_COL_PLACE] or None)

    with SessionLocal() as s:
        existing = {(r.country, r.plz): r.id for r in
                    s.execute(select(PlzGeo.country, PlzGeo.plz, PlzGeo.id))}
        inserted = updated = 0
        for (cc, plz), (lat_sum, lng_sum, n, place) in agg.items():
            lat, lng = lat_sum / n, lng_sum / n
            key = (cc, plz)
            if key in existing:
                s.execute(update(PlzGeo).where(PlzGeo.id == existing[key])
                          .values(lat=lat, lng=lng, place=place))
                updated += 1
            else:
                s.add(PlzGeo(country=cc, plz=plz, lat=lat, lng=lng, place=place))
                inserted += 1
        s.commit()
    return {"files": len(files), "codes": len(agg),
            "inserted": inserted, "updated": updated}


def assign_plz_centroids() -> dict:
    """Jeder Firma mit PLZ + Land eine Zentroid-Koordinate geben.

    Überschreibt NIE eine genauere Quelle: nur Zeilen ohne Koordinate oder mit
    precision='plz' werden gesetzt (re-runnbar, z. B. nach einem CRM-Import
    oder einer besseren GeoNames-Tabelle). 'street'/'manual' bleiben stehen."""
    with SessionLocal() as s:
        geo = {(g.country, g.plz): (g.lat, g.lng) for g in s.scalars(select(PlzGeo))}
        rows = s.execute(select(Company).where(
            Company.postal_code.is_not(None), Company.postal_code != "",
            Company.country.is_not(None))).scalars().all()
        hit = miss = kept = 0
        now = dt.datetime.utcnow()
        for c in rows:
            if c.geocode_precision in ("street", "manual"):
                kept += 1
                continue
            cc = (c.country or "").strip().upper()[:2]
            plz = _norm_plz(cc, c.postal_code)
            pt = geo.get((cc, plz))
            if not pt:
                miss += 1
                continue
            c.lat, c.lng = pt
            c.geocode_precision = "plz"
            c.geocoded_at = now
            hit += 1
        s.commit()
    return {"geocoded": hit, "no_match": miss, "kept_better": kept}


# --- Projekte (Baustellen) --------------------------------------------------
# Reihenfolge der Länder-Kandidaten, wenn die PLZ selbst keine Auskunft gibt.
# Nicht alphabetisch, sondern nach Bestand: DE ist bei Solarlux der Normalfall,
# alles andere die Ausnahme. Bei einer 5-stelligen PLZ, die es in DE und FR
# gibt, ist DE damit die Antwort — was in 24.480 von 36.905 Fällen ohnehin
# durch das Firmenland bestätigt wird.
_PLZ_KANDIDATEN: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"^\d{4}[A-Z]{2}$"), ("NL",)),
    (re.compile(r"^\d{5}$"), ("DE", "FR", "ES", "IT")),
    (re.compile(r"^\d{4}$"), ("AT", "DK", "CH", "BE", "NO")),
    (re.compile(r"^[A-Z]{1,2}\d"), ("GB", "IE")),
]


def _laender_kandidaten(raw_plz: str, firmenland: str | None) -> list[str]:
    """Welche Länder kommen für diese PLZ in Frage — bestes zuerst.

    Die Verkaufschance nennt ihr Land nicht (`country` ist in ALLEN 57.776
    Zeilen leer), also wird es erschlossen: zuerst das Land der Firma, an der
    die Chance hängt — die baut fast immer im eigenen Markt —, danach das
    Format der PLZ selbst. Geraten wird nie: passt keine Kombination auf eine
    Zeile in `plz_geo`, bleibt das Projekt ohne Koordinate und damit von der
    Karte weg."""
    out: list[str] = []
    if firmenland:
        out.append(firmenland.strip().upper()[:2])
    p = re.sub(r"[\s\-]", "", str(raw_plz)).upper()
    for muster, laender in _PLZ_KANDIDATEN:
        if muster.match(p):
            out.extend(laender)
            break
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _formatgruppe(raw_plz: str) -> frozenset[str]:
    """Die Länder, die dieses PLZ-Format teilen — leer, wenn keines passt.

    Vier Ziffern sehen in Österreich, Dänemark, der Schweiz, Belgien und
    Norwegen gleich aus; fünf Ziffern in Deutschland, Frankreich, Spanien und
    Italien. Innerhalb einer Gruppe sagen die Ziffern nichts, außerhalb sagen
    sie alles."""
    p = re.sub(r"[\s\-]", "", str(raw_plz)).upper()
    for muster, laender in _PLZ_KANDIDATEN:
        if muster.match(p):
            return frozenset(laender)
    return frozenset()


def _ort_schluessel(s: str | None) -> str:
    """Ortsname auf das Vergleichbare eindampfen: Kleinschreibung, Akzente und
    alles Nicht-Buchstabliche weg. "København Ø" und "Kobenhavn" sollen sich
    treffen, "Wien 12., Meidling" und "Wien" ebenfalls."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s)


def _land_per_ortsname(kandidaten: list[tuple[str, tuple]], stadt: str | None) -> str | None:
    """Wenn dieselbe PLZ in mehreren Ländern existiert: entscheidet der ORT.

    Eine 4-stellige PLZ gibt es in AT, DK, CH, BE und NO gleichermaßen — die
    Ziffern allein können das nicht auflösen, und die Reihenfolge der Kandidaten
    ist dann bloß eine Vermutung. Gemessen: 10.417 Objekte sind auf diese Weise
    mehrdeutig, bei 7.506 macht der Ortsname sie eindeutig, und 132 Pins lagen
    dadurch im falschen Land — ein Projekt in København stand in Österreich.
    Das ist die Sorte Fehler, die eine ganze Karte unglaubwürdig macht.

    Bleibt es mehrdeutig, wird NICHT geraten: der Aufrufer nimmt dann seine
    Reihenfolge, und die ist wenigstens nachvollziehbar begründet."""
    sn = _ort_schluessel(stadt)
    if not sn:
        return None
    passend = [cc for cc, (_la, _ln, ort) in kandidaten
               if (o := _ort_schluessel(ort))
               and (o.startswith(sn[:6]) or sn.startswith(o[:6]))]
    return passend[0] if len(passend) == 1 else None


def assign_project_centroids() -> dict:
    """Jedem Objekt (primäre Verkaufschance) eine Koordinate geben.

    Geokodiert wird die PRIMÄRE Verkaufschance, denn sie *ist* das Objekt --
    ihre Adresse ist die Bauadresse. Die Geschwister-VCs sind Angebote an
    verschiedene Firmen für dasselbe Gebäude und bekommen bewusst keinen
    eigenen Pin; sonst stünden am selben Haus acht Punkte übereinander.

    Wie `assign_plz_centroids` re-runnbar und additiv: eine bereits gesetzte
    'street'- oder 'manual'-Koordinate wird nie überschrieben."""
    from .models import Company, CrmOpportunity

    with SessionLocal() as s:
        geo = {(g.country, g.plz): (g.lat, g.lng, g.place)
               for g in s.scalars(select(PlzGeo))}
        land_der_firma = {
            (c.crm_id or "").lower(): c.country
            for c in s.execute(select(Company.crm_id, Company.country)
                               .where(Company.crm_id.is_not(None))).all()}
        rows = s.scalars(select(CrmOpportunity).where(
            CrmOpportunity.opportunity_guid == CrmOpportunity.project_id,
            CrmOpportunity.postal_code.is_not(None),
            CrmOpportunity.postal_code != "")).all()

        hit = miss = kept = mehrdeutig = 0
        for o in rows:
            if o.geocode_precision in ("street", "manual"):
                kept += 1
                continue
            firmenland = land_der_firma.get((o.parent_account_crm_id or "").lower())
            # ALLE passenden Länder sammeln statt beim ersten aufzuhören: erst
            # wenn man sie nebeneinander hat, kann der Ortsname entscheiden.
            treffer = []
            for cc in _laender_kandidaten(o.postal_code, firmenland):
                pt = geo.get((cc, _norm_plz(cc, o.postal_code)))
                if pt:
                    treffer.append((cc, pt))
            if not treffer:
                miss += 1
                continue
            if len(treffer) > 1:
                mehrdeutig += 1
            ort_sagt = (_land_per_ortsname(treffer, o.city)
                        if len(treffer) > 1 else None)
            gewaehlt = ort_sagt or treffer[0][0]

            # Wann darf das FORMAT das Firmenland überstimmen? Nur, wenn es
            # wirklich etwas weiß. "CT15 6DZ" ist eine britische Postleitzahl,
            # egal wo die Firma gemeldet ist — dort zu pinnen ist keine
            # Vermutung, sondern eine Tatsache (2.088 solcher Fälle).
            #
            # Steht das Firmenland dagegen SELBST in der Formatgruppe und
            # trifft trotzdem nicht, dann ist das Geschwisterland reine
            # Vermutung: eine dänische Firma mit vertippter 4-stelliger PLZ
            # landete so in Österreich, ein Projekt in Jakarta in Frankreich.
            # 159 Fälle — sie fallen lieber unter "ohne Bauadresse", wo sie
            # gezählt werden, als als stiller falscher Pin auf die Karte.
            fl = (firmenland or "").strip().upper()[:2]
            if fl and gewaehlt != fl and not ort_sagt \
                    and fl in _formatgruppe(o.postal_code):
                # Verwerfen heißt auch: eine FRÜHER geschriebene Zentroid-
                # Koordinate wieder wegnehmen. Sonst überlebt der falsche Pin
                # die Regel, die ihn verhindern soll — beim ersten Lauf stand
                # das dänische Præstevangen danach weiter in Österreich.
                if o.geocode_precision == "plz":
                    o.lat = o.lng = o.geocode_country = None
                    o.geocode_precision = None
                miss += 1
                continue

            lat, lng, _ort = dict(treffer)[gewaehlt]
            o.lat, o.lng = lat, lng
            o.geocode_precision = "plz"
            o.geocode_country = gewaehlt
            hit += 1
        s.commit()
    # Der Projekt-Cache hängt an einem Fingerabdruck aus Zeilenzahl und
    # `synced_at` — beide bewegt eine Geokodierung nicht. Ohne diesen Aufruf
    # zeigte die Karte bis zum nächsten Import 0 Pins, obwohl die Koordinaten
    # in der Datenbank stünden.
    from .insights import projekte
    projekte.invalidate_cache()
    return {"projects": len(rows), "geocoded": hit, "no_match": miss,
            "kept_better": kept, "mehrdeutig": mehrdeutig}


def project_pins(status: str | None = None, min_members: int = 1,
                 max_members: int | None = None, q: str | None = None,
                 min_value: float = 0.0, lost_reason: str | None = None,
                 limit: int = 60000) -> dict:
    """Pins für die Projektkarte — dieselben Objekte wie die Projektliste.

    Der Filter ist wörtlich derselbe (`projekte._kandidaten`), damit Liste und
    Karte nicht auseinanderlaufen können; die Karte zeigt davon die Teilmenge
    mit Koordinate. `ohne_koordinate` sagt, wie groß der Rest ist — eine Karte,
    die 70 % zeigt und 100 % suggeriert, wäre die stille Kappung, die wir uns
    an anderer Stelle schon eingefangen haben."""
    from .insights import projekte

    kandidaten = projekte.kandidaten(status=status, min_members=min_members,
                                     max_members=max_members, q=q,
                                     min_value=min_value, lost_reason=lost_reason)
    out, ohne = [], 0
    for rank, key, members, outcome in kandidaten:
        primary = next((m for m in members
                        if (m.opportunity_guid or "") == key), members[0])
        if primary.lat is None or primary.lng is None:
            ohne += 1
            continue
        if len(out) >= limit:
            ohne += 1
            continue
        out.append({
            "id": key,
            "name": primary.project_name or primary.name or "(ohne Namen)",
            "city": primary.city,
            "lat": round(primary.lat, 5), "lng": round(primary.lng, 5),
            "prec": primary.geocode_precision,
            "typ": outcome,
            "members": len(members),
            "value": round(rank, 2) or None,
        })
    return {"pins": out, "total": len(out), "ohne_koordinate": ohne}


def pins(filters: dict | None = None, country: str | None = None,
         limit: int = 60000) -> dict:
    # limit > Gesamtbestand (44.397 geokodiert, 2026-08) — eine Notbremse gegen
    # zukünftige Millionen-Importe, KEINE stille Kappung des heutigen Bestands.
    # Beim ersten Test stand sie auf 40.000 und schnitt 4.397 echte Pins ab,
    # während der Zähler "von 40.000" behauptete — genau die Sorte stiller
    # Deckel, die verboten ist.
    """Pins für die Karte — nur Firmen im Scope (Private Endkunden sind
    Privatadressen und erscheinen auf KEINER Karte), nur mit Koordinate.

    `filters` ist DASSELBE Filterobjekt wie im Firmen-Explorer
    (customers._apply_filters) — der Filter reist mit: was die Liste zeigt,
    zeigt die Karte, ohne zweite Filtersprache. `typ` färbt den Pin:
    kunde (kauft), architekt (plant), interessent (Rest)."""
    from . import scope
    from .customers import _apply_filters
    from .models import CrmOrderEvent

    with SessionLocal() as s:
        stmt = (select(Company.id, Company.name, Company.city, Company.lat,
                       Company.lng, Company.geocode_precision,
                       Company.identity_status, Company.segment,
                       Company.beleg_count)
                .where(scope.in_scope_clause(), Company.lat.is_not(None)))
        if filters:
            stmt = _apply_filters(stmt, filters)
        if country:
            stmt = stmt.where(func.upper(Company.country) == country.strip().upper())
        stmt = stmt.limit(limit)
        buyers = set(s.scalars(select(CrmOrderEvent.company_id).distinct()))
        out = []
        for cid, name, city, lat, lng, prec, ident, segment, beleg in s.execute(stmt):
            seg = (segment or "").lower()
            typ = ("kunde" if (cid in buyers or (beleg or 0) > 0)
                   else "architekt" if "architekt" in seg
                   else "interessent")
            out.append({"id": cid, "name": name, "city": city,
                        "lat": round(lat, 5), "lng": round(lng, 5),
                        "prec": prec, "ident": ident, "typ": typ})
    return {"pins": out, "total": len(out)}
