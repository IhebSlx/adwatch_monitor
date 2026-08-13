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
