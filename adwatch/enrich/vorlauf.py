"""Der Vorabtest: je WEBSITE entscheiden, ob sie tief gelesen wird.

IHEBS ENTWURF, UND ER IST BESSER ALS MEINER.
Mein erster Aufbau las jede Projektseite jeder Domain und filterte danach —
10.212 Domains × 98 Seiten, ein bis zwei Tage. Iheb: „for each suspect website
where spain is mentioned, you look at the sitemap and you decide with haiku
what to crawl, wouldn't that work?"

Es funktioniert, und die Zahlen sind eindeutig. Gemessen an einer Stichprobe
von 200 Domains aus dem breiten Bestand und an den 35 Büros, von denen der
Tiefenlauf bestätigt hat, dass sie in Spanien bauen:

    schlägt an bei              8 % des breiten Bestands
    Seiten je Domain            2,6  statt 98
    Tempo                       68 Domains/min bei 20 Arbeitern
    Trefferquote                97 % der bestätigten Büros

    Vorabtest über alle         ~2,5 Stunden, kostenlos
    danach tief, ~900 Domains   ~4,5 Stunden
                                ~7 Stunden statt 1–2 Tage

WARUM DIE ÜBERSICHTSSEITEN DAZUGEHÖREN.
Mit Startseite + Sitemap allein lag die Trefferquote bei 71 % — und sechs der
zehn verpassten Büros hatten überhaupt keine Sitemap (krekeler-architekten.de,
gmp-architekten.de, linear-architekt.de …). Dort hatte der Test nur die
Startseite. Nimmt man bis zu vier Projekt- und Referenzübersichten dazu, wo
die Projekttitel stehen, steigt die Quote auf 97 % — bei 2,3 Seiten je Domain.
Die eine, die dann noch fehlt, ist springtijarchitecten.nl.

WAS DER TEST NICHT LEISTET: entscheiden. Er sucht nur Gründe, hinzusehen. Ob
das Büro wirklich in Spanien baut, sagt erst der Tiefenlauf mit Haiku.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text as _sql

from ..db import SessionLocal
from . import spanienverdacht, tiefenlauf
from .laenderlauf import _BUDGET_ARCHITEKT

logger = logging.getLogger("adwatch.vorlauf")

ARBEITER = 20
MAX_UEBERSICHTEN = 4

_fortschritt = {"gesamt": 0, "fertig": 0, "verdacht": 0, "leer": 0,
                "unerreichbar": 0, "laeuft": False, "start": None}
_lock = threading.Lock()


def stand() -> dict:
    with _lock:
        d = dict(_fortschritt)
    if d["start"] and d["fertig"]:
        weg = (dt.datetime.now() - d["start"]).total_seconds()
        d["pro_minute"] = round(d["fertig"] / max(weg / 60, 0.01), 1)
        d["rest_minuten"] = round((d["gesamt"] - d["fertig"])
                                  / max(d["fertig"] / (weg / 60), 0.01))
    return d


def _tabelle(s) -> None:
    s.execute(_sql("""
        CREATE TABLE IF NOT EXISTS arch_web_vorab (
            domain TEXT PRIMARY KEY, verdacht INTEGER, gruende TEXT,
            seiten INTEGER, erreichbar INTEGER, geprueft_am TEXT)"""))


def eine_domain(dom: str) -> dict:
    """Startseite, dann die Übersichten, dann die Sitemap — bis etwas anspringt.

    Die Reihenfolge ist Absicht: der erste Treffer beendet die Suche. Ein
    Büro, dessen Startseite „Mallorca" sagt, kostet eine Seite.
    """
    from ..identity import website_source as ws

    gruende: list[str] = []
    seiten = 0
    got = tiefenlauf._startseite(dom)
    if not got:
        return {"domain": dom, "verdacht": False, "gruende": [], "seiten": 0,
                "erreichbar": False}
    seiten = 1
    gruende += spanienverdacht.gruende(
        ws._page_text(got["home_html"], limit=tiefenlauf._ZEICHEN_JE_SEITE),
        got["home_url"], got["home_html"])

    if not gruende:
        for url in ws._subpage_urls(got["home_url"], got["home_html"],
                                    max_pages=MAX_UEBERSICHTEN,
                                    budget=_BUDGET_ARCHITEKT)[:MAX_UEBERSICHTEN]:
            holen = ws._fetch_url(url, timeout=12)
            if not holen:
                continue
            seiten += 1
            gruende += spanienverdacht.gruende(
                ws._page_text(holen[0], limit=tiefenlauf._ZEICHEN_JE_SEITE),
                url, holen[0])
            if gruende:
                break

    if not gruende:
        urls = tiefenlauf._sitemap_alles(dom, grenze=4000)
        if urls:
            gruende += spanienverdacht.gruende("\n".join(urls), dom, "")

    return {"domain": dom, "verdacht": bool(gruende), "gruende": gruende[:8],
            "seiten": seiten, "erreichbar": True}


def grundgesamtheit(mit_spanischen: bool = True) -> list[str]:
    """Alle europäischen Architekturbüros mit Website.

    Spanische Büros sind jetzt DABEI: Iheb will sie über die Spalte
    „Hauptsitz" später selbst herausfiltern, und sie kosten nur 226 Domains
    von 10.212.
    """
    laender = ("'DE','AT','CH','NL','BE','LU','FR','IT','PT','GB','IE','DK',"
               "'SE','NO','FI','PL','CZ','SK','HU','RO','HR','SI','GR','LI',"
               "'EE','LV','LT','BG','RS','MT','CY','IS'")
    if mit_spanischen:
        laender += ",'ES'"
    with SessionLocal() as s:
        return [r[0] for r in s.execute(_sql(
            f"SELECT DISTINCT website_domain FROM companies "
            f"WHERE segment='Architekten' AND sub_segment='Architekturbüro' "
            f"AND duplicate_of IS NULL AND website_domain IS NOT NULL "
            f"AND website_domain <> '' AND country IN ({laender})"))]


def lauf(limit: int | None = None, arbeiter: int = ARBEITER,
         neu: bool = False, mit_spanischen: bool = True) -> dict:
    import json

    domains = sorted(grundgesamtheit(mit_spanischen))
    with SessionLocal() as s:
        _tabelle(s)
        s.commit()
        if not neu:
            fertig = {r[0] for r in s.execute(_sql(
                "SELECT domain FROM arch_web_vorab"))}
            domains = [d for d in domains if d not in fertig]
    if limit:
        domains = domains[:limit]

    with _lock:
        _fortschritt.update({"gesamt": len(domains), "fertig": 0, "verdacht": 0,
                             "leer": 0, "unerreichbar": 0, "laeuft": True,
                             "start": dt.datetime.now()})
    logger.info("Vorabtest startet: %d Domains, %d Arbeiter", len(domains), arbeiter)

    puffer: list[dict] = []

    def schreiben(satz: list[dict]) -> None:
        jetzt = dt.datetime.now().isoformat(timespec="seconds")
        with SessionLocal() as s:
            _tabelle(s)
            for d in satz:
                s.execute(_sql(
                    "INSERT INTO arch_web_vorab (domain, verdacht, gruende, "
                    "seiten, erreichbar, geprueft_am) VALUES (:d,:v,:g,:s,:e,:z) "
                    "ON CONFLICT(domain) DO UPDATE SET verdacht=:v, gruende=:g, "
                    "seiten=:s, erreichbar=:e, geprueft_am=:z"),
                    {"d": d["domain"], "v": int(d["verdacht"]),
                     "g": json.dumps(d["gruende"], ensure_ascii=False),
                     "s": d["seiten"], "e": int(d["erreichbar"]), "z": jetzt})
            s.commit()

    with ThreadPoolExecutor(max_workers=arbeiter) as pool:
        futures = {pool.submit(eine_domain, d): d for d in domains}
        for fut in as_completed(futures):
            try:
                d = fut.result()
            except Exception as e:                          # noqa: BLE001
                dom = futures[fut]
                logger.warning("vorab %s: %s", dom, e)
                d = {"domain": dom, "verdacht": False, "gruende": [],
                     "seiten": 0, "erreichbar": False}
            puffer.append(d)
            with _lock:
                _fortschritt["fertig"] += 1
                if not d["erreichbar"]:
                    _fortschritt["unerreichbar"] += 1
                elif d["verdacht"]:
                    _fortschritt["verdacht"] += 1
                else:
                    _fortschritt["leer"] += 1
            if len(puffer) >= 200:
                schreiben(puffer)
                puffer = []
    if puffer:
        schreiben(puffer)

    with _lock:
        _fortschritt["laeuft"] = False
        aus = dict(_fortschritt)
    logger.info("Vorabtest fertig: %s", aus)
    return aus


def verdachtsdomains() -> list[str]:
    """Die Domains, die der Vorabtest weitergereicht hat."""
    with SessionLocal() as s:
        _tabelle(s)
        return [r[0] for r in s.execute(_sql(
            "SELECT domain FROM arch_web_vorab WHERE verdacht = 1"))]
