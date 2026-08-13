"""IPP — Ideales Projekt-Profil: was unterscheidet gewonnene Objekte von
verlorenen, und welche der offenen sehen den Gewinnern ähnlich?

Warum das Objekt die richtige Einheit ist (und nicht die Firma): das Firmen-ICP
scheitert an der Basisrate — 87% der Händler im CRM kaufen ohnehin, es gibt
kaum Negativbeispiele. Projekte haben beides reichlich: 8.189 gewonnene gegen
34.258 verlorene (Basisrate 19,3%, gemessen 2026-08-13). Und die Features sind
sauber PRE-outcome — gemessen am selben Tag: 82% der gewonnenen und 85% der
verlorenen Projekte tragen Produktzeilen, die Angebotsfamilien entstehen also
bei der ANFRAGE und nicht durch den Gewinn (Verhältnis 0,96 — kein Leck; zum
Vergleich: die Firmen-Produktliste hatte 1,85 und flog raus).

Dieselbe Ehrlichkeitsmechanik wie icp.py: Lift mit Laplace-Glättung statt
Häufigkeit (Popularität ist keine Neigung), Support-Boden je Ausprägung,
Out-of-time-Backtest als einzige Wahrheit — trainiert auf Projekten bis
SPLIT_YEAR-1, getestet auf den späteren. Rankt der Score im Test nicht,
sagt build() das im Klartext, statt eine Zahl zu liefern, die keiner
verteidigen kann.

Die unmittelbare Verwendung ist triage(): die offenen Projekte, gereiht nach
Ähnlichkeit zu vergangenen Gewinnern — mit den Gründen je Projekt, nicht nur
einer Zahl.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict

from sqlalchemy import select

from ..db import SessionLocal
from ..models import CrmOpportunityProduct
from .icp import plz_zone
from . import projekte

# Unter diesem Boden ist eine Ausprägung Anekdote, kein Muster — derselbe Wert
# wie icp._MIN_WINNER_SUPPORT, hier über die GEWONNENEN Projekte gezählt.
MIN_SUPPORT = 10
# Erst ab diesem Test-Lift (oberstes gegen unterstes Dezil) gilt das Profil als
# rankfähig; darunter wird es als Scorecard ausgewiesen. 2,0 = das oberste
# Dezil gewinnt doppelt so oft wie das unterste.
RANKS_AT = 2.0
SPLIT_YEAR = 2025          # Training < 2025, Test >= 2025
_LAPLACE = 5.0

_CACHE: dict = {"profile": None}


def _lines_by_guid() -> dict[str, set[str]]:
    with SessionLocal() as s:
        by_guid: dict[str, set[str]] = defaultdict(set)
        for guid, family in s.execute(
                select(CrmOpportunityProduct.opportunity_guid,
                       CrmOpportunityProduct.family)):
            by_guid[(guid or "").lower()].add(family)
    return by_guid


def _features(members, key: str, lines: dict[str, set[str]]) -> set[str]:
    """Die Merkmale EINES Projekts — alles daran ist vor dem Ausgang lesbar.

    `mehrere_vcs` ist bewusst dabei: mehr registrierte Verkaufschancen am selben
    Bau heißt mehr Händler am Tisch — 39,0% Gewinnquote gegen 19,3% insgesamt
    (projekte.MEMBER_BUCKETS). Bei OFFENEN Projekten ist der Stand "bis jetzt";
    das zensiert leicht nach unten und ist als Preis der Nutzbarkeit akzeptiert.
    """
    prim = next((m for m in members if (m.opportunity_guid or "") == key), members[0])
    f: set[str] = set()
    if prim.sales_channel:
        f.add(f"kanal:{prim.sales_channel}")
    zone = plz_zone(prim.postal_code, prim.country)
    if zone:
        f.add(f"region:{zone}")
    if prim.country:
        f.add(f"land:{prim.country.upper()}")
    # Nutzung/Bautyp ist selten gepflegt (3% der VCs) — jedes Mitglied darf es
    # beisteuern, sonst existiert das Merkmal praktisch nicht
    bt = next((m.building_type for m in members if m.building_type), None)
    if bt:
        f.add(f"bautyp:{bt}")
    if len(members) >= 2:
        f.add("mehrere_vcs")
    if any(m.architect_crm_id for m in members):
        f.add("architekt_beteiligt")
    if any(m.end_customer_crm_id for m in members):
        f.add("endkunde_bekannt")
    for m in members:
        for fam in lines.get((m.opportunity_guid or "").lower(), ()):
            f.add(f"familie:{fam}")
    return f


def _collect():
    """Alle Projekte als (key, features, outcome, jahr) — eine Passage."""
    groups = projekte._project_rows()
    lines = _lines_by_guid()
    rows = []
    for key, members in groups.items():
        prim = next((m for m in members if (m.opportunity_guid or "") == key),
                    members[0])
        year = prim.created_on.year if prim.created_on else None
        rows.append((key, _features(members, key, lines),
                     projekte._outcome(members), year, prim))
    return rows


def _fit(rows) -> dict[str, dict]:
    """Lift je Merkmal über die entschiedenen Projekte in `rows`."""
    decided = [(f, o) for _k, f, o, _y, _p in rows if o in (projekte.WON, projekte.LOST)]
    n_all = len(decided)
    n_won = sum(1 for _f, o in decided if o == projekte.WON)
    base = n_won / n_all if n_all else 0.0
    in_won: Counter = Counter()
    in_all: Counter = Counter()
    for feats, o in decided:
        for f in feats:
            in_all[f] += 1
            if o == projekte.WON:
                in_won[f] += 1
    out = {}
    for f, total in in_all.items():
        won = in_won.get(f, 0)
        if won < MIN_SUPPORT:
            continue          # Anekdote — zehn Gewinner sind das Minimum
        # Laplace: eine Ausprägung mit 100% Quote aus 12 Fällen darf nicht mehr
        # versprechen als eine mit 60% aus 800
        p_feat = (won + _LAPLACE * base) / (total + _LAPLACE)
        out[f] = {"lift": p_feat / base if base else 0.0,
                  "won": won, "total": total, "rate": won / total}
    return out


def _score(feats: set[str], weights: dict[str, dict]) -> float:
    """Summe der Log-Lifts — naive, aber verteidigbar; ohne Merkmal = Basisrate."""
    return sum(math.log(w["lift"]) for f in feats
               if (w := weights.get(f)) and w["lift"] > 0)


def build(force: bool = False) -> dict:
    """Profil + Out-of-time-Backtest in einem Aufruf. Der Backtest ist nicht
    Beiwerk, er ist das URTEIL: trainiert auf < SPLIT_YEAR, getestet auf den
    entschiedenen Projekten ab SPLIT_YEAR. `ranks` sagt, ob triage() eine
    Reihenfolge liefert oder nur eine dokumentierte Checkliste."""
    if _CACHE["profile"] and not force:
        return _CACHE["profile"]
    rows = _collect()

    train = [r for r in rows if r[3] and r[3] < SPLIT_YEAR]
    test = [r for r in rows if r[3] and r[3] >= SPLIT_YEAR
            and r[2] in (projekte.WON, projekte.LOST)]
    weights = _fit(train)

    # --- Backtest: Dezile über den Score, Gewinnquote je Dezil -------------
    scored = sorted(((_score(f, weights), o) for _k, f, o, _y, _p in test),
                    key=lambda t: t[0])
    deciles = []
    n = len(scored)
    for d in range(10):
        chunk = scored[d * n // 10:(d + 1) * n // 10]
        wins = sum(1 for _s, o in chunk if o == projekte.WON)
        deciles.append({"decile": d + 1, "n": len(chunk),
                        "win_rate": wins / len(chunk) if chunk else 0.0})
    top, bottom = deciles[-1]["win_rate"], deciles[0]["win_rate"]
    lift_td = (top / bottom) if bottom else float("inf")
    rising = sum(1 for a, b in zip(deciles, deciles[1:])
                 if b["win_rate"] >= a["win_rate"])

    full_weights = _fit(rows)   # das AUSGELIEFERTE Profil nutzt alles Entschiedene
    decided_all = [r for r in rows if r[2] in (projekte.WON, projekte.LOST)]
    base = (sum(1 for r in decided_all if r[2] == projekte.WON) / len(decided_all)
            if decided_all else 0)

    profile = {
        "base_rate": round(base, 4),
        "train": {"n": len([r for r in train if r[2] != projekte.OPEN]),
                  "until": SPLIT_YEAR - 1},
        "test": {"n": n, "from": SPLIT_YEAR,
                 "lift_top_vs_bottom": round(lift_td, 2),
                 "monotone_steps": rising, "deciles": deciles},
        "ranks": lift_td >= RANKS_AT and rising >= 6,
        "features": sorted(
            ({"feature": f, **{k: (round(v, 3) if isinstance(v, float) else v)
                               for k, v in w.items()}}
             for f, w in full_weights.items()),
            key=lambda x: -x["lift"]),
        "weights": full_weights,
    }
    _CACHE["profile"] = profile
    return profile


def triage(limit: int = 50) -> dict:
    """Die OFFENEN Projekte, gereiht nach Ähnlichkeit zu vergangenen Gewinnern —
    je Projekt die drei stärksten Gründe, damit ein Vertriebler dem Ranking
    widersprechen kann, statt ihm glauben zu müssen."""
    p = build()
    weights = p["weights"]
    rows = _collect()
    open_rows = [r for r in rows if r[2] == projekte.OPEN]
    scored = []
    for key, feats, _o, _y, prim in open_rows:
        s = _score(feats, weights)
        why = sorted(((f, weights[f]["lift"]) for f in feats if f in weights),
                     key=lambda t: -abs(math.log(t[1])))[:3]
        scored.append({"project_id": key, "name": prim.name,
                       "city": prim.city, "country": prim.country,
                       "channel": prim.sales_channel,
                       "estimated_value": prim.estimated_value,
                       "score": round(s, 3),
                       "why": [{"feature": f, "lift": round(l, 2)} for f, l in why]})
    scored.sort(key=lambda r: -r["score"])
    return {"ranks": p["ranks"], "base_rate": p["base_rate"],
            "open_total": len(open_rows), "rows": scored[:limit]}
