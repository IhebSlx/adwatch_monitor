"""Fragen in Alltagssprache über den gesamten Datenbestand — der Agent.

DIE KOSTENARCHITEKTUR IST DER PUNKT (Ihebs Entwurf, 2026-08-20): das
Sprachmodell macht nie die Arbeit, es entscheidet nur, WELCHES Werkzeug die
Arbeit macht. Python scannt die 47.770 Firmen in Millisekunden und für null
Token; das Modell liest nur die Frage und das eingeengte Ergebnis. Eine
typische Frage kostet damit Cents, nicht Euros — die 40.000 Datenpunkte
betreten den Kontext nie.

DIE FALLEN LEBEN IN DEN WERKZEUGEN, NICHT IM AGENTEN. Alles, was dieses
Projekt teuer gelernt hat, ist hier einkodiert, damit das Modell es nicht
umgehen KANN statt es nicht umgehen SOLL:

* Nur Lesen. Das SQL-Werkzeug öffnet die Datenbank im Read-only-Modus und
  nimmt ausschließlich SELECT/WITH an — ein Schreibversuch scheitert an der
  Verbindung selbst, nicht an einer Bitte im Prompt.
* Belege sind ANGEBOTE, keine Rechnungen. Steht im Systemprompt UND in der
  Werkzeugbeschreibung, weil eine Regel an einer Stelle vergessen wird.
* Private Endkunden und Konzern-Töchter fliegen in den kuratierten Werkzeugen
  automatisch raus (scope) — für rohes SQL erinnert die Beschreibung daran.

Jede Antwort trägt ihren Beleg: welche Werkzeuge liefen, mit welchen
Parametern, wie viele Token, was es gekostet hat. Eine Zahl ohne Herkunft ist
hier keine Antwort.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import time

from sqlalchemy import func, select

from . import config
from .db import SessionLocal
from .models import Company, CompanyEnrichment, CrmEmail, CrmOpportunity, CrmOrderEvent

log = logging.getLogger("adwatch.fragen")

MAX_RUNDEN = 6           # Werkzeug-Schleifen je Frage — genug für "suche, dann vertiefe"
MAX_ERGEBNIS_ZEICHEN = 6000   # je Werkzeugergebnis; mehr braucht keine Antwort
SQL_MAX_ZEILEN = 200

# USD je Million Token (Eingabe, Ausgabe) — Teilstring-Match auf den Modellnamen.
# Bewusst großzügig gepflegt statt exakt: die Zahl dient der Ehrlichkeit der
# Kostenanzeige, nicht der Buchhaltung.
_PREISE = [("haiku", (1.0, 5.0)), ("sonnet", (3.0, 15.0)), ("opus", (15.0, 75.0))]


def _kosten_usd(model: str, tin: int, tout: int) -> float:
    for k, (i, o) in _PREISE:
        if k in (model or ""):
            return round((tin * i + tout * o) / 1e6, 4)
    return 0.0


def _clip(text: str, limit: int = MAX_ERGEBNIS_ZEICHEN) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [gekürzt, {len(text):,} Zeichen insgesamt]"


def _j(obj) -> str:
    return _clip(json.dumps(obj, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Werkzeuge
# ---------------------------------------------------------------------------

def _db_pfad() -> str:
    url = config.DB_URL
    if not url.startswith("sqlite:///"):
        raise RuntimeError("SQL-Werkzeug unterstützt nur die lokale SQLite-Datenbank.")
    return url[len("sqlite:///"):]


_SQL_VERBOTEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b", re.I)


def w_sql(sql: str) -> str:
    """SELECT gegen die lokale Datenbank — read-only erzwungen, nicht erbeten."""
    s = (sql or "").strip().rstrip(";")
    if ";" in s:
        raise ValueError("Nur EINE Anweisung je Aufruf.")
    if not re.match(r"^\s*(select|with)\b", s, re.I):
        raise ValueError("Nur SELECT (oder WITH … SELECT) ist erlaubt.")
    if _SQL_VERBOTEN.search(s):
        raise ValueError("Anweisung enthält ein nicht erlaubtes Schlüsselwort.")
    if not re.search(r"\blimit\b", s, re.I):
        s += f" LIMIT {SQL_MAX_ZEILEN}"

    # mode=ro: die VERBINDUNG kann nicht schreiben — stärker als jede Prüfung oben
    conn = sqlite3.connect(f"file:{_db_pfad()}?mode=ro", uri=True, timeout=10)
    # Notbremse gegen versehentliche Vollscans mit Kreuzprodukt: nach ~50 Mio
    # VM-Schritten wird abgebrochen statt den Server minutenlang zu blockieren.
    steps = {"n": 0}

    def _bremse():
        steps["n"] += 1
        return 1 if steps["n"] > 5000 else 0
    conn.set_progress_handler(_bremse, 10_000)
    try:
        cur = conn.execute(s)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(SQL_MAX_ZEILEN)
    finally:
        conn.close()
    return _j({"spalten": cols, "zeilen": [list(r) for r in rows],
               "hinweis": None if len(rows) < SQL_MAX_ZEILEN else
               f"auf {SQL_MAX_ZEILEN} Zeilen gekappt — enger filtern oder aggregieren"})


def w_datenbestand() -> str:
    """Was es gibt, wie es heißt, und die Regeln, ohne die jede Zahl falsch wird."""
    with SessionLocal() as s:
        zaehl = {t: s.execute(select(func.count()).select_from(m)).scalar()
                 for t, m in (("companies", Company), ("crm_opportunities", CrmOpportunity),
                              ("crm_order_events", CrmOrderEvent), ("crm_emails", CrmEmail))}
    return _j({
        "tabellen": {
            "companies": {
                "zeilen": zaehl["companies"],
                "wichtige_spalten": ["id", "name", "city", "postal_code", "country",
                                     "segment", "sub_segment", "sales_channel", "kv",
                                     "website_domain", "resolution_status", "customer_state",
                                     "beleg_sum", "beleg_count", "crm_id", "sap_number",
                                     "revenue_y0..revenue_y4", "is_intercompany",
                                     "lead_source", "crm_created_on", "lat", "lng"]},
            "crm_opportunities": {
                "zeilen": zaehl["crm_opportunities"],
                "wichtige_spalten": ["crm_id", "parent_account_crm_id (-> companies.crm_id)",
                                     "project_id", "name", "state (gewonnen/verloren/offen)",
                                     "order_value", "estimated_value", "created_on",
                                     "closed_on", "sales_channel", "lost_reason",
                                     "postal_code", "country", "architect_crm_id"]},
            "crm_order_events": {
                "zeilen": zaehl["crm_order_events"],
                "wichtige_spalten": ["company_id (-> companies.id)", "order_date",
                                     "amount", "beleg_count"]},
            "crm_emails": {
                "zeilen": zaehl["crm_emails"],
                "wichtige_spalten": ["company_id", "regarding_id", "regarding_type",
                                     "created_on", "direction (eingehend/ausgehend)",
                                     "subject", "body_text"]},
            "weitere": ["crm_leads (236k, Vorstufe zur Firma)",
                        "crm_opportunity_products / crm_company_products (Produktzeilen)",
                        "target_lists / target_list_entries (Arbeitslisten mit Kontrollarm)",
                        "ads / weekly_company_metrics (Anzeigen)",
                        "company_enrichment (Website-Fakten)"],
        },
        "regeln": [
            "crm_order_events sind ANGEBOTE (Belege), keine Rechnungen. amount>0 = "
            "substanzielles Angebot erhalten, NICHT bezahlt. Echte Umsätze gibt es nicht.",
            "amount=0 (Garantie/Muster) zählt nie als Nachfrage.",
            "Bei Firmen-Auswertungen ausschließen: segment='Private Endkunden' und "
            "is_intercompany=1 — sonst sind die Zahlen wertlos.",
            "Das Segment 'Architekten' fragt praktisch nie an (sie planen, kaufen nicht). "
            "Basisraten immer je Population nennen, nie gemischt.",
            "order_value auf Verkaufschancen ist ein ANGEBOTSWERT bei Gewinn, keine Rechnung.",
        ],
    })


def w_firma_suchen(text: str, limit: int = 10) -> str:
    """Firmen per Namensteil/Ort/Domain finden — liefert IDs für die anderen Werkzeuge."""
    t = f"%{(text or '').strip()}%"
    with SessionLocal() as s:
        rows = s.execute(
            select(Company.id, Company.name, Company.city, Company.country,
                   Company.segment, Company.sub_segment, Company.website_domain)
            .where(Company.name.ilike(t) | Company.city.ilike(t)
                   | Company.website_domain.ilike(t))
            .limit(max(1, min(limit, 25)))).all()
    return _j([{"id": r[0], "name": r[1], "ort": r[2], "land": r[3],
                "segment": r[4], "branche": r[5], "website": r[6]} for r in rows]
              or {"hinweis": f"keine Firma passt auf '{text}'"})


def w_firma_dossier(company_id: int) -> str:
    """Alles Wesentliche zu EINER Firma, aus allen Tabellen zusammengetragen."""
    from . import crm_emails as ce
    with SessionLocal() as s:
        c = s.get(Company, int(company_id))
        if not c:
            return _j({"fehler": f"keine Firma mit id {company_id}"})
        ang = s.execute(
            select(func.count(), func.sum(CrmOrderEvent.amount),
                   func.min(CrmOrderEvent.order_date), func.max(CrmOrderEvent.order_date))
            .where(CrmOrderEvent.company_id == c.id, CrmOrderEvent.amount > 0)).one()
        vc = {st: n for st, n in s.execute(
            select(CrmOpportunity.state, func.count())
            .where(CrmOpportunity.parent_account_crm_id == c.crm_id)
            .group_by(CrmOpportunity.state))} if c.crm_id else {}
        enr = s.scalars(select(CompanyEnrichment)
                        .where(CompanyEnrichment.company_id == c.id)
                        .order_by(CompanyEnrichment.id.desc())).first()
    mails = (ce.features([c.id]) or {}).get(c.id)
    return _j({
        "id": c.id, "name": c.name, "ort": c.city, "plz": c.postal_code,
        "land": c.country, "segment": c.segment, "branche": c.sub_segment,
        "vertriebsweg": c.sales_channel, "kv": c.kv,
        "website": c.website_domain, "identitaet": c.resolution_status,
        "kundenstatus": c.customer_state, "im_crm_seit": c.crm_created_on,
        "angebote": {"anzahl": ang[0], "volumen_eur": round(ang[1] or 0),
                     "erstes": ang[2], "letztes": ang[3],
                     "hinweis": "Angebote, keine Rechnungen"},
        "verkaufschancen": vc or None,
        "korrespondenz": mails,
        "website_fakten": (json.loads(enr.fields) if enr and isinstance(enr.fields, str)
                           else (enr.fields if enr else None)),
    })


def w_firma_mails(company_id: int, limit: int = 8) -> str:
    """Die jüngste Korrespondenz einer Firma — Betreff und Anfang, nicht alles."""
    with SessionLocal() as s:
        rows = s.execute(
            select(CrmEmail.created_on, CrmEmail.direction, CrmEmail.subject,
                   CrmEmail.body_text)
            .where(CrmEmail.company_id == int(company_id))
            .order_by(CrmEmail.created_on.desc())
            .limit(max(1, min(limit, 20)))).all()
    return _j([{"datum": r[0], "richtung": r[1], "betreff": r[2],
                "anfang": (r[3] or "")[:280]} for r in rows]
              or {"hinweis": "keine Korrespondenz zu dieser Firma im Bestand"})


def w_profil_rangliste(profil: str, land: str | None = None, top: int = 15) -> str:
    """Die gemessenen Profile: wer/welches Projekt ist der Zeit wert."""
    from .insights import ipp, profiles
    top = max(1, min(int(top or 15), 50))
    p = (profil or "").strip().lower()
    if p in ("projekt", "ipp"):
        t = ipp.triage(limit=top)
        return _j({"profil": "Projekt (IPP)", "guete": "Lift 13,7x (out-of-time)",
                   "offene_projekte": t.get("open_total"), "rows": t.get("rows")})
    if p in ("trichter", "funnel"):
        d = profiles.funnel_triage(limit=top, country=land)
        return _j({"profil": "Trichter", "guete": d.get("quality"),
                   "basisrate": d.get("base_rate"), "rows": d.get("rows")})
    if p in ("bestand", "fortsetzung", "churn"):
        d = profiles.continuation(limit=top, country=land)
        return _j({"profil": "Bestand (riskanteste zuerst)", "guete": d.get("quality"),
                   "hinweis": d.get("hinweis"), "rows": d.get("at_risk")})
    if p in ("kalt", "kaltakquise", "icp"):
        d = profiles.cold_icp(country=land or "DE")
        return _j({"profil": "Kaltakquise — Vorsortierung nach Branche, KEINE "
                             "Firmen-Rangliste (gemessen zu schwach dafür)",
                   "guete": d.get("quality"), "rows": d.get("branchen")})
    return _j({"fehler": f"unbekanntes Profil '{profil}' — erlaubt: projekt, "
                         f"trichter, bestand, kalt"})


def w_markt_bild(land: str | None = None) -> str:
    """Marktübersicht: vom Gesamtbestand über Trichter bis Kunde, je Land."""
    from .insights import pipeline
    return _j(pipeline.board(land or None))


WERKZEUGE = [
    {"name": "datenbestand",
     "description": "IMMER zuerst aufrufen, wenn SQL geplant ist: Tabellen, Spalten "
                    "und die Regeln, ohne die Zahlen falsch werden (Belege sind "
                    "Angebote; Private Endkunden ausschließen; Architekten-Falle).",
     "input_schema": {"type": "object", "properties": {}},
     "fn": lambda **kw: w_datenbestand()},
    {"name": "sql",
     "description": "Eine SELECT-Abfrage gegen die lokale SQLite-Datenbank (read-only, "
                    "max 200 Zeilen). Für Aggregate und Scheiben, die kein anderes "
                    "Werkzeug abdeckt. Denk an die Regeln aus `datenbestand` — "
                    "insbesondere: segment != 'Private Endkunden' und is_intercompany "
                    "ausschließen, amount > 0 für echte Nachfrage.",
     "input_schema": {"type": "object", "properties": {
         "sql": {"type": "string", "description": "die SELECT-Anweisung (SQLite-Dialekt)"}},
         "required": ["sql"]},
     "fn": lambda **kw: w_sql(kw.get("sql", ""))},
    {"name": "firma_suchen",
     "description": "Firmen nach Namensteil, Ort oder Website-Domain finden. Liefert "
                    "die IDs für firma_dossier und firma_mails.",
     "input_schema": {"type": "object", "properties": {
         "text": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["text"]},
     "fn": lambda **kw: w_firma_suchen(kw.get("text", ""), kw.get("limit", 10))},
    {"name": "firma_dossier",
     "description": "Alles Wesentliche zu EINER Firma: Stammdaten, Angebots-Historie "
                    "(Belege = Angebote!), Verkaufschancen, Korrespondenz-Kennzahlen, "
                    "geprüfte Website-Fakten.",
     "input_schema": {"type": "object", "properties": {
         "company_id": {"type": "integer"}}, "required": ["company_id"]},
     "fn": lambda **kw: w_firma_dossier(kw["company_id"])},
    {"name": "firma_mails",
     "description": "Die jüngsten E-Mails einer Firma (Betreff + Anfang). Für die "
                    "Frage, was zuletzt zwischen uns und der Firma lief.",
     "input_schema": {"type": "object", "properties": {
         "company_id": {"type": "integer"}, "limit": {"type": "integer"}},
         "required": ["company_id"]},
     "fn": lambda **kw: w_firma_mails(kw["company_id"], kw.get("limit", 8))},
    {"name": "profil_rangliste",
     "description": "Die gemessenen Profile abrufen: 'projekt' (offene Projekte "
                    "gereiht, Lift 13,7x), 'trichter' (wer im Gespräch wird aktiv, "
                    "0,75), 'bestand' (wer bricht ab — riskanteste zuerst, 0,80), "
                    "'kalt' (nur Branchen-Vorsortierung, 0,63). Für Fragen wie 'wer "
                    "ist am nächsten an unserem ICP/IPP'.",
     "input_schema": {"type": "object", "properties": {
         "profil": {"type": "string", "enum": ["projekt", "trichter", "bestand", "kalt"]},
         "land": {"type": "string", "description": "Ländercode wie DE, optional"},
         "top": {"type": "integer"}}, "required": ["profil"]},
     "fn": lambda **kw: w_profil_rangliste(kw.get("profil", ""), kw.get("land"),
                                           kw.get("top", 15))},
    {"name": "markt_bild",
     "description": "Marktübersicht je Land: Gesamtbestand, Trichter, aktive Kunden, "
                    "Herkunft (CRM vs. entdeckt).",
     "input_schema": {"type": "object", "properties": {
         "land": {"type": "string", "description": "Ländercode, optional"}}},
     "fn": lambda **kw: w_markt_bild(kw.get("land"))},
]
_WERKZEUG_INDEX = {w["name"]: w for w in WERKZEUGE}

_SYSTEM = """Du bist der Analyse-Assistent von AdWatch, dem Business-Development-Werkzeug von Solarlux (Glas-Faltwände, Schiebesysteme, Wintergärten). Du beantwortest Fragen über den Datenbestand — Firmen, Verkaufschancen, Projekte, Angebote, Korrespondenz, Profile.

