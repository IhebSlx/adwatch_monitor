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


def _j(obj) -> str:
    """JSON für den Agenten — notfalls gekürzt, aber NIE kaputt.

    Erst serialisieren, dann abschneiden wäre der naheliegende Weg und ist
    falsch: das Ergebnis ist mitten in einer Struktur abgeschnittenes JSON, das
    der Agent nicht mehr lesen kann. Gemessen am 2026-08-20 an einer Abfrage
    über 200 Firmen — sie kippte bei Zeichen 6.001 mitten in einen Namen.

    Stattdessen werden ZEILEN entfernt, bis es passt. Das Ergebnis bleibt
    gültiges JSON und sagt selbst, was fehlt — der Agent kann dann enger
    filtern, statt an einem Parserfehler zu scheitern.
    """
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= MAX_ERGEBNIS_ZEICHEN:
        return text

    def _kuerze(liste: list, huelle) -> str | None:
        behalten = len(liste)
        while behalten > 1:
            behalten //= 2
            kurz = huelle(liste[:behalten])
            if isinstance(kurz, dict):
                kurz["gekuerzt"] = (f"{len(liste)} Zeilen vorhanden, {behalten} "
                                    f"gezeigt — enger filtern oder aggregieren")
            t = json.dumps(kurz, ensure_ascii=False, default=str)
            if len(t) <= MAX_ERGEBNIS_ZEICHEN:
                return t
        return None

    if isinstance(obj, list) and obj:
        t = _kuerze(obj, lambda teil: {"zeilen": teil, "gekuerzt": True})
        if t:
            return t
    elif isinstance(obj, dict):
        listen = [(k, v) for k, v in obj.items() if isinstance(v, list) and v]
        if listen:
            k, v = max(listen, key=lambda kv: len(json.dumps(kv[1], default=str)))
            t = _kuerze(v, lambda teil: {**obj, k: teil})
            if t:
                return t

    # Notnagel: als STRING-Wert kürzen, damit die Hülle gültiges JSON bleibt
    return json.dumps({"gekuerzt_roh": text[:MAX_ERGEBNIS_ZEICHEN],
                       "hinweis": f"{len(text):,} Zeichen — Abfrage enger fassen"},
                      ensure_ascii=False)


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


# --- Einen Lauf VORSCHLAGEN -------------------------------------------------
# Der Agent darf den Umfang bestimmen, nicht den Startknopf druecken.
#
# Ein Lauf kostet echtes Geld (Anreicherung und Identitaetspruefung je rund
# halber Cent pro Firma, Ad lookup ueber Apify) und laeuft ueber Stunden. Ein
# missverstandener Satz wie "mach das mal fuer alle" waeren 46.810 Firmen. Also
# macht das Modell genau das, was es gut kann -- aus einem Satz einen Filter
# bauen -- und Python macht den Rest: zaehlen, schaetzen, auflisten. Gestartet
# wird erst per Knopf in der Oberflaeche.
#
# Die Filtersprache ist DIESELBE wie im Firmen-Explorer (customers._apply_filters),
# damit ein Vorschlag genau die Menge trifft, die man dort auch sehen wuerde.
# Welche Filterschluessel es gibt, wird NICHT von Hand gepflegt, sondern aus
# customers._apply_filters gelesen.
#
# Die Handliste hatte genau den Fehler, den die Pruefung verhindern soll: vier
# Eintraege (products_str, competitor_brands_str, mentions_solarlux_str,
# assessment) waren gar keine Filter, sondern SPALTENNAMEN des Excel-Exports.
# Sie standen in der Liste, wurden also durchgewinkt — und _apply_filters
# ignorierte sie stillschweigend. Ein Vorschlag haette "nur Firmen, die cero
# fuehren" behauptet und in Wahrheit alle 46.810 getroffen.
#
# Aus der Quelle gelesen kann das nicht mehr passieren: kommt ein Filter dazu,
# darf der Agent ihn sofort benutzen; faellt einer weg, bietet er ihn nicht mehr an.
def _filter_schluessel() -> set[str]:
    import inspect
    from .customers import _apply_filters
    q = inspect.getsource(_apply_filters)
    schluessel = set(re.findall(r"""f\.get\(\s*["']([a-z_]+)["']""", q))
    schluessel |= set(re.findall(r"""f\[\s*["']([a-z_]+)["']\s*\]""", q))
    # Die Schleifen ueber (Feldname, Spalte) stehen als Tupel im Quelltext.
    schluessel |= set(re.findall(r"""\(\s*["']([a-z_]+)["']\s*,\s*Company\.""", q))
    return schluessel


