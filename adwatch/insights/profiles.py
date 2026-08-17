"""Entscheidungsspezifische Kundenprofile — vier Fragen, vier Güten.

Der Fehler aller früheren Versuche war, EIN "ICP" bauen zu wollen. Es sind vier
verschiedene Entscheidungen, und sie sind unterschiedlich gut vorhersagbar
(gemessen 2026-08-13, Methodik und Zahlen in ICP-STRATEGY.md):

    Projekt-Profil (ipp.py)     Lift 13,7x   -> ausgeliefert
    Funnel-Triage               AUC 0,753    -> hier
    Kunden-Fortsetzung          AUC 0,797    -> hier
    Kalt-Akquise                AUC 0,60     -> hier, ausdrücklich als Vorsortierung

Der rote Faden: Verhaltensdaten schlagen Stammdaten um +0,14 bis +0,16 AUC — in
jedem der drei Fälle. Die Kalt-Akquise ist genau der Fall ohne Verhaltensdaten,
und deshalb schwach. Das ist kein Modell- und kein Datenmengenproblem (die
Lernkurve ist ab ~6.000 Zeilen flach), sondern ein Merkmalsproblem.

Bewusst OHNE scikit-learn: die ausgelieferten Modelle sind
Lift-Punktetabellen mit Laplace-Glättung, wie in icp.py und ipp.py. Der Grund
ist nicht Bequemlichkeit — bei AUC 0,60 bis 0,80 lag der Gradient-Boosting-
Vorsprung in der Messung bei ±0,01, und ein Verkäufer kann eine Punktetabelle
nachrechnen und ihr widersprechen. Ein Modell, dem man nicht widersprechen kann,
wird nicht benutzt.

VERGIFTETE MERKMALE — niemals aufnehmen, mit gemessenem Grund:
    kv                      Verfügbarkeitsquotient 150x  (wird zugeteilt, WEIL Kunde)
    products/description/…  Quotient 200-220x            (angereichert wurde nur, wer kauft)
    Vertriebsweg            Wert 'Direktvertrieb' n=55, Kaufrate 54,5% vs 13,5%
                            -> beschreibt unsere Beziehung, nicht die Firma
    Untersegment = leer     n=209, Kaufrate 42,6% vs 13,5% -> Import-Herkunft.
                            Gefährlich: eine neu gefundene Firma hat auch kein
                            Untersegment und bekäme aus nicht übertragbarem
                            Grund eine hohe Punktzahl.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import Counter, defaultdict

from sqlalchemy import func, select

from .. import scope
from ..db import SessionLocal
from ..models import Company, CrmOpportunity, CrmOrderEvent

DEALER_SEGMENTS = ("Handel", "Verarbeiter")
MATERIAL_EUR = 2000
_LAPLACE = 5.0
MIN_SUPPORT = 25          # Träger je Ausprägung, sonst Anekdote

# Gemessene Güte je Profil — wird mit ausgeliefert, damit niemand eine
# Rangfolge für belastbarer hält, als sie ist.
QUALITY = {
    "kalt":       {"auc": 0.629, "geo_holdout": 0.588, "top_decile_lift": 1.61,
                   "verdict": "schwach — Vorsortierung, keine Rangliste"},
    "funnel":     {"auc": 0.753, "top_decile_lift": 3.83,
                   "verdict": "stark — direkt abtelefonierbar"},
    "bestand":    {"auc": 0.797, "top_decile_lift": 1.92,
                   "verdict": "stark — unterstes Fünftel ist die Abbruchgefahr"},
}


def _plz2(pc, country) -> str | None:
    if not pc:
        return None
    d = "".join(ch for ch in str(pc) if ch.isdigit())
    if len(d) < 4:
        return None
    cc = (str(country or "DE")[:2] or "DE").upper()
    return f"{cc}{d[:2]}"


def _load(cutoff: dt.date, country: str | None = None):
    """Firmen im Scope + zeitlich sauber getrennte Vor-/Nachgeschichte."""
    with SessionLocal() as s:
        stmt = select(Company).where(scope.in_scope_clause(),
                                     Company.segment.in_(DEALER_SEGMENTS))
        if country:
            stmt = stmt.where(func.upper(Company.country) == country.upper())
        comps = list(s.scalars(stmt))
        ids = [c.id for c in comps]
        if not ids:
            return [], {}, {}, {}

        # `paid` zählt NUR Ereignisse mit echtem Betrag. 14.049 der 91.992
        # Bewegungen stehen auf 0 EUR (Garantie, Muster, Ersatz), und 486 Firmen
        # haben ausschließlich solche — die galten bisher als Kunden. Eine
        # Garantiegutschrift ist kein Kauf; wer sie als Erfolg zählt, trainiert
        # das Modell auf Reklamationen. Gemessen kostet die Bereinigung nichts
        # und bringt +0,012 AUC (0,617 -> 0,629).
        id_set = set(ids)
        pre: dict[int, dict] = defaultdict(lambda: {"n": 0, "sum": 0.0, "last": None})
        post: dict[int, dict] = defaultdict(
            lambda: {"n": 0, "paid": 0, "sum": 0.0, "max": 0.0})
        for cid, d, amt in s.execute(
                select(CrmOrderEvent.company_id, CrmOrderEvent.order_date,
                       CrmOrderEvent.amount)):
            if cid not in id_set:
                continue
            if d < cutoff:
                p = pre[cid]; p["n"] += 1; p["sum"] += amt or 0
                p["last"] = d if p["last"] is None else max(p["last"], d)
            else:
                q = post[cid]; q["n"] += 1; q["sum"] += amt or 0
                q["max"] = max(q["max"], amt or 0)
                if (amt or 0) > 0:
                    q["paid"] += 1

        # Trichter-Historie strikt vor dem Stichtag, je CRM-Id
        vc: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "won": 0, "lost": 0, "open": 0, "value": 0.0})
        for guid, state, val, created in s.execute(
                select(CrmOpportunity.parent_account_crm_id, CrmOpportunity.state,
                       CrmOpportunity.order_value, CrmOpportunity.created_on)):
            if not guid or not created or created.date() >= cutoff:
                continue
            v = vc[guid]
            v["n"] += 1
            v["value"] += val or 0
            if state == "gewonnen":
                v["won"] += 1
            elif state == "verloren":
                v["lost"] += 1
            elif state == "offen":
                v["open"] += 1
    return comps, pre, post, vc


def _features_cold(c: Company) -> set[str]:
    """Was ein Fremder über diese Firma wüsste — mehr darf hier nicht hinein."""
    f = set()
    if c.segment:
        f.add(f"segment:{c.segment}")
    # Untersegment nur wenn GESETZT: 'leer' ist Import-Herkunft, kein Merkmal
    if c.sub_segment:
        f.add(f"branche:{c.sub_segment}")
    z = _plz2(c.postal_code, c.country)
    if z:
        f.add(f"region:{z}")
    if c.country:
        f.add(f"land:{c.country.upper()}")
    f.add(f"website:{'ja' if c.website_domain else 'nein'}")
    return f


def _fit(rows: list[tuple[set[str], int]]) -> dict[str, dict]:
    """Lift je Ausprägung mit Laplace-Glättung.

    Popularität ist keine Neigung: eine Ausprägung, die 36 % der Käufer tragen,
    kann Lift 1,03 haben, weil sie 35 % von allen tragen. Deshalb Lift und nicht
    Häufigkeit — dieselbe Lehre wie in icp.fit_for."""
    n_all = len(rows)
    n_won = sum(y for _f, y in rows)
    base = n_won / n_all if n_all else 0.0
    won: Counter = Counter()
    tot: Counter = Counter()
    for feats, y in rows:
        for f in feats:
            tot[f] += 1
            won[f] += y
    out = {}
    for f, t in tot.items():
        if t < MIN_SUPPORT:
            continue
        p = (won[f] + _LAPLACE * base) / (t + _LAPLACE)
        out[f] = {"lift": (p / base) if base else 0.0, "won": won[f],
                  "total": t, "rate": won[f] / t}
    return out, base


def _score(feats: set[str], w: dict[str, dict]) -> float:
    return sum(math.log(v["lift"]) for f in feats
               if (v := w.get(f)) and v["lift"] > 0)


def cold_icp(country: str | None = "DE", cutoff: dt.date | None = None) -> dict:
    """Kalt-Akquise: welche Branche/Region kauft überhaupt?

    AUSDRÜCKLICH eine Vorsortierung. Gemessene AUC 0,598 (Geo-Holdout 0,588):
    ein Fensterbauer ist ein besserer Erstkontakt als ein Baustoffhändler, aber
    Rang 3 ist nicht besser als Rang 30. Wer das anders darstellt, verkauft
    Rauschen als Rangfolge."""
    cutoff = cutoff or dt.date(2023, 1, 1)
    comps, pre, post, _vc = _load(cutoff, country)
    rows = [(_features_cold(c), 1 if post.get(c.id, {}).get("paid", 0) else 0)
            for c in comps if not pre.get(c.id, {}).get("n", 0)
            and c.sub_segment]          # siehe Modul-Kopf: leer = Herkunftsartefakt
    w, base = _fit(rows)
    branchen = sorted(
        ({"branche": f.split(":", 1)[1], **v} for f, v in w.items()
         if f.startswith("branche:")),
        key=lambda x: -x["lift"])
    return {"quality": QUALITY["kalt"], "base_rate": round(base, 4),
            "n": len(rows), "positives": sum(y for _f, y in rows),
            "country": country, "cutoff": str(cutoff),
            "branchen": [{"branche": b["branche"], "lift": round(b["lift"], 2),
                          "rate": round(b["rate"], 3), "n": b["total"]}
                         for b in branchen],
            "hinweis": "Vorsortierung. Die Reihenfolge INNERHALB der Liste "
                       "trägt wenig Information (AUC 0,60)."}


def funnel_triage(limit: int = 100, country: str | None = None,
                  cutoff: dt.date | None = None) -> dict:
    """Wer im Trichter wird Kunde? AUC 0,753, oberstes Dezil 3,83x Basisrate.

    Integritätsprüfung, die dieses Modell bestanden hat: ohne das Merkmal
    'hat schon eine VC gewonnen' ist die AUC unverändert 0,753. Das Signal ist
    also Kontaktintensität (Anzahl, Werte, Rollen) und nicht die fast
    buchhalterische Tatsache eines gewonnenen Auftrags."""
    cutoff = cutoff or dt.date.today().replace(month=1, day=1)
    comps, pre, post, vc = _load(cutoff, country)
    rows, live = [], []
    for c in comps:
        if pre.get(c.id, {}).get("n", 0):
            continue                       # schon Kunde -> andere Frage
        v = vc.get(c.crm_id or "", None)
        if not v or not v["n"]:
            continue                       # nicht im Trichter -> Kalt-ICP
        f = _features_cold(c)
        f.add("vc_anzahl:" + ("1" if v["n"] == 1 else "2-3" if v["n"] <= 3 else "4+"))
        f.add("vc_offen:" + ("ja" if v["open"] else "nein"))
        f.add("vc_verloren:" + ("ja" if v["lost"] else "nein"))
        if v["value"] >= 50000:
            f.add("vc_wert:hoch")
        y = 1 if post.get(c.id, {}).get("paid", 0) else 0
        rows.append((f, y))
        live.append((c, f, v))
    w, base = _fit(rows)
    scored = []
    for c, f, v in live:
        why = sorted(((k, w[k]["lift"]) for k in f if k in w),
                     key=lambda t: -abs(math.log(t[1])))[:3]
        scored.append({"company_id": c.id, "name": c.name, "city": c.city,
                       "country": c.country, "sub_segment": c.sub_segment,
                       "vc_n": v["n"], "vc_open": v["open"],
                       "vc_value": round(v["value"]),
                       "score": round(_score(f, w), 3),
                       "why": [{"feature": k, "lift": round(l, 2)} for k, l in why]})
    scored.sort(key=lambda r: -r["score"])
    return {"quality": QUALITY["funnel"], "base_rate": round(base, 4),
            "n": len(rows), "rows": scored[:limit]}


def continuation(limit: int = 100, country: str | None = None,
                 cutoff: dt.date | None = None,
                 min_revenue: float = MATERIAL_EUR) -> dict:
    """Bestandskunden: wer kauft weiter, wer bricht ab? AUC 0,797.

    Die Dezile laufen von 18 % bis 98 % Fortsetzungswahrscheinlichkeit. Der
    handlungsrelevante Teil ist das UNTERSTE Fünftel — Kunden, deren eigener
    Rhythmus abreißt. Das obere Ende bestätigt nur, was man ohnehin weiß."""
    cutoff = cutoff or dt.date.today().replace(month=1, day=1)
    comps, pre, post, _vc = _load(cutoff, country)
    rows, live = [], []
    for c in comps:
        p = pre.get(c.id)
        if not p or not p["n"]:
            continue
        days = (cutoff - p["last"]).days if p["last"] else 9999
        f = _features_cold(c)
        f.add("frequenz:" + ("1" if p["n"] == 1 else "2-5" if p["n"] <= 5
                             else "6-20" if p["n"] <= 20 else "20+"))
        f.add("aktualitaet:" + ("<90T" if days < 90 else "<1J" if days < 365
                                else "<2J" if days < 730 else "2J+"))
        f.add("volumen:" + ("<5k" if p["sum"] < 5000 else "<50k" if p["sum"] < 50000
                            else "<250k" if p["sum"] < 250000 else "250k+"))
        y = 1 if post.get(c.id, {}).get("paid", 0) else 0
        rows.append((f, y))
        live.append((c, f, p, days, y))
    w, base = _fit(rows)
    scored = []
    for c, f, p, days, _y in live:
        scored.append({"company_id": c.id, "name": c.name, "city": c.city,
                       "orders": p["n"], "revenue": round(p["sum"]),
                       "days_since_last": days,
                       "score": round(_score(f, w), 3)})
    scored.sort(key=lambda r: r["score"])      # RISIKO zuerst — das ist die Aktion
    # Ohne Wertgrenze besteht die Spitze der Liste aus Firmen mit EINER
    # 40-Euro-Bestellung vor drei Jahren. Mathematisch korrekt (sie kaufen
    # sicher nicht wieder), betriebswirtschaftlich wertlos: da ist nichts zu
    # retten, was den Anruf lohnt. Die Grenze ist dieselbe wie überall im
    # Projekt für "materieller Kunde" (Bericht, ICP): 2.000 Euro.
    worth = [r for r in scored if r["revenue"] >= min_revenue]
    return {"quality": QUALITY["bestand"], "base_rate": round(base, 4),
            "n": len(rows), "at_risk": worth[:limit],
            "min_revenue": min_revenue,
            "ausgeblendet": len(scored) - len(worth),
            "hinweis": f"Aufsteigend: riskanteste zuerst. Nur Kunden ab "
                       f"{min_revenue:,.0f} EUR Bestandsumsatz — darunter lohnt "
                       f"die Rückholung den Anruf nicht."}
