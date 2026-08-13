"""Pipeline-Board: wo ein Markt in der Kette steht — und was als Nächstes dran ist.

Die App erzwingt eine feste Reihenfolge (Identität vor Anreicherung, Anreicherung
vor Anzeigen, 30 Käufer vor einem ICP-Modell), aber bis zu diesem Board stand die
Kette nirgends im Produkt: sie lebte im Kopf des Nutzers. Die konkrete Folge,
gemessen an diesem Projekt: "sollen wir zuerst anreichern?" musste im Chat geklärt
werden, obwohl die Antwort deterministisch aus dem Datenstand folgt.

Jede Stufe liefert drei Dinge: den Zählerstand, die ehrliche Erklärung der Lücke
(446 Firmen OHNE auffindbare Website sind kein Rückstand, sondern eine Obergrenze)
und — wo sinnvoll — den nächsten Schritt. Sperren werden angezeigt, nicht nur
durchgesetzt: der ICP-Boden (MIN_WINNERS_USABLE) steht mit Zahl und Grund im Board
statt als Konstante im Code.

Alle Abfragen laufen als JOINs gegen die Scope-Klausel statt über IN-Listen:
Deutschland hat 33.622 Firmen im Scope, und eine expandierte IN-Liste dieser
Größe reißt das SQLite-Variablenlimit.
"""
from __future__ import annotations

from sqlalchemy import and_, func, select

from .. import scope
from ..db import SessionLocal
from ..models import Company, CompanyEnrichment, CrmOrderEvent, WeeklyCompanyMetric
from .icp import MIN_WINNERS_USABLE

# Ab hier zählt ein Käufer als "material" — dieselbe Grenze wie im Bericht
# (report._qualification_story) und im ICP: max(Bestellwert), nicht Summe.
MATERIAL_ORDER_EUR = 2000

_COUNTRY_LABEL = {
    "DE": "Deutschland", "AT": "Österreich", "FR": "Frankreich",
    "NL": "Niederlande", "ES": "Spanien", "DK": "Dänemark", "SE": "Schweden",
    "IT": "Italien", "CH": "Schweiz", "BE": "Belgien", "GB": "Großbritannien",
    "PL": "Polen", "PT": "Portugal", "LU": "Luxemburg", "IE": "Irland",
    "NO": "Norwegen", "FI": "Finnland", "CZ": "Tschechien",
}


def _scope_clause(cc: str):
    return and_(scope.in_scope_clause(), func.upper(Company.country) == cc)


def markets(s) -> list[dict]:
    """Länder mit Bestand, größte zuerst — die Auswahlliste des Boards."""
    rows = s.execute(
        select(func.upper(Company.country), func.count(Company.id))
        .where(scope.in_scope_clause(), Company.country.is_not(None),
               Company.country != "")
        .group_by(func.upper(Company.country))
        .order_by(func.count(Company.id).desc())
        .limit(12)).all()
    return [{"country": cc, "label": _COUNTRY_LABEL.get(cc, cc), "total": n}
            for cc, n in rows]