ARBEITSWEISE:
- Nutze die Werkzeuge für JEDE Zahl. Erfinde nie eine Zahl und rate nie. Wenn kein Werkzeug die Antwort liefert, sag das offen.
- Erst eingrenzen, dann lesen: hole nie mehr Zeilen als nötig.
- Vor eigener SQL immer zuerst `datenbestand` aufrufen.

REGELN, DIE JEDE ZAHL BETREFFEN (im Projekt teuer gelernt):
- Belege/crm_order_events sind ANGEBOTE, keine Rechnungen. Sag nie "kauft" oder "Umsatz", wenn die Zahl aus Belegen stammt — sag "fragt an" / "Angebotsvolumen". Echte Umsätze existieren im Bestand NICHT.
- Private Endkunden (segment) und Konzern-Töchter (is_intercompany=1) gehören in keine Auswertung.
- Das Segment Architekten fragt praktisch nie an — Basisraten immer je Population nennen.
- 0-Euro-Belege (Garantie, Muster) sind keine Nachfrage.
- Gewerke (Fensterbau, Tischler-Schreiner-Zimmerer, Wintergartenbau …) stehen in companies.sub_segment. `segment` ist grob: Handel, Verarbeiter, Architekten, Baudienstleister … Bei Gewerke-Fragen zuerst `select distinct sub_segment` prüfen statt zu raten.

