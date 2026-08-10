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
    "crm_products": 2.0,
    "size_bucket": 1.0,
    "age_bucket": 0.5,
    "ad_presence": 1.0,
}

# Features whose value is a LIST, not a single value: a company can deal in
# several product families at once, so they are counted per element and scored
# as the mean lift over the elements we have a trusted lift for.
_LIST_FEATURES = ("products", "crm_products")

# Segments that must NEVER define the profile: they are not the kind of company
# the partner program acquires. "Private Endkunden" are consumers — 332 of them
# bought something, which silently made them 32% of the default winners set and
# dragged the trade-partner profile toward consumer traits. The Explorer already
# hides them from the VIEW; the winners definition has to exclude them too.
WINNER_EXCLUDED_SEGMENTS = ("Private Endkunden",)

_FEATURE_LABEL_DE = {
    "segment": "Kundensegment", "sub_segment": "Untersegment",
    "sales_channel": "Vertriebsweg", "plz_zone": "PLZ-Zone",
    "products": "Produkte (Website)", "crm_products": "Produktfamilien (CRM)",
    "size_bucket": "Betriebsgröße",
    "age_bucket": "Firmenalter", "ad_presence": "Anzeigen-Aktivität",
}

# ---------------------------------------------------------------------------
# Availability leakage
# ---------------------------------------------------------------------------
# A feature can separate winners from everyone else for the wrong reason: it is
# simply KNOWN more often for winners. Two live cases in this app:
#
#   size_bucket   comes from website enrichment, and enrichment was run on the
#                 monitored base — which was the old buyers-only Excel export.
#   ad_presence   exists only for the ~580 companies with a linked ad page, and
#                 those were chosen from that same buyer base.
#
# Both then "predict" buying with high confidence while carrying no information
# about fit at all. The equivalent trap in CRM is `numberofemployees`: filled on
# 467 of 15,235 dealers, buy rate 47-75% where filled vs 20.7% where not, because
# a rep fills it in for accounts they are already working. It is deliberately NOT
# imported, and tests assert it stays out.
#
# A feature is dropped from SCORING when it is known for winners far more often
# than for the population being scored. Reporting it in diagnose() is still
# useful — knowing the leak exists is what stops someone re-adding the feature.
# 2.5 was too loose, and `crm_products` proved it. Its family list is known for
# 92.4% of buyers and 50.4% of the whole base — ratio 1.83, so it passed the
# guard, and the whole-base backtest promptly jumped to lift 2.71 with
# ranks=True while Handel+Verarbeiter ALONE fell to 0.75, i.e. worse than
# random. The feature was re-learning "this account has been worked", which
# separates architects from dealers and nothing else — precisely the artefact
# backtest() was written to catch. At 1.5 it is dropped on the raw base and kept
# inside an engaged population, where its availability ratio is 1.04.
# Nothing else in the current feature set is affected: the next highest ratio is
# PLZ-Zone at 1.14.
_LEAK_RATIO = 1.5      # winners' coverage this many times the population's
_LEAK_POP_FLOOR = 0.6  # ...and the population is not broadly covered anyway

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


def crm_product_map(as_of: dt.date | None = None) -> dict[int, list[str]]:
    """company_id -> the Solarlux product families that company deals in.

    From `crm_company_products` (the slx_product ACCOUNT link — a catalogue
    relationship, so it says which families a firm deals in, never how much it
    bought; the euros live on the opportunity link). 23.431 companies against
    1.207 for the website-derived `companies.products`, which is why this exists.

    `as_of` restricts to families whose `first_seen` is on or before that date —
    the "knowable in time" gate. Rows with no `first_seen` (2.920 of 38.430) are
    EXCLUDED when a cut is given: we cannot show they were known then, and
    assuming they were is how a backtest flatters itself.

    Availability warning, measured 2026-08-10 and the reason this is not a
    free win: across the whole base the family list is known for 92,4% of buyers
    and 46,7% of everyone else (1,98x) — it is populated by working the account.
    Restricted to firms that HAVE a Verkaufschance the ratio falls to 1,04
    (98,5% vs 94,9%), i.e. the leak is engagement, not fit. So this feature is
    sound inside an engaged population and leaky on the raw base; the existing
    `leaky` check is what enforces that, and _LEAK_RATIO is set to catch it.
    """
    from ..models import CrmCompanyProduct
    out: dict[int, list[str]] = {}
    with SessionLocal() as s:
        stmt = select(CrmCompanyProduct.company_id, CrmCompanyProduct.family,
                      CrmCompanyProduct.first_seen)
        for cid, family, first_seen in s.execute(stmt):
            if not family:
                continue
            if as_of is not None and (first_seen is None or first_seen > as_of):
                continue
            out.setdefault(cid, []).append(family)
    return {k: sorted(set(v)) for k, v in out.items()}


