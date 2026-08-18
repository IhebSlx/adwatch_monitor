"""Kandidaten-Beschaffung — Firmen finden, die wir noch NICHT kennen.

Der Engpass der Zwillingssuche ist nicht das Bewerten, sondern das FINDEN.
Gemessen 2026-08-18: Website-Merkmale allein erreichen AUC 0,595 und damit
etwas mehr als die CRM-Stammdaten (0,583). Einen Fremden zu bewerten ist also
möglich. Nur muss man ihn erst einmal haben.

Warum Suchmaschine statt Wettbewerber-Verzeichnis: geprüft wurden Schüco,
Technal und Cortizo. Cortizo verbietet sein Installateursverzeichnis
ausdrücklich in der robots.txt (`/instaladores/desplegar/`), Schücos
Partnersuche ist ein JavaScript-Formular hinter einer nicht dokumentierten
Schnittstelle. Vor allem aber: ein Verzeichnis liefert nur, was ein
Wettbewerber gerade veröffentlicht, und ist morgen weg. Die Suche skaliert
über Gewerke und Regionen und benutzt Infrastruktur, die ohnehin bezahlt ist.

DIE ENTSCHEIDENDE MESSUNG ist nicht, wie viele Firmen wir finden, sondern wie
viele davon NEU sind. Ein Kanal, der zu 90 % Bekanntes liefert, ist kein Kanal.
Deshalb wird gegen den Bestand abgeglichen — und zwar auf ZWEI Wegen: über die
Domain und über Name+Ort. Nur über die Domain zu prüfen würde „neu"
systematisch überschätzen, weil bloß rund die Hälfte unserer Händler überhaupt
eine Domain hinterlegt hat.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select

from . import config, scope
from .db import SessionLocal
from .enrich.domains import normalize_domain, registrable
from .enrich.website_finder import _is_directory, _run_query
from .models import Company

logger = logging.getLogger("adwatch.discover")

# Gewerke, nach denen gesucht wird — abgeleitet aus den GEMESSENEN Kaufquoten
# des Kalt-ICP (profiles.cold_icp), nicht aus dem Bauchgefühl:
# Fensterbau 15,4 % · Glaser 14,9 % · Tischler 13,9 % · Metallbau 13,6 %
# gegen Baustoffhandel 6,3 %. Wonach man sucht, ist bereits eine Auswahl —
# darum stammt sie aus der Messung.
TRADES: dict[str, str] = {
    "Fensterbau": "Fensterbau",
    "Glaserei": "Glaser",
    "Wintergartenbau": "Wintergartenbau",
    "Metallbau": "Metallbau-Schlosser",
    "Tischlerei": "Tischler-Schreiner-Zimmerer",
}

_LEGAL = re.compile(r"\b(gmbh|mbh|ag|kg|ohg|e\.?k\.?|gbr|co|ug|se|s\.?l\.?|sarl)\b", re.I)


def _norm_name(s: str | None) -> str:
    """Vergleichsform eines Firmennamens: ohne Rechtsform, ohne Sonderzeichen."""
    if not s:
        return ""
    x = _LEGAL.sub(" ", s.lower())
    x = x.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", x).strip()


def _known_index() -> tuple[dict[str, int], dict[str, list[tuple[str, int]]]]:
    """Bestand als Nachschlagewerk: Domain -> id, und Ort -> [(Name, id)]."""
    by_domain: dict[str, int] = {}
    by_city: dict[str, list[tuple[str, int]]] = {}
    with SessionLocal() as s:
        for cid, name, dom, city in s.execute(
                select(Company.id, Company.name, Company.website_domain, Company.city)):
            if dom:
                reg = registrable(normalize_domain(dom) or dom)
                if reg:
                    by_domain.setdefault(reg, cid)
            key = _norm_name(city)
            if key:
                by_city.setdefault(key, []).append((_norm_name(name), cid))
    return by_domain, by_city


def _match_known(cand: dict, by_domain: dict, by_city: dict) -> tuple[int | None, str]:
    """Kennen wir die schon? Domain zuerst (hart), dann Name+Ort (weich).

    Der zweite Weg ist nötig, weil nur etwa die Hälfte unserer Händler eine
    Domain hinterlegt hat — ohne ihn wäre jede zweite bekannte Firma als
    'neu' gezählt worden und das Ergebnis des Versuchs wertlos.
    """
    reg = registrable(cand["domain"]) or cand["domain"]
    if reg in by_domain:
        return by_domain[reg], "domain"
    title = _norm_name(cand.get("title"))
    if title:
        for city_key, rows in by_city.items():
            if city_key and city_key in title:
                for nm, cid in rows:
                    if nm and len(nm) > 6 and (nm in title or title.startswith(nm[:12])):
                        return cid, "name_ort"
    return None, ""


def discover(cities: list[str], trades: list[str] | None = None,
             country: str = "DE", per_query: int = 10) -> dict:
    """Für jede Kombination Gewerk × Ort eine Suche, Ergebnisse gegen den
    Bestand abgeglichen. Verändert NICHTS in der Datenbank — dieser Schritt
    beantwortet nur die Frage, ob der Kanal etwas hergibt."""
    if not config.SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY fehlt — ohne Suchschlüssel keine Beschaffung.")
    trades = trades or list(TRADES)
    by_domain, by_city = _known_index()
    seen: set[str] = set()
    rows: list[dict] = []
    queries = 0

    for trade in trades:
        for city in cities:
            q = f"{trade} {city}"
            try:
                hits = _run_query(q, country, per_query, seen)
            except Exception as exc:  # noqa: BLE001 — eine Abfrage darf den Lauf nicht kippen
                logger.warning("Suche fehlgeschlagen (%s): %s", q, exc)
                continue
            queries += 1
            for h in hits:
                cid, how = _match_known(h, by_domain, by_city)
                rows.append({**h, "trade": trade, "city": city, "query": q,
                             "known_company_id": cid, "matched_by": how,
                             "is_new": cid is None})

    new = [r for r in rows if r["is_new"]]
    return {
        "queries": queries, "found": len(rows),
        "known": len(rows) - len(new), "new": len(new),
        "new_share": (len(new) / len(rows)) if rows else 0.0,
        "matched_by_domain": sum(1 for r in rows if r["matched_by"] == "domain"),
        "matched_by_name": sum(1 for r in rows if r["matched_by"] == "name_ort"),
        "rows": rows,
    }