_FILTER_SCHLUESSEL = _filter_schluessel()


_SCHRITT_NAMEN = {
    "anreichern": ("enrich", "Daten anreichern", 0.004),
    "identitaet": ("identity", "Identitätsprüfung", 0.005),
    "anzeigen": ("ads", "Ad lookup", None),
    "bericht": ("report", "Bericht erstellen", 0.0),
}


def w_lauf_vorschlagen(filter: dict | None, schritte: list[str],
                       label: str | None = None) -> str:
    from .customers import query_companies

    f = dict(filter or {})
    # Nicht verhandelbar, egal was im Satz stand: Privatadressen und eigene
    # Toechter gehoeren in keinen Lauf.
    aus = list(f.get("exclude_segment") or [])
    if "Private Endkunden" not in aus:
        aus.append("Private Endkunden")
    f["exclude_segment"] = aus

    # Unbekannte Schluessel werden von _apply_filters STILL ignoriert. Beim
    # ersten Test schrieb das Modell `postal_prefix: "8"` fuer Bayern, der
    # Server kannte den Schluessel nicht, und der Vorschlag haette "Bayern"
    # behauptet, waehrend er ganz Deutschland getroffen haette. Ein Filter, der
    # nicht wirkt, muss LAUT sein — sonst genehmigt jemand einen Lauf fuer eine
    # Menge, die er nie gesehen hat.
    fremd = [k for k in f if k not in _FILTER_SCHLUESSEL]
    if fremd:
        return _j({"fehler": f"Diese Filter kennt der Server nicht: {fremd}. "
                             "Sie wuerden stillschweigend ignoriert.",
                   "erlaubt": sorted(_FILTER_SCHLUESSEL),
                   "hinweis": "Fuer Regionen gibt es keinen Filter — nutze `q` "
                              "(sucht in Name und SAP-Nummer) oder frag nach."})

    unbekannt = [s for s in schritte if s not in _SCHRITT_NAMEN]
    if unbekannt:
        return _j({"fehler": f"unbekannte Schritte: {unbekannt}",
                   "erlaubt": list(_SCHRITT_NAMEN)})
    if not schritte:
        return _j({"fehler": "kein Schritt gewählt"})

    gesamt = query_companies(f, page_size=1).get("total", 0)
    if not gesamt:
        return _j({"vorschlag": False,
                   "hinweis": "Dieser Filter trifft keine einzige Firma — "
                              "nichts zu starten.", "filter": f})

    # Deckel: mehr als das laeuft tagelang und kostet entsprechend. Wird er
    # erreicht, steht das im Vorschlag, statt still zu kappen.
    DECKEL = 2000
    anzahl = min(gesamt, DECKEL)

    plan: dict = {}
    kosten = []
    for s in schritte:
        key, name, pro = _SCHRITT_NAMEN[s]
        if key == "ads":
            plan["ads"] = ["meta"]
            kosten.append({"schritt": name, "hinweis": "über Apify, Preis je Abruf"})
        elif key == "report":
            plan["report"] = "full"
            kosten.append({"schritt": name, "usd": 0.0})
        else:
            plan[key] = True
            kosten.append({"schritt": name, "usd": round(pro * anzahl, 2)})

    return _j({
        "vorschlag": True,
        "label": label or "Lauf aus dem Chatbot",
        "filter": f,
        "treffer_gesamt": gesamt,
        "im_lauf": anzahl,
        "gekappt": gesamt > DECKEL,
        "schritte": [_SCHRITT_NAMEN[s][1] for s in schritte],
        "plan": plan,
        "kosten": kosten,
        # BEWUSST keine Firmen-IDs: 2.000 Zahlen durch das Modell zu schicken
        # kostet Token und bringt nichts — und der Kuerzungsschutz von _j() warf
        # sie beim ersten Versuch stillschweigend auf 500 herunter, waehrend
        # daneben "2.000 im Lauf" stand. Der Startknopf schickt den FILTER, und
        # der Server loest ihn im selben Moment auf.
        "deckel": DECKEL,
        "hinweis": "NICHT gestartet. Der Vorschlag erscheint im Chat als Karte "
                   "mit Startknopf — erst ein Klick löst den Lauf aus.",
    })


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
    {"name": "lauf_vorschlagen",
     "description": "Einen Pipeline-Lauf für eine gefilterte Firmenmenge VORSCHLAGEN "
                    "(Anreicherung, Identitätsprüfung, Ad lookup, Bericht). Startet "
                    "NICHTS — liefert Umfang, Kosten und einen Startknopf für den "
                    "Menschen. Für Sätze wie 'reichere alle Händler in Bayern an' "
                    "oder 'mach einen Ad lookup für die Architekten in DE'. "
                    "Der Filter spricht dieselbe Sprache wie der Firmen-Explorer: "
                    "country ['DE'], segment ['Handel'], sub_segment, sales_channel, "
                    "customer_state 'active'|'new'|'lapsed'|'never', ad_activity, "
                    "enrichment_status 'none', has_website true, no_website true, "
                    "revenue_min, fit_min, q (Freitext). Private Endkunden und "
                    "Konzern-Töchter werden immer ausgeschlossen.",
     "input_schema": {"type": "object", "properties": {
         "filter": {"type": "object", "description": "Filterobjekt wie im Explorer"},
         "schritte": {"type": "array", "items": {"type": "string",
             "enum": ["anreichern", "identitaet", "anzeigen", "bericht"]}},
         "label": {"type": "string", "description": "Name des Laufs, optional"}},
         "required": ["schritte"]},
     "fn": lambda **kw: w_lauf_vorschlagen(kw.get("filter"), kw.get("schritte", []),
                                           kw.get("label"))},
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