def company_features(c: Company, ads: dict[int, str],
                     crm_products: dict[int, list[str]] | None = None) -> dict:
    """The feature values of one company; None/[] = unknown -> feature skipped."""
    return {
        "segment": c.segment or None,
        "sub_segment": c.sub_segment or None,
        "sales_channel": c.sales_channel or None,
        "plz_zone": plz_zone(c.postal_code),
        "products": list(c.products) if c.products else [],
        "crm_products": list((crm_products or {}).get(c.id) or []),
        "size_bucket": size_bucket(c.employee_hint),
        "age_bucket": age_bucket(c.founded_year),
        "ad_presence": ads.get(c.id),
    }


# ---------------------------------------------------------------------------
# profile building
# ---------------------------------------------------------------------------

def material_buyer_ids(min_eur: float | None = None) -> list[int]:
    """Companies with at least one order EVENT at or above the materiality floor.

    Uses CrmOrderEvent (Belege collapsed per company+day) rather than Company
    revenue columns, and `max(amount)` rather than a sum: one real system order is
    what makes a company a system customer. Summing hundreds of small orders to
    clear the floor would readmit exactly the spare-parts buyers the floor exists
    to keep out.
    """
    from ..models import CrmOrderEvent
    from .rfm import MATERIAL_EUR
    floor = MATERIAL_EUR if min_eur is None else min_eur
    with SessionLocal() as s:
        q = (select(CrmOrderEvent.company_id)
             .group_by(CrmOrderEvent.company_id)
             .having(func.max(CrmOrderEvent.amount) >= floor))
        return [cid for (cid,) in s.execute(q)]


def _population_stats(feats: tuple[str, ...], pop_ids: list[int] | None = None,
                      as_of: dt.date | None = None) -> dict:
    """Per feature: how often it is known in the SCORED population, and how the
    population distributes across its values.

    Both halves are needed. Coverage catches availability leakage (see _LEAK_RATIO
    above). The value distribution is what turns a winners' share into a LIFT —
    without it the profile can only say "this value is common among customers",
    which is not the same as "this value predicts becoming one".
    """
    with SessionLocal() as s:
        stmt = scope.apply(select(Company))
        if pop_ids is not None:
            stmt = stmt.where(Company.id.in_(pop_ids))
        pop = list(s.scalars(stmt))
    ads = _ad_presence_map()
    crm_prod = crm_product_map(as_of)
    out: dict[str, dict] = {}
    for f in feats:
        counts: dict[str, int] = {}
        known = 0
        for c in pop:
            v = company_features(c, ads, crm_prod)[f]
            if f in _LIST_FEATURES:
                if v:
                    known += 1
                    for p in v:
                        counts[p] = counts.get(p, 0) + 1
            elif v:
                known += 1
                counts[v] = counts.get(v, 0) + 1
        out[f] = {"coverage": (known / len(pop)) if pop else 0.0,
                  "counts": counts, "known": known}
    return out


# Laplace smoothing when turning counts into a lift. Without it a value held by
# three winners and three population rows would read as a perfect 1.0 predictor,
# and a value no winner happens to have would read as lift 0 = "impossible".
_SMOOTH = 2.0

# A value must appear at least this often in the population before its lift is
# trusted for scoring. Rare values produce enormous lifts from tiny numerators.
_MIN_VALUE_SUPPORT = 15

# Lift is clamped into this band before scoring. The cap stops one extreme value
# dominating the weighted sum; the floor keeps a "never buys" value informative
# without letting it veto everything else about a company.
_LIFT_CLAMP = (0.2, 5.0)


