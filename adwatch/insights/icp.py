"""PART 3d — the IDEAL CUSTOMER PROFILE (ICP) and the FIT score.

Two orthogonal questions, two scores:
  fit_score          "How much does this Firma LOOK like the ones that buy
                      from us?"  — computed here, against a profile built from
                      a chosen winners set (any Explorer filter).
  opportunity_score  "Is there something to win right now?" — the existing
                      Divergenz (insights/divergence.py), reused unchanged.
  target_score       geometric mean of the two when both exist — the ranked
                      call list.

The profile is deliberately COUNTING, not machine learning: for every feature
(Segment, Region, Produkte, Größe, Alter, Anzeigen-Aktivität) it stores the
winners' value distribution, and a company scores by how typical its values are
among winners — normalised so the modal winner value = 1.0. Every score
decomposes into a per-feature breakdown a BD colleague can read ("Segment
Fachhandel: 92% so häufig wie der häufigste Gewinner-Wert"). No black box, or
the ranking won't be trusted — the same rule Divergenz follows.

Missing data never punishes: a feature the company has no value for is EXCLUDED
and the remaining weights renormalised. An unenriched company scores on master
data alone (and its breakdown says how many features were usable), it is not
dragged to zero for being un-crawled.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import func, select

from .. import scope
from ..db import SessionLocal
from ..models import Company, IcpProfile, WeeklyCompanyMetric

# Feature weights — the preliminary formula, adjustable without touching the
# scoring code (stored on each profile row, so old scores stay traceable even
# after the defaults change).
DEFAULT_WEIGHTS = {
    "segment": 2.0,
    "sub_segment": 1.5,
    "sales_channel": 1.0,
    "plz_zone": 1.0,
    "products": 2.0,
    "size_bucket": 1.0,
    "age_bucket": 0.5,
    "ad_presence": 1.0,
}

# Segments that must NEVER define the profile: they are not the kind of company
# the partner program acquires. "Private Endkunden" are consumers — 332 of them
# bought something, which silently made them 32% of the default winners set and
# dragged the trade-partner profile toward consumer traits. The Explorer already
# hides them from the VIEW; the winners definition has to exclude them too.
WINNER_EXCLUDED_SEGMENTS = ("Private Endkunden",)

_FEATURE_LABEL_DE = {
    "segment": "Kundensegment", "sub_segment": "Untersegment",
    "sales_channel": "Vertriebsweg", "plz_zone": "PLZ-Zone",
    "products": "Produkte", "size_bucket": "Betriebsgröße",
    "age_bucket": "Firmenalter", "ad_presence": "Anzeigen-Aktivität",
}

# ---------------------------------------------------------------------------
# feature extraction per company
# ---------------------------------------------------------------------------

_NUM_WORDS = {"ein": 1, "eine": 1, "einem": 1, "zwei": 2, "drei": 3, "vier": 4,
              "fuenf": 5, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8,
              "neun": 9, "zehn": 10, "elf": 11, "zwoelf": 12, "zwölf": 12,
              "fuenfzehn": 15, "fünfzehn": 15, "zwanzig": 20, "dreissig": 30,
              "dreißig": 30, "vierzig": 40, "fuenfzig": 50, "fünfzig": 50}


def parse_employee_count(hint: str | None) -> int | None:
    """'15 Mitarbeiter' -> 15, 'drei Angestellten' -> 3, '10-15 Mann' -> 12.
    Verbatim hints only — this parses what enrichment stored, it never guesses."""
    if not hint:
        return None
    s = str(hint).lower()
    m = re.search(r"(\d{1,4})\s*[-–bis]+\s*(\d{1,4})", s)
    if m:
        return (int(m.group(1)) + int(m.group(2))) // 2
    m = re.search(r"\d{1,4}", s)
    if m:
        return int(m.group(0))
    for word, n in _NUM_WORDS.items():
        if re.search(rf"\b{word}\b", s):
            return n
    return None


def size_bucket(hint: str | None) -> str | None:
    n = parse_employee_count(hint)
    if n is None:
        return None
    for limit, label in ((5, "1-4"), (10, "5-9"), (20, "10-19"), (50, "20-49")):
        if n < limit:
            return label
    return "50+"


def age_bucket(founded_year: int | None, today: dt.date | None = None) -> str | None:
    if not founded_year:
        return None
    years = (today or dt.date.today()).year - int(founded_year)
    if years < 0:
        return None
    if years < 10:
        return "<10 Jahre"
    if years < 25:
        return "10-24 Jahre"
    if years < 50:
        return "25-49 Jahre"
    return "50+ Jahre"


def plz_zone(postal_code: str | None) -> str | None:
    d = re.sub(r"\D", "", str(postal_code or ""))
    return f"PLZ {d[0]}x" if len(d) == 5 else None


def _ad_presence_map(ids: list[int] | None = None) -> dict[int, str]:
    """company_id -> 'aktiv' | 'keine' from each company's LATEST fetched week
    (summed across sources). Companies never fetched are absent — UNKNOWN, so
    the feature is skipped for them rather than counted as 'keine'."""
    with SessionLocal() as s:
        from sqlalchemy import and_ as _and
        latest = (select(WeeklyCompanyMetric.company_id,
                         func.max(WeeklyCompanyMetric.week_start).label("wk"))
                  .group_by(WeeklyCompanyMetric.company_id))
        if ids:
            latest = latest.where(WeeklyCompanyMetric.company_id.in_(ids))
        latest = latest.subquery()
        q = (select(WeeklyCompanyMetric.company_id, func.sum(WeeklyCompanyMetric.total_active_ads))
             .join(latest, _and(WeeklyCompanyMetric.company_id == latest.c.company_id,
                                WeeklyCompanyMetric.week_start == latest.c.wk))
             .group_by(WeeklyCompanyMetric.company_id))
        return {cid: ("aktiv" if (n or 0) > 0 else "keine") for cid, n in s.execute(q)}


def company_features(c: Company, ads: dict[int, str]) -> dict:
    """The feature values of one company; None/[] = unknown -> feature skipped."""
    return {
        "segment": c.segment or None,
        "sub_segment": c.sub_segment or None,
        "sales_channel": c.sales_channel or None,
        "plz_zone": plz_zone(c.postal_code),
        "products": list(c.products) if c.products else [],
        "size_bucket": size_bucket(c.employee_hint),
        "age_bucket": age_bucket(c.founded_year),
        "ad_presence": ads.get(c.id),
    }


# ---------------------------------------------------------------------------
# profile building
# ---------------------------------------------------------------------------

def build_profile(filters: dict | None = None, name: str = "ICP") -> dict:
    """Compute the winners' feature distributions for a chosen filter (default:
    everyone who ever bought — customer_state active/new/lapsed). Returns the
    profile as a dict WITHOUT saving it; apply_profile() persists + scores."""
    from ..customers import _apply_filters

    filters = dict(filters or {})
    if not any(filters.values()):
        # Default winners: the companies buying NOW (active + new), minus the
        # segments that are not partner material at all. Deliberately NOT
        # "everyone who ever bought" — in this dataset that is the entire
        # population (it's a customer export, never-bought = 0), and a profile
        # built from everyone just mirrors the population and ranks nothing.
        filters = {"customer_state": ["active", "new"],
                   "exclude_segment": list(WINNER_EXCLUDED_SEGMENTS)}
    elif not filters.get("include_consumers"):
        # An explicit filter is respected, but consumers never DEFINE the profile.
        # Keyed on include_consumers, not on whether ids were supplied: a
        # hand-picked id list used to be treated as consent, which is exactly the
        # kind of "explicit enough" door that kept 36% of the base leaking into
        # counts nobody meant to include. Any exclusion the caller set is kept and
        # extended, not replaced.
        already = list(filters.get("exclude_segment") or [])
        filters["exclude_segment"] = already + [s for s in WINNER_EXCLUDED_SEGMENTS if s not in already]

    with SessionLocal() as s:
        stmt = _apply_filters(select(Company), filters)
        if not filters.get("ids"):
            # own-group companies never define the profile (see
            # customers.INTERCOMPANY_NAME_PATTERNS) — they are large, look ideal,
            # and would teach the model to seek out its own subsidiaries
            stmt = stmt.where(Company.is_intercompany.is_(False))
        winners = list(s.scalars(stmt))
    ads = _ad_presence_map([c.id for c in winners]) if winners else {}

    dists: dict[str, dict] = {}
    for feat in DEFAULT_WEIGHTS:
        counts: dict[str, int] = {}
        known = 0
        for c in winners:
            value = company_features(c, ads)[feat]
            if feat == "products":
                if value:
                    known += 1
                    for p in value:
                        counts[p] = counts.get(p, 0) + 1
            elif value:
                known += 1
                counts[value] = counts.get(value, 0) + 1
        shares = {v: n / known for v, n in counts.items()} if known else {}
        dists[feat] = {
            "label": _FEATURE_LABEL_DE[feat],
            "coverage": round(known / len(winners), 3) if winners else 0,  # share of winners with a known value
            "top": sorted(shares.items(), key=lambda kv: -kv[1])[:8],
            "shares": shares,
        }
    return {"name": name, "winners_filter": filters, "winners_count": len(winners),
            "features": dists, "weights": dict(DEFAULT_WEIGHTS)}


# ---------------------------------------------------------------------------
# fit scoring
# ---------------------------------------------------------------------------

# Shared by SCORING (fit_for) and DIAGNOSIS (diagnose): a feature known for fewer
# than this share of winners proves nothing, however cleanly it seems to separate
# those few rows. Defined here because scoring is the more important consumer —
# diagnose only reports, fit_for changes every number in the app.
_MIN_COVERAGE = 0.15


def fit_for(features: dict, profile: dict) -> tuple[float | None, list[dict]]:
    """(fit 0-100, per-feature breakdown) of one company against a profile.
    Per feature: points = winners' share of the company's value, normalised by
    the feature's modal share (so the most typical winner value = 1.0).
    Unknown company values are skipped and weights renormalised. Returns
    (None, []) when NOTHING was comparable."""
    weights = profile.get("weights") or DEFAULT_WEIGHTS
    breakdown: list[dict] = []
    total_w = scored = 0.0
    for feat, weight in weights.items():
        dist = (profile.get("features") or {}).get(feat) or {}
        shares = dist.get("shares") or {}
        if not shares:
            continue                      # winners themselves had no data here
        # Too thinly known to score with. diagnose() already refuses to trust a
        # feature below this coverage, but fit_for used it anyway — so
        # Betriebsgröße (known for 3% of winners, i.e. a distribution built from
        # ~20 companies) was shaping EVERY company's fit score. Warning about a
        # number and then scoring with it is worse than not having it.
        if (dist.get("coverage") or 0) < _MIN_COVERAGE:
            continue
        max_share = max(shares.values())
        if max_share >= 0.9 and feat != "products":
            # non-discriminating: ~every winner has the same value (live case:
            # Vertriebsweg is 99% 'Fachhandelsvertrieb'). Matching it says
            # nothing about fit, so it must not inflate anyone's score.
            continue
        value = features.get(feat)
        if feat == "products":
            if not value:
                continue
            pts = sum(shares.get(p, 0.0) for p in value) / (len(value) * max_share)
            shown = ", ".join(value[:4])
        else:
            if not value:
                continue
            pts = shares.get(value, 0.0) / max_share
            shown = value
        pts = min(pts, 1.0)
        total_w += weight
        scored += weight * pts
        breakdown.append({"feature": feat, "label": dist.get("label", feat),
                          "value": shown, "points": round(pts, 3), "weight": weight})
    if total_w <= 0:
        return None, []
    fit = round(100.0 * scored / total_w, 1)
    breakdown.sort(key=lambda b: -(b["points"] * b["weight"]))
    return fit, breakdown


def apply_profile(filters: dict | None = None, name: str = "ICP") -> dict:
    """Build the profile, SAVE it, and score the WHOLE company base against it:
    fit_score (everyone with any comparable data), opportunity_score (Divergenz,
    only where ad data exists), target_score = geometric mean where both exist.
    Pure local computation — no network, no API cost."""
    from .divergence import compute_divergence

    profile = build_profile(filters, name=name)
    if profile["winners_count"] < MIN_WINNERS_USABLE:
        raise ValueError(
            f"Nur {profile['winners_count']} Gewinner im Filter — unter "
            f"{MIN_WINNERS_USABLE} sind die Verteilungen Rauschen. Filter weiter "
            "fassen (z. B. Land oder Segment nicht einschränken).")

    opp = {r["company_id"]: r["divergence"] for r in compute_divergence()["rows"]}
    now = dt.datetime.utcnow()
    scored = with_target = 0

    with SessionLocal() as s:
        row = IcpProfile(name=name, winners_filter=profile["winners_filter"],
                         winners_count=profile["winners_count"],
                         features=profile["features"], weights=profile["weights"],
                         applied_at=now)
        s.add(row)
        s.flush()
        profile_id = row.id

        ads = _ad_presence_map()
        # Consumers are out of scope entirely (adwatch/scope.py) — they are not
        # companies that can be acquired, so they get no scores at all rather
        # than a score that would then have to be filtered out of every view.
        for c in s.scalars(scope.apply(select(Company))):
            fit, breakdown = fit_for(company_features(c, ads), profile)
            c.fit_score = fit
            c.fit_breakdown = {"profile_id": profile_id, "features": breakdown} if breakdown else None
            c.opportunity_score = opp.get(c.id)
            # An own-group company can never be "acquired" — it keeps its
            # descriptive scores but is removed from the ranked call list.
            if c.is_intercompany:
                c.target_score = None
            elif fit is not None and c.opportunity_score is not None:
                c.target_score = round((fit * c.opportunity_score) ** 0.5, 1)
                with_target += 1
            else:
                c.target_score = None
            c.scores_updated_at = now
            if fit is not None:
                scored += 1
        s.commit()

    return {"profile_id": profile_id, "name": name,
            "winners_count": profile["winners_count"],
            "companies_scored": scored, "with_target_score": with_target,
            "applied_at": now.isoformat(timespec="minutes")}


# ---------------------------------------------------------------------------
# Validity check — "an ICP for ANY filter, but only the ones that make sense,
# and say why". A profile can be built from any winners set; whether it can
# RANK anything is a different question. Four failure modes, all measurable:
#
#   too few winners      -> the distributions are noise
#   not discriminating   -> winners look like the population; every fit ~equal
#   mixed population     -> the set spans two incompatible groups (Handel vs
#                           Verarbeiter, DE vs ES); the blend fits neither
#   no feature data      -> only 1-2 features carry values at all
#
# Thresholds are deliberately conservative and named, not hidden in a formula.
# ---------------------------------------------------------------------------

MIN_WINNERS_USABLE = 30      # below this: indicative only
MIN_WINNERS_SOLID = 80       # at/above this: stable distributions
_TVD_NO_SIGNAL = 0.05        # winners vs population distance below this = noise
_SPLIT_GAP = 25              # self-score minus cross-score above this = mixed set


def _distribution(companies: list[Company], feat: str, ads: dict) -> dict[str, float]:
    counts: dict[str, int] = {}
    known = 0
    for c in companies:
        value = company_features(c, ads)[feat]
        if feat == "products":
            if value:
                known += 1
                for p in value:
                    counts[p] = counts.get(p, 0) + 1
        elif value:
            known += 1
            counts[value] = counts.get(value, 0) + 1
    return {v: n / known for v, n in counts.items()} if known else {}


def _tvd(a: dict[str, float], b: dict[str, float]) -> float:
    """Total variation distance between two distributions (0 = identical,
    1 = disjoint). Readable as 'how different are winners from the population'."""
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def diagnose(filters: dict | None = None) -> dict:
    """Judge whether a winners filter yields a profile worth trusting.
    Returns {verdict, winners, population, reasons[], features[], splits[]}
    where verdict is 'ok' | 'weak' | 'unusable'. Pure computation, no writes."""
    from ..customers import _apply_filters

    profile = build_profile(filters)
    win_filter = profile["winners_filter"]
    # The honest baseline: the same population the winners were drawn from,
    # i.e. the identical filter WITHOUT the buying condition.
    pop_filter = {k: v for k, v in win_filter.items() if k != "customer_state"}

    with SessionLocal() as s:
        winners = list(s.scalars(_apply_filters(select(Company), win_filter)
                                 .where(Company.is_intercompany.is_(False))))
        population = list(s.scalars(_apply_filters(select(Company), pop_filter)
                                    .where(Company.is_intercompany.is_(False))))
    ads = _ad_presence_map()

    # --- per-feature: does it separate winners from the population? ---
    features = []
    for feat, weight in (profile.get("weights") or DEFAULT_WEIGHTS).items():
        w_dist = _distribution(winners, feat, ads)
        p_dist = _distribution(population, feat, ads)
        cov = profile["features"][feat]["coverage"]
        tvd = _tvd(w_dist, p_dist)
        top = sorted(w_dist.items(), key=lambda kv: -kv[1])[:1]
        lift = (top[0][1] / p_dist.get(top[0][0], 1e-9)) if top else 0.0
        features.append({
            "feature": feat, "label": _FEATURE_LABEL_DE[feat], "weight": weight,
            "coverage": round(cov, 3), "separation": round(tvd, 3),
            "top_value": top[0][0] if top else None, "lift": round(min(lift, 99), 2),
            "usable": cov >= _MIN_COVERAGE and tvd >= _TVD_NO_SIGNAL,
        })

    # --- is the winners set secretly two populations? ---
    splits = []
    for dim, getter in (("country", lambda c: c.country),
                        ("segment", lambda c: c.segment),
                        ("sales_channel", lambda c: c.sales_channel)):
        groups: dict[str, list[Company]] = {}
        for c in winners:
            key = getter(c)
            if key:
                groups.setdefault(key, []).append(c)
        big = sorted((g for g in groups.items() if len(g[1]) >= 15), key=lambda kv: -len(kv[1]))[:2]
        if len(big) < 2:
            continue
        (na, ga), (nb, gb) = big
        pa = build_profile({"ids": [c.id for c in ga]})
        pb = build_profile({"ids": [c.id for c in gb]})
        def med(pool, prof):
            vals = [fit_for(company_features(c, ads), prof)[0] for c in pool]
            vals = [v for v in vals if v is not None]
            return round(sorted(vals)[len(vals) // 2], 1) if vals else 0.0
        gap = max(med(ga, pa) - med(ga, pb), med(gb, pb) - med(gb, pa))
        splits.append({"dimension": dim, "group_a": na, "n_a": len(ga),
                       "group_b": nb, "n_b": len(gb), "gap": round(gap, 1),
                       "should_split": gap >= _SPLIT_GAP})

    # --- verdict ---
    reasons: list[str] = []
    n = len(winners)
    verdict = "ok"
    if n < MIN_WINNERS_USABLE:
        verdict = "unusable"
        reasons.append(f"Nur {n} Gewinner — unter {MIN_WINNERS_USABLE} sind die Verteilungen Rauschen.")
    elif n < MIN_WINNERS_SOLID:
        verdict = "weak"
        reasons.append(f"{n} Gewinner — verwertbar, aber erst ab {MIN_WINNERS_SOLID} stabil.")
    usable = [f for f in features if f["usable"]]
    if not usable:
        verdict = "unusable"
        reasons.append("Kein Merkmal trennt die Gewinner von der Grundgesamtheit — "
                       "dieses Profil kann nicht ranken.")
    elif len(usable) <= 2:
        verdict = "unusable" if verdict == "unusable" else "weak"
        reasons.append(f"Nur {len(usable)} von {len(features)} Merkmalen tragen Signal "
                       f"({', '.join(f['label'] for f in usable)}) — meist fehlen die Daten (Anreicherung).")
    thin = [f["label"] for f in features
            if f["coverage"] < _MIN_COVERAGE and f["separation"] >= _TVD_NO_SIGNAL]
    if thin:
        reasons.append(f"Zu dünn belegt, daher ignoriert: {', '.join(thin)} "
                       f"(unter {int(_MIN_COVERAGE * 100)}% der Gewinner haben einen Wert) — "
                       "die Anreicherung würde genau diese Merkmale nutzbar machen.")
    _DIM_LABEL = {"country": "Land", "segment": "Kundensegment", "sales_channel": "Vertriebsweg"}
    for sp in splits:
        if sp["should_split"]:
            verdict = "weak" if verdict == "ok" else verdict
            a, b, gap = sp["group_a"], sp["group_b"], sp["gap"]
            dim = _DIM_LABEL.get(sp["dimension"], sp["dimension"])
            reasons.append(f"Gemischte Grundgesamtheit: {a} und {b} unterscheiden sich um "
                           f"{gap} Punkte — besser getrennt nach {dim} bilden.")
    if verdict == "ok" and not reasons:
        reasons.append(f"{n} Gewinner, {len(usable)} tragende Merkmale, keine gemischte "
                       "Grundgesamtheit erkannt.")

    return {"verdict": verdict, "winners": n, "population": len(population),
            "reasons": reasons, "features": features, "splits": splits,
            "winners_filter": win_filter}


def latest_profile() -> dict | None:
    with SessionLocal() as s:
        row = s.scalar(select(IcpProfile).order_by(IcpProfile.created_at.desc()).limit(1))
        if not row:
            return None
        return {"id": row.id, "name": row.name, "winners_filter": row.winners_filter,
                "winners_count": row.winners_count, "features": row.features,
                "weights": row.weights,
                "created_at": row.created_at.isoformat(timespec="minutes"),
                "applied_at": row.applied_at.isoformat(timespec="minutes") if row.applied_at else None}