ANTWORTSTIL:
- Einfaches Deutsch, kurz, direkt. Zahlen deutsch formatiert (1.234,56).
- Nenne zu jeder zentralen Zahl, woher sie stammt (Werkzeug/Abfrage).
- Wenn ein Ergebnis eine bekannte Schwäche hat (kleine Fallzahl, Vorsortierung anstatt Rangliste), sag es in einem Halbsatz dazu."""


# ---------------------------------------------------------------------------
# Die Schleife
# ---------------------------------------------------------------------------

def fragen(frage: str, max_runden: int = MAX_RUNDEN) -> dict:
    """Eine Frage beantworten: Modell wählt Werkzeuge, Python arbeitet, Modell
    formuliert. Gibt Antwort + vollständigen Werkzeug-Beleg + Kosten zurück."""
    frage = (frage or "").strip()
    if not frage:
        raise ValueError("Leere Frage.")
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY fehlt — in den Einstellungen hinterlegen.")

    from anthropic import Anthropic
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    model = config.ANTHROPIC_MODEL
    tools = [{k: w[k] for k in ("name", "description", "input_schema")} for w in WERKZEUGE]

    messages = [{"role": "user", "content": frage}]
    verlauf: list[dict] = []
    tin = tout = 0
    t0 = time.monotonic()

    for _runde in range(max_runden):
        msg = client.messages.create(model=model, max_tokens=1500, system=_SYSTEM,
                                     tools=tools, messages=messages)
        tin += msg.usage.input_tokens
        tout += msg.usage.output_tokens

        calls = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        if not calls:
            antwort = "".join(b.text for b in msg.content
                              if getattr(b, "type", None) == "text").strip()
            return {"antwort": antwort or "(keine Antwort)", "verlauf": verlauf,
                    "tokens_in": tin, "tokens_out": tout,
                    "kosten_usd": _kosten_usd(model, tin, tout), "model": model,
                    "dauer_s": round(time.monotonic() - t0, 1)}

        messages.append({"role": "assistant", "content": msg.content})
        results = []
        for call in calls:
            w = _WERKZEUG_INDEX.get(call.name)
            t1 = time.monotonic()
            try:
                out = w["fn"](**(call.input or {})) if w else \
                    json.dumps({"fehler": f"unbekanntes Werkzeug {call.name}"})
                fehler = None
            except Exception as exc:  # noqa: BLE001 — der Agent soll den Fehler SEHEN
                out = json.dumps({"fehler": str(exc)[:300]}, ensure_ascii=False)
                fehler = str(exc)[:200]
            verlauf.append({"werkzeug": call.name, "params": call.input,
                            "dauer_s": round(time.monotonic() - t1, 2),
                            "fehler": fehler})
            log.info("fragen: %s(%s) in %.2fs%s", call.name,
                     json.dumps(call.input, ensure_ascii=False)[:200],
                     time.monotonic() - t1, f" FEHLER {fehler}" if fehler else "")
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": out})
        messages.append({"role": "user", "content": results})

    return {"antwort": "Abgebrochen: zu viele Werkzeug-Runden. Die Frage enger "
                       "stellen oder in zwei Fragen teilen.",
            "verlauf": verlauf, "tokens_in": tin, "tokens_out": tout,
            "kosten_usd": _kosten_usd(model, tin, tout), "model": model,
            "dauer_s": round(time.monotonic() - t0, 1)}