def build_profile(filters: dict | None = None, name: str = "ICP",
                  as_of: dt.date | None = None,
                  pop_ids: list[int] | None = None) -> dict:
    """Compute the winners' feature distributions for a chosen filter (default:
    everyone who ever bought — customer_state active/new/lapsed). Returns the
    profile as a dict WITHOUT saving it; apply_profile() persists + scores."""
    from ..customers import _apply_filters

    filters = dict(filters or {})
    if not any(filters.values()):
        # Default winners: companies with a MATERIAL purchase on record.
        #
        # This used to be customer_state active/new, derived from the
        # revenue_y0..y4 snapshot — a CRM field filled on 2.9% of accounts. That
        # was defensible only while the base WAS the buyers-only Excel export.
        # Now that the full population is loaded, the snapshot marks almost
        # nobody and the Belege are the authoritative source.
        #
        # "Material" matters as much as "bought": ~25% of Belege are 0 EUR and the
        # median is 194 EUR, so a spare-parts order would otherwise make a company
        # a winner and teach the profile the traits of a gasket buyer.
        ids = material_buyer_ids()
        if len(ids) >= MIN_WINNERS_USABLE:
            filters = {"ids": ids,
                       "exclude_segment": list(WINNER_EXCLUDED_SEGMENTS)}
        else:
            # No Beleg data loaded yet — fall back so a fresh DB still works.
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
        # Own-group companies never define the profile (see
        # customers.INTERCOMPANY_NAME_PATTERNS) — they are large, look ideal, and
        # would teach the model to seek out its own subsidiaries.
        #
        # This used to be skipped whenever the filter carried `ids`, which is
        # exactly the DEFAULT path: material_buyer_ids() returns ids, so the
        # guard never ran on the profile the app actually builds. Measured
        # 2026-08-10: 7 of the 8 flagged intercompany companies were in the
        # default winners set. An id list is a choice of population, never
        # consent to train on our own companies.
        stmt = _apply_filters(select(Company), filters).where(
            Company.is_intercompany.is_(False))
        winners = list(s.scalars(stmt))
    ads = _ad_presence_map([c.id for c in winners]) if winners else {}
    crm_prod = crm_product_map(as_of)

    dists: dict[str, dict] = {}
    for feat in DEFAULT_WEIGHTS:
        counts: dict[str, int] = {}
        known = 0
        for c in winners:
            value = company_features(c, ads, crm_prod)[feat]
            if feat in _LIST_FEATURES:
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

    # ---- population comparison: leakage check + LIFT per value ----
    # The reference population for the leakage check and for every lift is the
    # population this profile will SCORE. Defaulting it to the whole base is
    # right for apply_profile, and wrong for a profile built to rank inside a
    # restricted set: crm_products is 50% covered across all 46k companies but
    # 96% covered among firms we have actually engaged, so judged against the
    # wrong reference it is dropped as leaky exactly where it is sound.
    pstats = _population_stats(tuple(DEFAULT_WEIGHTS), pop_ids=pop_ids, as_of=as_of)
    for feat, dist in dists.items():
        ps = pstats.get(feat) or {"coverage": 0.0, "counts": {}, "known": 0}
        pc, wc = ps["coverage"], dist["coverage"]
        dist["pop_coverage"] = round(pc, 3)
        ratio = (wc / pc) if pc > 0 else (float("inf") if wc > 0 else 0.0)
        dist["availability_ratio"] = (round(ratio, 2) if ratio != float("inf")
                                      else None)
        dist["leaky"] = bool(wc > 0 and pc < _LEAK_POP_FLOOR
                             and (pc == 0 or ratio >= _LEAK_RATIO))

        # lift(value) = P(winner | value) / P(winner), computed from the two
        # distributions by Bayes: (winner share of value) / (population share).
        # This is the number that replaced winners'-share scoring, because a
        # share only says "common among customers". Measured case: Bauelemente-
        # handel is 36.5% of winners but converts at 1.03x, while Wintergartenbau
        # is ~1% of winners and converts at 1.62x — share ranked them backwards.
        p_known = ps["known"] or 0
        w_known = max(int(round(dist["coverage"] * len(winners))), 0) if winners else 0
        k = max(len(set(dist["shares"]) | set(ps["counts"])), 1)
        lifts: dict[str, float] = {}
        support: dict[str, int] = {}
        for value, w_share in (dist["shares"] or {}).items():
            p_count = ps["counts"].get(value, 0)
            support[value] = p_count
            # Never demand more support than a tenth of the data — otherwise the
            # floor that protects a 46,000-row population from noise silently
            # refuses to score anything at all on a small base.
            floor = max(3, min(_MIN_VALUE_SUPPORT, p_known // 10))
            if p_count < floor or not p_known or not w_known:
                continue
            w_rate = (w_share * w_known + _SMOOTH) / (w_known + _SMOOTH * k)
            p_rate = (p_count + _SMOOTH) / (p_known + _SMOOTH * k)
            if p_rate <= 0:
                continue
            lifts[value] = round(min(max(w_rate / p_rate, _LIFT_CLAMP[0]),
                                     _LIFT_CLAMP[1]), 4)
        dist["lifts"] = lifts
        dist["support"] = support
        dist["top_lift"] = sorted(lifts.items(), key=lambda kv: -kv[1])[:8]

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
    import math
    weights = profile.get("weights") or DEFAULT_WEIGHTS
    breakdown: list[dict] = []
    total_w = raw = 0.0
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
        # Known for winners far more often than for the population — it separates
        # them because of who got enriched/monitored, not because of fit. Scoring
        # with it produces a confident model that says "accounts we already sell
        # to, buy from us". diagnose() still reports it so the leak stays visible.
        if dist.get("leaky"):
            continue
        lifts = dist.get("lifts")
        if lifts is None:
            # profile saved before lift scoring existed — refuse to score rather
            # than silently fall back to the share method this replaced
            continue
        max_share = max(shares.values())
        if max_share >= 0.9 and feat not in _LIST_FEATURES:
            # non-discriminating: ~every winner has the same value (live case:
            # Vertriebsweg is 99% 'Fachhandelsvertrieb'). Matching it says
            # nothing about fit, so it must not inflate anyone's score.
            continue
        value = features.get(feat)
        if feat in _LIST_FEATURES:
            if not value:
                continue
            known = [lifts[p] for p in value if p in lifts]
            if not known:
                continue
            lift = sum(known) / len(known)
            shown = ", ".join(value[:4])
        else:
            if not value or value not in lifts:
                # unknown, or too rare in the population to have a trusted lift
                continue
            lift = lifts[value]
            shown = value
        total_w += weight
        raw += weight * math.log(lift)
        breakdown.append({"feature": feat, "label": dist.get("label", feat),
                          "value": shown, "lift": round(lift, 2),
                          "points": round(lift, 3), "weight": weight})
    if total_w <= 0:
        return None, []
    # Weighted mean log-lift, squashed to 0-100. The squash is monotonic, so it
    # changes no ranking — it only makes the number readable. 50 = exactly the
    # base rate, above 50 = more likely than average to buy, below = less.
    mean_log = raw / total_w
    fit = round(100.0 / (1.0 + math.exp(-mean_log)), 1)
    breakdown.sort(key=lambda b: -(abs(math.log(max(b["points"], 1e-9))) * b["weight"]))
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
        crm_prod = crm_product_map()
        # Consumers are out of scope entirely (adwatch/scope.py) — they are not
        # companies that can be acquired, so they get no scores at all rather
        # than a score that would then have to be filtered out of every view.
        for c in s.scalars(scope.apply(select(Company))):
            fit, breakdown = fit_for(company_features(c, ads, crm_prod), profile)
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


def _distribution(companies: list[Company], feat: str, ads: dict,
                  crm_prod: dict | None = None) -> dict[str, float]:
    counts: dict[str, int] = {}
    known = 0
    for c in companies:
        value = company_features(c, ads, crm_prod)[feat]
        if feat in _LIST_FEATURES:
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
    where verdict is 'ok' | 'weak' | 'unusable'. Pure computation, no writes.

    The baseline used to be built by stripping `customer_state` from the winners
    filter. That was right while the default winners set WAS
    {"customer_state": [...]}. When the default moved to the Belege —
    {"ids": material_buyer_ids(), ...} — the strip stopped matching anything, so
    population == winners and every feature reported separation 0.000 with the
    verdict "Kein Merkmal trennt die Gewinner von der Grundgesamtheit". Measured
    2026-08-10: diagnose(None) returned 3.781 winners against 3.781 population.
    The panel had been saying "this cannot rank" for a structural reason, for
    any input whatsoever.

    The buying condition is now named explicitly rather than guessed at, and the
    two sets are built the way filter_trust already builds them:

      caller passed a market slice (country/segment/...) -> population is that
        slice, winners are the material buyers inside it. This is what someone
        asking "can I build an ICP for German dealers?" means.
      caller passed `ids` -> they named the winners themselves, so the baseline
        is the whole in-scope business.
      no filter -> winners are the material buyers, population is everything in
        scope.
    """
    from ..customers import _apply_filters

    # Keys that SELECT ON THE OUTCOME rather than describe a market. They say who
    # won; they must not also decide who could have.
    _OUTCOME_KEYS = ("ids", "customer_state")
    given = dict(filters or {})
    # If the caller expressed a buying condition themselves — an explicit id list
    # or customer_state — that IS the winners definition and is honoured. Only
    # when they described a pure market slice (or nothing) do we supply the
    # condition, and then it is the same one filter_trust uses: a material order.
    caller_named_winners = any(given.get(k) for k in _OUTCOME_KEYS)
    pop_filter = {k: v for k, v in given.items() if k not in _OUTCOME_KEYS}

    with SessionLocal() as s:
        population = list(s.scalars(
            scope.apply(_apply_filters(select(Company), pop_filter))
            .where(Company.is_intercompany.is_(False))))
        if caller_named_winners:
            winners = list(s.scalars(_apply_filters(select(Company), given)
                                     .where(Company.is_intercompany.is_(False))))
        else:
            buyers = set(material_buyer_ids())
            winners = [c for c in population if c.id in buyers]

    # The profile has to come from the WINNERS, not from the caller's filter —
    # for a market slice those are different sets, and building from the slice is
    # what made the comparison self-referential.
    win_ids = [c.id for c in winners]
    profile = (build_profile(given) if caller_named_winners
               else build_profile({"ids": win_ids,
                                   "exclude_segment": list(WINNER_EXCLUDED_SEGMENTS)})
               if len(win_ids) >= MIN_WINNERS_USABLE else build_profile(given or None))
    win_filter = profile["winners_filter"]
    ads = _ad_presence_map()
    crm_prod = crm_product_map()

    # --- per-feature: does it separate winners from the population? ---
    features = []
    for feat, weight in (profile.get("weights") or DEFAULT_WEIGHTS).items():
        w_dist = _distribution(winners, feat, ads, crm_prod)
        p_dist = _distribution(population, feat, ads, crm_prod)
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
            vals = [fit_for(company_features(c, ads, crm_prod), prof)[0] for c in pool]
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


def filter_trust(filters: dict | None = None, cut: dt.date | None = None) -> dict:
    """Can an ICP built from THIS filter be trusted? The on-demand guardrail.

    "Enough positives" is necessary but not sufficient — this project hit every
    other failure mode an on-demand filter can produce, so each is checked:

      * positives         < 30 unusable, < 150 indicative only (noise floors we
                          measured; below them distributions are decoration)
      * base rate         near 100% inside the filter = the 87% trap: everyone a
                          buyer, nothing to discriminate (the original Excel base)
      * near 0% = nothing to learn FROM either
      * outcome leakage   a filter that selects on the outcome ("has ads" ->
                          profile 'predicts' ads) can't be caught mechanically in
                          general, but ad/enrichment-derived filters are flagged
      * feature collapse  filtering ON the best feature makes it uniform inside
                          the filter (Wintergartenbau-only -> sub_segment says
                          nothing anymore); reported so the drop in power is
                          expected rather than mysterious
      * backtest          the final word: does it actually RANK within the
                          filter, on a time split (lift@decile + monotonicity)

    Verdict: 'green' (rank and use), 'yellow' (directional — use as a scorecard,
    not an ordering), 'red' (do not rank; use plain filters). Written for the
    Profil tab so a colleague sees the verdict BEFORE trusting a score.
    """
    from ..customers import _apply_filters
    from ..models import CrmOrderEvent
    from .rfm import MATERIAL_EUR

    filters = dict(filters or {})
    reasons: list[str] = []
    flags: list[str] = []

    # filters derived from ad or enrichment activity select on engagement — the
    # profile then "discovers" the funnel that produced the filter
    for k in filters:
        if k in ("resolution_status", "enrichment_status", "has_ads", "advertising"):
            flags.append(f"Filter '{k}' basiert auf unserer eigenen Aktivität — "
                         "das Profil würde den eigenen Funnel 'entdecken'")

    with SessionLocal() as s:
        pop = list(s.scalars(_apply_filters(scope.apply(select(Company)), filters)
                             .where(Company.is_intercompany.is_(False))))
        events: dict[int, float] = {}
        for cid, amt in s.execute(select(CrmOrderEvent.company_id,
                                         func.max(CrmOrderEvent.amount))
                                  .group_by(CrmOrderEvent.company_id)):
            events[cid] = float(amt or 0)

    positives = [c for c in pop if events.get(c.id, 0.0) >= MATERIAL_EUR]
    n_pop, n_pos = len(pop), len(positives)
    base = (n_pos / n_pop) if n_pop else 0.0

    if n_pos < MIN_WINNERS_USABLE:
        reasons.append(f"nur {n_pos} Gewinner im Filter — unter {MIN_WINNERS_USABLE} "
                       "sind Verteilungen Rauschen; Scorecard statt Statistik verwenden")
    elif n_pos < 150:
        flags.append(f"{n_pos} Gewinner — indikativ, nicht stabil (ab ~150 belastbar)")
    else:
        # Events-per-variable guidance (Peduzzi 1996: >=10 EPV for stable
        # estimates; Harrell suggests 15-20). Our scoring is per-category lift
        # with smoothing, which is more forgiving than raw logistic regression —
        # and 'green' is ultimately decided by the OUT-OF-TIME backtest below,
        # which van Smeden et al. argue matters more than any EPV rule. So this
        # is a warning, never a blocker: thin-per-feature evidence with a
        # passing backtest is usable; the warning explains the residual risk.
        epv = n_pos / max(len(DEFAULT_WEIGHTS), 1)
        if epv < 10:
            flags.append(
                f"~{epv:.0f} Gewinner je Merkmal (Richtwert >=10): einzelne "
                "Merkmalswerte können überangepasst sein — Backtest entscheidet")
    if n_pop and base >= 0.7:
        reasons.append(f"Basisrate {base:.0%} im Filter — fast alle sind Käufer, "
                       "es gibt nichts zu diskriminieren (die 87%-Falle)")
    if n_pop and n_pos and base <= 0.005:
        flags.append(f"Basisrate {base:.2%} — extrem selten; Ranking möglich, aber "
                     "Treffer bleiben absolut selten")

    # feature collapse: which scoring features become near-uniform INSIDE the filter
    collapsed: list[str] = []
    if n_pop >= 30:
        ads = _ad_presence_map([c.id for c in pop])
        crm_prod = crm_product_map()
        for feat in DEFAULT_WEIGHTS:
            vals = {}
            known = 0
            for c in pop:
                v = company_features(c, ads, crm_prod)[feat]
                if feat in _LIST_FEATURES:
                    continue
                if v:
                    known += 1
                    vals[v] = vals.get(v, 0) + 1
            if known >= n_pop * 0.5 and vals and max(vals.values()) / known >= 0.95:
                collapsed.append(feat)
        if collapsed:
            flags.append("im Filter (nahezu) konstant und damit wirkungslos: "
                         + ", ".join(collapsed))

    result = {"filters": filters, "population": n_pop, "positives": n_pos,
              "base_rate": round(base, 4), "blockers": reasons, "warnings": flags,
              "collapsed_features": collapsed}

    if reasons:
        result["verdict"] = "red"
        return result

    # the final word: does it rank, on a time split, WITHIN the filter?
    ids = [c.id for c in pop]
    bt = backtest(cut, ids=ids)
    result["backtest"] = {k: bt.get(k) for k in
                          ("train", "test", "positives", "base_rate",
                           "top_decile_lift", "monotone_steps", "of_steps", "ranks")}
    if bt.get("ranks"):
        result["verdict"] = "yellow" if flags else "green"
    else:
        result["verdict"] = "yellow" if n_pos >= 150 else "red"
        result["warnings"].append(
            "Backtest: kein Ranking innerhalb des Filters (Lift "
            f"{bt.get('top_decile_lift')}) — Ergebnis als Scorecard/Filter nutzen, "
            "nicht als Reihenfolge")
    return result


def backtest(cut: dt.date | None = None, segments: tuple[str, ...] | None = None,
             deciles: int = 10, ids: list[int] | None = None) -> dict:
    """Does the profile actually RANK? Train before `cut`, test after it.

    This exists because the app twice reported a spectacular top-vs-bottom lift
    that was an artefact. Both times the profile was really only sorting segments
    — "Architekten never buy, dealers do" — which is true, already known, and one
    categorical filter rather than a ranking. Measured here: 164x across all
    segments, but 0.63x restricted to Handel+Verarbeiter, i.e. no ordering power
    at all inside the population anyone actually prospects.

    So always read `by_segment_restricted` alongside the headline, and treat
    `ranks` as the verdict. Pass `segments` to restrict the test population.
    """
    from ..models import CrmOrderEvent
    from .rfm import MATERIAL_EUR
    cut = cut or (dt.date.today() - dt.timedelta(days=365 * 2))

    with SessionLocal() as s:
        events: dict[int, list[tuple[dt.date, float]]] = {}
        for cid, d, amt in s.execute(select(CrmOrderEvent.company_id,
                                            CrmOrderEvent.order_date,
                                            CrmOrderEvent.amount)):
            events.setdefault(cid, []).append((d, float(amt or 0)))
        pop = [c for c in s.scalars(scope.apply(select(Company)))
               if not c.is_intercompany
               and (segments is None or c.segment in segments)
               and (ids is None or c.id in set(ids))]

    train, later, test_ids = [], set(), []
    for c in pop:
        mats = [d for d, a in events.get(c.id, []) if a >= MATERIAL_EUR]
        if any(d <= cut for d in mats):
            train.append(c.id)
        else:
            test_ids.append(c.id)
            if mats:
                later.add(c.id)

    out = {"cut": cut.isoformat(), "segments": list(segments) if segments else None,
           "train": len(train), "test": len(test_ids), "positives": len(later)}
    if len(train) < MIN_WINNERS_USABLE or not test_ids or not later:
        out["ranks"] = False
        out["reason"] = ("zu wenige Trainings-Gewinner" if len(train) < MIN_WINNERS_USABLE
                         else "keine Käufer im Testzeitraum")
        return out

    profile = build_profile({"ids": train,
                             "exclude_segment": list(WINNER_EXCLUDED_SEGMENTS)},
                            name="Backtest", as_of=cut, pop_ids=ids)
    ads = _ad_presence_map()
    # As of the CUT, not today: a family first seen after the split is not
    # something we knew when the prediction would have been made.
    crm_prod = crm_product_map(as_of=cut)
    with SessionLocal() as s:
        comps = list(s.scalars(select(Company).where(Company.id.in_(test_ids))))
    scored = []
    for c in comps:
        fit, _ = fit_for(company_features(c, ads, crm_prod), profile)
        if fit is not None:
            scored.append((fit, c.id in later))
    if not scored:
        out["ranks"] = False
        out["reason"] = "nichts bewertbar"
        return out

    scored.sort(key=lambda x: -x[0])
    n = len(scored)
    base = sum(1 for _, y in scored if y) / n
    rates = []
    for i in range(deciles):
        lo, hi = i * n // deciles, (i + 1) * n // deciles
        part = scored[lo:hi] or [(0, False)]
        rates.append(sum(1 for _, y in part if y) / len(part))
    top, bot = rates[0], rates[-1]
    # "Ranks" needs BOTH a real top-vs-bottom gap and a mostly monotone curve.
    # A big gap with a jagged curve is what an artefact looks like.
    monotone = sum(1 for i in range(deciles - 1) if rates[i] >= rates[i + 1])
    out.update({
        "scored": n, "base_rate": round(base, 4),
        "decile_rates": [round(r, 4) for r in rates],
        "top_decile_lift": round(top / base, 2) if base else None,
        "top_vs_bottom": round(top / bot, 2) if bot else None,
        "monotone_steps": monotone, "of_steps": deciles - 1,
        "ranks": bool(top / base >= 1.3 and monotone >= (deciles - 1) * 0.7)
        if base else False,
    })
    return out


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
