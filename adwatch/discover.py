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


def persist(rows: list[dict], lead_source: str, country: str = "DE") -> dict:
    """Neue Kandidaten als Firmen anlegen — mit sichtbarer Herkunft.

    Dieselbe Konvention wie die spanische Marktliste (`lead_source =
    'marktanalyse_es_2026_08'`, kein `crm_id`): so bleibt jederzeit
    unterscheidbar, was aus dem CRM stammt und was wir selbst gefunden haben.
    `monitored = False`, damit kein Anzeigen-Abruf automatisch Geld ausgibt.

    Angelegt wird NUR, was noch nicht bekannt ist (`is_new`) und noch keine
    Firma mit derselben Domain hat — der Abgleich läuft ein zweites Mal gegen
    die Datenbank, weil zwischen Suche und Anlegen Zeit vergangen sein kann.

    Der Name aus dem Suchtreffer-Titel ist ein VORSCHLAG, keine belegte
    Firmierung — die Anreicherung ersetzt ihn später aus dem Impressum. Er ist
    außerdem oft gar kein Name: der erste Versuch scheiterte an der
    UNIQUE-Bedingung auf `companies.name`, weil eine Seite schlicht
    „Wintergärten" im Titel trug und eine Firma dieses Namens bereits existierte.
    Deshalb gilt: ein Titel wird nur übernommen, wenn er lang genug und nicht
    bloß ein Gattungswort ist, und die Domain hängt sich an, sobald der Name
    schon vergeben wäre. Die Domain ist hier die einzige verlässlich eindeutige
    Angabe.
    """
    made, skipped = [], 0
    with SessionLocal() as s:
        have = {registrable(normalize_domain(d) or d)
                for (d,) in s.execute(select(Company.website_domain))
                if d}
        names = {(n or "").strip().lower() for (n,) in s.execute(select(Company.name))}
        for r in rows:
            if not r.get("is_new"):
                skipped += 1
                continue
            reg = registrable(r["domain"]) or r["domain"]
            if not reg or reg in have or _is_directory(reg):
                skipped += 1
                continue
            name = _proposed_name(r.get("title"), reg, names)
            c = Company(name=name[:200], website_domain=reg,
                        city=r.get("city"), country=country.upper(),
                        lead_source=lead_source, monitored=False,
                        segment=None, sub_segment=None)
            s.add(c)
            have.add(reg)
            names.add(name.strip().lower())
            made.append(c)
        s.commit()
        out = [{"id": c.id, "name": c.name, "domain": c.website_domain} for c in made]
    return {"created": len(out), "skipped": skipped, "lead_source": lead_source,
            "companies": out}


# Titel, die keine Firmierung sind, sondern das Gewerk oder ein Werbespruch.
_GENERIC_TITLE = re.compile(
    r"^(fenster|fensterbau|glaserei|glas|wintergarten|wintergärten|wintergaerten|"
    r"tischlerei|metallbau|schreinerei|haustüren|türen|startseite|home|"
    r"willkommen|impressum|kontakt)\b", re.I)


def _proposed_name(title: str | None, domain: str, taken: set[str]) -> str:
    """Ein Anzeigename, der eindeutig ist und nicht mehr behauptet, als er weiß.

    Reihenfolge: brauchbarer Titel → Titel plus Domain (wenn der Name schon
    vergeben ist) → nur die Domain. Der Zusatz ist kein Schönheitsfehler: er
    macht sichtbar, dass die Firmierung noch nicht belegt ist.
    """
    t = (title or "").split("|")[0].split("—")[0].split(" - ")[0].strip(" -–·,")
    if len(t) < 4 or _GENERIC_TITLE.match(t):
        t = ""
    if t and t.strip().lower() not in taken:
        return t
    combined = f"{t} ({domain})" if t else domain
    if combined.strip().lower() not in taken:
        return combined
    return f"{domain} #{abs(hash(domain)) % 10000}"