def market_status(s, cc: str) -> dict:
    """Alle Stufen für ein Land. Ein Aufruf, ein Dict — das Board rendert nur."""
    sc = _scope_clause(cc)

    total = s.scalar(select(func.count(Company.id)).where(sc)) or 0
    with_site = s.scalar(select(func.count(Company.id))
                         .where(sc, Company.website_domain.is_not(None),
                                Company.website_domain != "")) or 0

    # ---- Identität --------------------------------------------------------
    ident: dict[str, int] = {}
    for st, n in s.execute(select(Company.identity_status, func.count(Company.id))
                           .where(sc).group_by(Company.identity_status)):
        ident[st or "unbekannt"] = n

    # ---- Anreicherung ------------------------------------------------------
    # "mit Fakten" = eine Enrichment-Zeile mit extrahierten Feldern existiert.
    # Der ehrliche Nenner bleibt der Gesamtbestand; die not_found-Zahl erklärt,
    # warum 100% nie erreichbar sind (keine auffindbare Website ist ein
    # Ergebnis, kein Rückstand).
    enriched = s.scalar(
        select(func.count(CompanyEnrichment.id))
        .join(Company, Company.id == CompanyEnrichment.company_id)
        .where(sc, CompanyEnrichment.fields.is_not(None))) or 0
    # verifiziert, aber noch ohne Fakten -> genau das holt der nächste Lauf
    verified_no_facts = s.scalar(
        select(func.count(Company.id))
        .outerjoin(CompanyEnrichment, CompanyEnrichment.company_id == Company.id)
        .where(sc, Company.identity_status == "verified",
               CompanyEnrichment.fields.is_(None))) or 0

    # ---- Anzeigen ----------------------------------------------------------
    # "je abgerufen" = es existiert mindestens eine Wochenzeile. "aktiv" zählt
    # auf der JÜNGSTEN Woche je Firma — eine alte aktive Woche ist Geschichte,
    # keine Aktivität.
    latest = (select(WeeklyCompanyMetric.company_id.label("cid"),
                     func.max(WeeklyCompanyMetric.week_start).label("wk"))
              .group_by(WeeklyCompanyMetric.company_id).subquery())
    fetched = s.scalar(
        select(func.count(latest.c.cid))
        .join(Company, Company.id == latest.c.cid).where(sc)) or 0
    active = s.scalar(
        select(func.count(WeeklyCompanyMetric.id))
        .join(latest, and_(WeeklyCompanyMetric.company_id == latest.c.cid,
                           WeeklyCompanyMetric.week_start == latest.c.wk))
        .join(Company, Company.id == WeeklyCompanyMetric.company_id)
        .where(sc, WeeklyCompanyMetric.total_active_ads > 0)) or 0

    # ---- Käufer / Qualifizierung / ICP-Boden -------------------------------
    buyers_sq = select(CrmOrderEvent.company_id).distinct().subquery()
    is_buyer = Company.id.in_(select(buyers_sq.c.company_id))
    buyers = s.scalar(select(func.count(Company.id)).where(sc, is_buyer)) or 0
    material = s.scalar(
        select(func.count()).select_from(
            select(CrmOrderEvent.company_id)
            .join(Company, Company.id == CrmOrderEvent.company_id)
            .where(sc).group_by(CrmOrderEvent.company_id)
            .having(func.max(CrmOrderEvent.amount) >= MATERIAL_ORDER_EUR)
            .subquery())) or 0

    # Dieselben Ränge wie im Bericht: Interessenten (Nicht-Käufer) zählen,
    # Käufer sind Referenz, nicht Ziel.
    betriebe_hoch = s.scalar(select(func.count(Company.id))
                             .where(sc, ~is_buyer, Company.solarlux_fit == "hoch")) or 0
    betriebe_mittel = s.scalar(select(func.count(Company.id))
                               .where(sc, ~is_buyer, Company.solarlux_fit == "mittel")) or 0
    bueros_hoch = s.scalar(select(func.count(Company.id))
                           .where(sc, ~is_buyer,
                                  Company.solarlux_relevance == "hoch")) or 0
    vergibt = s.scalar(select(func.count(Company.id))
                       .where(sc, ~is_buyer, Company.solarlux_relevance == "hoch",
                              Company.decision_role == "vergibt Aufträge")) or 0

    # ---- Bericht ------------------------------------------------------------
    # Der jüngste Bericht, dessen Filter dieses Land nennt. String-Match auf das
    # deutsche Filter-Label ist bewusst schlicht — die Metadaten speichern kein
    # strukturiertes Land, und ein falsch-leeres Feld ist hier schlimmer als ein
    # gelegentlicher Fehltreffer.
    bericht = None
    try:
        from ..report import list_reports
        for r in list_reports():
            if f"Land: {cc}" in (r.get("filter_label") or ""):
                bericht = {"filename": r["filename"], "label": r["label"],
                           "created_at": r.get("created_at")}
                break
    except Exception:  # noqa: BLE001 — ein kaputtes PDF-Verzeichnis sperrt kein Board
        pass

    from .. import config
    return {
        "country": cc, "label": _COUNTRY_LABEL.get(cc, cc),
        "bestand": {"total": total, "mit_website": with_site, "kaeufer": buyers},
        "identitaet": {
            "verified": ident.get("verified", 0),
            "offen": ident.get("needs_review", 0),
            "unbekannt": ident.get("unbekannt", 0) + ident.get("unverified", 0),
            "not_found": ident.get("not_found", 0),
            "unreachable": ident.get("unreachable", 0),
            "conflict": ident.get("conflict", 0),
        },
        "anreicherung": {"mit_fakten": enriched,
                         "verified_ohne_fakten": verified_no_facts,
                         "ohne_website_final": ident.get("not_found", 0)},
        "anzeigen": {"je_abgerufen": fetched, "aktiv": active,
                     "nie_abgerufen": max(total - fetched, 0),
                     "apify_konfiguriert": bool(config.APIFY_API_TOKEN)},
        "qualifizierung": {"betriebe_hoch": betriebe_hoch,
                           "betriebe_mittel": betriebe_mittel,
                           "bueros_hoch": bueros_hoch, "vergibt": vergibt},
        "icp": {"material_kaeufer": material, "boden": MIN_WINNERS_USABLE,
                "modus": "modell" if material >= MIN_WINNERS_USABLE else "scorecard"},
        "bericht": bericht,
    }


def board(country: str | None = None) -> dict:
    with SessionLocal() as s:
        mkts = markets(s)
        known = {m["country"] for m in mkts}
        sel = (country or "").strip().upper()
        if sel not in known:
            sel = mkts[0]["country"] if mkts else "DE"
        return {"markets": mkts, "selected": sel,
                "stages": market_status(s, sel)}
