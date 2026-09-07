"""Der Lauf: Website lesen, Tätigkeitsländer ableiten, in `companies` schreiben.

Kostet KEIN Geld. Gecrawlt wird mit dem eigenen Abrufer (robots.txt wird
beachtet, Pause zwischen den Seiten), ausgewertet wird von `laender.py` gegen
Tabellen, die schon in der Datenbank liegen. Kein Schlüssel, kein Modell, keine
Rechnung — deshalb darf er über die ganze Grundgesamtheit laufen und nicht nur
über eine Stichprobe.

DREI ENTSCHEIDUNGEN, DIE DEN LAUF PRÄGEN.

1. GRUPPIERT NACH DOMAIN, NICHT NACH ZEILE. 10.576 Büros teilen sich 10.212
   Websites — Drees & Sommer hat acht Zeilen und eine Seite. Jede Domain wird
   deshalb genau EINMAL gelesen und das Ergebnis auf alle ihre Zeilen
   geschrieben. Das spart nicht nur Zeit: „wo baut dieses Büro" ist eine
   Eigenschaft des BÜROS, nicht des Standorts Leipzig.

2. WIEDERAUFNEHMBAR. Wer schon ein `active_countries_at` trägt, wird
   übersprungen. Ein abgebrochener Lauf verliert also nichts, und ein zweiter
   Lauf kostet nur die Reste. `neu=True` erzwingt alles noch einmal.

3. DAS HEIMATLAND KOMMT AUS DEM CRM, NICHT VON DER SEITE. `country` der
   Firma ist gepflegt und verlässlich; es geht als Startwert in die Auswertung
   und dient zugleich als Stichentscheid bei mehrdeutigen Ortsnamen.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import func, select, text as _sql

from ..db import SessionLocal
from ..models import Company
from . import fetchpage, laender

logger = logging.getLogger("adwatch.laenderlauf")

# Acht gleichzeitige Abrufe. Die Grenze ist Höflichkeit, nicht Technik: es sind
# 10.212 verschiedene Hosts, also trifft es keinen einzelnen Server mehrfach,
# und `fetchpage` pausiert innerhalb einer Site ohnehin zwischen den Seiten.
ARBEITER = 8

_fortschritt = {"gesamt": 0, "fertig": 0, "gefunden": 0, "leer": 0,
                "fehler": 0, "laeuft": False, "start": None}
_lock = threading.Lock()


def stand() -> dict:
    """Wo steht der Lauf gerade — für die Oberfläche und fürs Log."""
    with _lock:
        d = dict(_fortschritt)
    if d["start"] and d["fertig"]:
        vergangen = (dt.datetime.now() - d["start"]).total_seconds()
        d["pro_minute"] = round(d["fertig"] / max(vergangen / 60, 0.01), 1)
        rest = d["gesamt"] - d["fertig"]
        d["rest_minuten"] = round(rest / max(d["fertig"] / (vergangen / 60), 0.01))
    return d


def _grundgesamtheit(s, neu: bool):
    """Segment = Architekten, Untersegment = Architekturbüro, Vertriebsweg = alle.

    Genau der Filter, den Iheb in der Oberfläche benutzt hat. Dubletten
    (`duplicate_of`) fallen heraus — sie bekommen das Ergebnis ihres Originals
    ohnehin über die Domain-Gruppe.
    """
    stmt = select(Company.id, Company.website_domain, Company.country).where(
        Company.segment == "Architekten",
        Company.sub_segment == "Architekturbüro",
        Company.duplicate_of.is_(None),
        Company.website_domain.is_not(None),
        Company.website_domain != "")
    if not neu:
        stmt = stmt.where(Company.active_countries_at.is_(None))
    return s.execute(stmt).all()


def lauf(neu: bool = False, limit: int | None = None,
         arbeiter: int = ARBEITER) -> dict:
    """Den ganzen Bestand durchgehen. Blockiert — für den Hintergrund gedacht."""
    with SessionLocal() as s:
        zeilen = _grundgesamtheit(s, neu)

    nach_domain: dict[str, list] = defaultdict(list)
    for cid, dom, land in zeilen:
        nach_domain[(dom or "").strip().lower()].append((cid, land))
    domains = sorted(nach_domain)
    if limit:
        domains = domains[:limit]

    with _lock:
        _fortschritt.update({"gesamt": len(domains), "fertig": 0, "gefunden": 0,
                             "leer": 0, "fehler": 0, "laeuft": True,
                             "start": dt.datetime.now()})
    logger.info("Länderlauf startet: %d Domains, %d Zeilen, %d Arbeiter",
                len(domains), sum(len(v) for v in nach_domain.values()), arbeiter)

    def eine(dom: str) -> tuple[str, dict | None, str | None]:
        try:
            bund = fetchpage.page_bundle(dom)
        except Exception as e:                      # noqa: BLE001
            return dom, None, f"abruf: {type(e).__name__}"
        if not bund or not (bund.get("text") or "").strip():
            return dom, None, None                  # erreichbar, aber nichts zu lesen
        # Heimatland: das häufigste Land der Zeilen zu dieser Domain
        laender_der_zeilen = [l for _, l in nach_domain[dom] if l]
        heimat = max(set(laender_der_zeilen), key=laender_der_zeilen.count) \
            if laender_der_zeilen else None
        tld = dom.rsplit(".", 1)[-1] if "." in dom else None
        try:
            return dom, laender.laender_aus_text(bund["text"], heimat=heimat,
                                                 tld=tld), None
        except Exception as e:                      # noqa: BLE001
            return dom, None, f"auswertung: {type(e).__name__}"

    ergebnisse: dict[str, dict] = {}
    fehlerliste: list[str] = []
    with ThreadPoolExecutor(max_workers=arbeiter) as pool:
        futures = {pool.submit(eine, d): d for d in domains}
        for fut in as_completed(futures):
            dom, res, fehler = fut.result()
            with _lock:
                _fortschritt["fertig"] += 1
                if fehler:
                    _fortschritt["fehler"] += 1
                elif res and res["laender"]:
                    _fortschritt["gefunden"] += 1
                else:
                    _fortschritt["leer"] += 1
            if fehler:
                if len(fehlerliste) < 40:
                    fehlerliste.append(f"{dom}: {fehler}")
                continue
            if res:
                ergebnisse[dom] = res
            # in Blöcken schreiben, damit ein Abbruch nicht alles verliert
            if len(ergebnisse) >= 200:
                _schreiben(ergebnisse, nach_domain)
                ergebnisse = {}
    if ergebnisse:
        _schreiben(ergebnisse, nach_domain)

    with _lock:
        _fortschritt["laeuft"] = False
        aus = dict(_fortschritt)
    aus["fehler_beispiele"] = fehlerliste[:20]
    logger.info("Länderlauf fertig: %s", {k: v for k, v in aus.items()
                                          if k != "fehler_beispiele"})
    return aus


def _schreiben(ergebnisse: dict[str, dict], nach_domain: dict) -> None:
    """Ergebnis einer Domain auf ALLE ihre Firmenzeilen schreiben."""
    jetzt = dt.datetime.now()
    with SessionLocal() as s:
        for dom, res in ergebnisse.items():
            sicher = [k for k, v in res["laender"].items()
                      if v["sicherheit"] == "sicher"]
            alle = {k: v["sicherheit"] for k, v in res["laender"].items()}
            belege = {k: v["belege"] for k, v in res["laender"].items()}
            if res.get("unsicher"):
                belege["_unsicher"] = res["unsicher"]
            for cid, _ in nach_domain.get(dom, []):
                s.execute(_sql(
                    "UPDATE companies SET active_countries = :a, "
                    "active_countries_all = :b, active_countries_evidence = :c, "
                    "active_countries_at = :d WHERE id = :i"),
                    {"a": _json(sicher), "b": _json(alle), "c": _json(belege),
                     "d": jetzt, "i": cid})
        s.commit()


def _json(x):
    import json
    return json.dumps(x, ensure_ascii=False)


def uebersicht() -> dict:
    """Was hat der Lauf bisher ergeben — je Land, für die Architekturbüros."""
    zaehler: dict[str, int] = defaultdict(int)
    moeglich: dict[str, int] = defaultdict(int)
    with SessionLocal() as s:
        gepruefte = s.scalar(select(func.count(Company.id)).where(
            Company.segment == "Architekten",
            Company.sub_segment == "Architekturbüro",
            Company.duplicate_of.is_(None),
            Company.active_countries_at.is_not(None)))
        for (alle,) in s.execute(select(Company.active_countries_all).where(
                Company.segment == "Architekten",
                Company.sub_segment == "Architekturbüro",
                Company.duplicate_of.is_(None),
                Company.active_countries_all.is_not(None))).all():
            for land, stufe in (alle or {}).items():
                if stufe == "sicher":
                    zaehler[land] += 1
                elif stufe == "moeglich":
                    moeglich[land] += 1
    return {"geprueft": gepruefte,
            "sicher": dict(sorted(zaehler.items(), key=lambda x: -x[1])),
            "moeglich": dict(sorted(moeglich.items(), key=lambda x: -x[1]))}