# ---------------------------------------------------------------------------
# Bewertung entdeckter Firmen — nur mit dem, was ein Fremder hergibt
# ---------------------------------------------------------------------------
# Gemessen 2026-08-18: Website-Merkmale ALLEIN erreichen AUC 0,595, die
# CRM-Stammdaten 0,583. Für eine entdeckte Firma gibt es die Stammdaten nicht
# (kein Segment, kein Untersegment, keine Vertriebszuordnung) — also wird auf
# genau dem bewertet, was auch bei einem Fremden vorliegt.
#
# Trainiert wird auf der Stichprobe des Anreicherungs-Experiments (Job 58):
# 600 deutsche Händler, 300 Käufer / 300 Nicht-Käufer, ALLE angereichert. Das
# ist der einzige deutsche Datensatz ohne Anreicherungs-Verzerrung — überall
# sonst wurde fast nur angereichert, wer ohnehin kauft (Quotient 200×), und ein
# darauf trainiertes Modell würde „ist angereichert" lernen statt „kauft".
_SCORE_FEATURES = ("hat_website", "produkte", "wettbewerber", "gegruendet",
                   "eigene_fertigung", "montiert", "showroom", "textlaenge")


def _website_features(fields: dict | None, status: str | None) -> dict:
    f = fields or {}
    return {
        "hat_website": 1.0 if status in ("enriched", "needs_review") else 0.0,
        "produkte": float(len(f.get("products") or [])),
        "wettbewerber": float(len(f.get("competitor_brands") or [])),
        "gegruendet": float(f.get("founded_year") or 0),
        "eigene_fertigung": 1.0 if f.get("own_fabrication") else 0.0,
        "montiert": 1.0 if f.get("installs") else 0.0,
        "showroom": 1.0 if f.get("has_showroom") else 0.0,
        "textlaenge": float(len(f.get("description_de") or "")
                            + len(f.get("assessment_de") or "")),
    }


def score_discovered(lead_source: str, training_ids: list[int] | None = None) -> dict:
    """Entdeckte Firmen gegen die Gewinner bewerten.

    Bewusst eine logistische Regression auf acht Merkmalen, keine Bäume: bei
    dieser Merkmalszahl lag der Vorsprung von Gradient Boosting in der Messung
    bei ±0,01, und die Koeffizienten sind einzeln nachvollziehbar. Wer eine
    Reihenfolge nicht bestreiten kann, benutzt sie nicht.

    Rückgabe enthält ausdrücklich die gemessene Güte (AUC 0,595) — eine
    Rangfolge ohne ihre Trennschärfe daneben lädt dazu ein, sie für stärker zu
    halten, als sie ist.
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    from .models import CompanyEnrichment, CrmOrderEvent

    with SessionLocal() as s:
        if training_ids is None:
            raise ValueError("training_ids fehlen — ohne unverzerrte "
                             "Trainingsmenge ist die Bewertung wertlos.")
        enr = {cid: (fields, status) for cid, fields, status in s.execute(
            select(CompanyEnrichment.company_id, CompanyEnrichment.fields,
                   CompanyEnrichment.status))}
        buyers = set(s.scalars(
            select(CrmOrderEvent.company_id).distinct()
            .where(CrmOrderEvent.amount > 0)))
        cand = list(s.scalars(select(Company)
                              .where(Company.lead_source == lead_source)))

        X, y = [], []
        for cid in training_ids:
            fields, status = enr.get(cid, (None, None))
            X.append([_website_features(fields, status)[k] for k in _SCORE_FEATURES])
            y.append(1 if cid in buyers else 0)
        if len(set(y)) < 2:
            raise ValueError("Trainingsmenge enthält nur eine Klasse.")

        sc = StandardScaler().fit(X)
        model = LogisticRegression(max_iter=3000, C=0.5,
                                   class_weight="balanced").fit(sc.transform(X), y)

        rows = []
        for c in cand:
            fields, status = enr.get(c.id, (None, None))
            feats = _website_features(fields, status)
            p = float(model.predict_proba(
                sc.transform([[feats[k] for k in _SCORE_FEATURES]]))[0, 1])
            rows.append({"company_id": c.id, "name": c.name,
                         "domain": c.website_domain, "city": c.city,
                         "score": round(p, 4), "angereichert": status,
                         "merkmale": {k: feats[k] for k in _SCORE_FEATURES}})
    rows.sort(key=lambda r: -r["score"])
    coef = dict(zip(_SCORE_FEATURES, (round(float(v), 3) for v in model.coef_[0])))
    return {"guete_auc": 0.595,
            "hinweis": "Nur Website-Merkmale — dieselbe Trennschärfe wie die "
                       "CRM-Stammdaten (0,583), aber bei Fremden die einzige "
                       "verfügbare. Vorsortierung, keine belastbare Rangfolge.",
            "trainiert_auf": len(y), "davon_kaeufer": int(sum(y)),
            "koeffizienten": coef, "n": len(rows), "rows": rows}