LÄUFE STARTEN:
- Du startest NICHTS. Willst du einen Lauf (Anreicherung, Identitätsprüfung, Ad lookup, Bericht), rufst du `lauf_vorschlagen` mit einem Filter auf. Das Ergebnis ist ein Vorschlag mit Umfang und Kosten; gestartet wird er per Knopf vom Menschen.
- Sag im Text, WAS der Filter trifft und wie viele Firmen das sind. Behaupte nie, ein Lauf sei gestartet.
- Ist der Filter unklar ("die guten Händler"), frag nach, statt zu raten. Ein zu weiter Filter kostet echtes Geld.

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

MAX_VERLAUF = 6          # frühere Wechsel im Kontext — mehr kostet mehr, ohne zu helfen


def fragen(frage: str, verlauf: list | None = None,
           max_runden: int = MAX_RUNDEN) -> dict:
    """Eine Frage beantworten: Modell wählt Werkzeuge, Python arbeitet, Modell
    formuliert. Gibt Antwort + vollständigen Werkzeug-Beleg + Kosten zurück.

    `verlauf` sind frühere Wechsel als [{"frage": …, "antwort": …}] — damit
    "und in Österreich?" funktioniert. Bewusst nur der TEXT früherer Antworten,
    nicht deren Werkzeug-Blöcke: die Antwort trägt das Ergebnis bereits in
    Worten, und ein vollständiges Replay der Werkzeugaufrufe würde jede Runde
    teurer machen, ohne mehr zu wissen.
    """
    frage = (frage or "").strip()
    if not frage:
        raise ValueError("Leere Frage.")
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY fehlt — in den Einstellungen hinterlegen.")

    from anthropic import Anthropic
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    model = config.ANTHROPIC_MODEL
    tools = [{k: w[k] for k in ("name", "description", "input_schema")} for w in WERKZEUGE]

    messages: list[dict] = []
    for w in (verlauf or [])[-MAX_VERLAUF:]:
        f, a = str(w.get("frage") or "").strip(), str(w.get("antwort") or "").strip()
        if f and a:
            messages.append({"role": "user", "content": f})
            messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": frage})

    werkzeug_log: list[dict] = []
    # Der letzte Vorschlag reist getrennt vom Text zur Oberfläche: dort wird er
    # zur Karte mit Startknopf. Im Antworttext stünde er nur als Prosa, und
    # eine Zahl in Prosa kann man nicht anklicken.
    vorschlag: dict | None = None
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
            return {"antwort": antwort or "(keine Antwort)", "verlauf": werkzeug_log,
                    "vorschlag": vorschlag,
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
            if call.name == "lauf_vorschlagen" and not fehler:
                try:
                    _v = json.loads(out)
                    if _v.get("vorschlag"):
                        vorschlag = _v
                except Exception:  # noqa: BLE001 — kaputtes JSON darf den Lauf nicht kippen
                    pass
            werkzeug_log.append({"werkzeug": call.name, "params": call.input,
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
            "verlauf": werkzeug_log, "vorschlag": vorschlag, "tokens_in": tin, "tokens_out": tout,
            "kosten_usd": _kosten_usd(model, tin, tout), "model": model,
            "dauer_s": round(time.monotonic() - t0, 1)}
