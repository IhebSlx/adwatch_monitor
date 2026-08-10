"""Phase 0: the hostile pass over the data, re-runnable.

`dataquality.py` answers "what can I repair". This module answers the prior
question — **"what would this data make me believe that is not true?"** — and
repairs nothing. Every check here found something real on 2026-08-10; they are
kept so the same class of error is caught the next time data lands, rather than
re-discovered by hand.

The distinction that matters: a NULL rate is not a data-quality problem until you
know whether it differs by outcome. A column that is filled 92% of the time on
won deals and 0% on lost ones is not sparse — it IS the outcome, and putting it
in a model produces a spectacular score that predicts nothing. Three such columns
live in `crm_opportunities` and sit in the same table as the legitimate features.

Run `report()` for everything. Nothing here writes.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

from sqlalchemy import func, select, text

from . import scope
from .db import SessionLocal
from .models import (Company, CrmOpportunity, CrmOpportunityProduct,
                     CrmOrderEvent)

# A fill rate this many times higher (or lower) on won deals than on lost ones
# means the column carries the outcome, not a property of the customer.
LEAK_RATIO = 1.5

# Columns of crm_opportunities worth testing for outcome leakage. Deliberately
# includes the obvious ones (order_value) so the report states plainly why they
# are unusable, rather than leaving a reader to assume nobody checked.
_LEAK_CANDIDATES = (
    "order_value", "estimated_value", "end_customer_budget", "architect_crm_id",
    "end_customer_crm_id", "type_of_use", "postal_code", "city", "street",
    "vc_type", "dealer_status", "origin", "invoiced_value", "quoted_value",
    "closed_on", "sap_order_numbers",
)


def _rate(s, col: str, state: str) -> float:
    tot = s.scalar(select(func.count()).select_from(CrmOpportunity)
                   .where(CrmOpportunity.state == state)) or 0
    if not tot:
        return 0.0
    filled = s.scalar(text(
        f'select count(*) from crm_opportunities where state=:st '
        f'and {col} is not null and {col} != ""'), {"st": state}) or 0
    return filled / tot


def outcome_leakage() -> dict:
    """Fill rate of each candidate column, won vs lost.

    Measured 2026-08-10: `order_value` 91.9% vs 0.0% (2955x), `invoiced_value`
    and `sap_order_numbers` 92.1% vs 0.2% (538x) — these are what happened AFTER
    the deal, stored beside the features. And `end_customer_budget` is inverted
    at 0.39x: filled more often on losses, presumably because a budget gets
    written down when the deal is being argued about on price.
    """
    rows = []
    with SessionLocal() as s:
        for col in _LEAK_CANDIDATES:
            w, l, o = (_rate(s, col, "gewonnen"), _rate(s, col, "verloren"),
                       _rate(s, col, "offen"))
            ratio = (w / l) if l > 0.0001 else (None if w <= 0 else float("inf"))
            leaks = ratio is not None and (ratio >= LEAK_RATIO
                                           or ratio <= 1 / LEAK_RATIO)
            rows.append({"column": col, "won": round(w, 4), "lost": round(l, 4),
                         "open": round(o, 4),
                         "ratio": (None if ratio in (None, float("inf"))
                                   else round(ratio, 2)),
                         "unusable_as_feature": bool(leaks or ratio == float("inf"))})
    rows.sort(key=lambda r: -(r["ratio"] or 1e9))
    return {"leak_ratio_threshold": LEAK_RATIO, "columns": rows,
            "unusable": [r["column"] for r in rows if r["unusable_as_feature"]]}


def orphans() -> dict:
    """Links that point at rows we do not have.

    The big one is `crm_opportunity_products`: 78,732 of 145,865 rows (54.0%)
    reference a Verkaufschance outside the loaded 2023+ window, because the
    product lines were pulled over a wider period than the opportunities. The
    Objekt drawer joins on `opportunity_guid` and therefore shows only the 46%
    that resolve, while any aggregate over the whole table (OVERVIEW.md §5)
    counts all of it. Not a corruption — a population mismatch, and it has to be
    stated wherever those euros or positions are shown.
    """
    with SessionLocal() as s:
        opp_guids = {g.lower() for (g,) in s.execute(
            select(CrmOpportunity.opportunity_guid)
            .where(CrmOpportunity.opportunity_guid.is_not(None)))}
        crm_ids = {g.lower() for (g,) in s.execute(
            select(Company.crm_id).where(Company.crm_id.is_not(None)))}

        prod_total = s.scalar(select(func.count()).select_from(CrmOpportunityProduct))
        prod_guids = {g.lower() for (g,) in s.execute(
            select(CrmOpportunityProduct.opportunity_guid).distinct())}
        prod_orphan = s.scalar(text(
            "select count(*) from crm_opportunity_products p where lower(p.opportunity_guid) "
            "not in (select lower(opportunity_guid) from crm_opportunities "
            "where opportunity_guid is not null)")) or 0
        prod_orphan_value = s.scalar(text(
            "select coalesce(sum(value),0) from crm_opportunity_products p "
            "where lower(p.opportunity_guid) not in (select lower(opportunity_guid) "
            "from crm_opportunities where opportunity_guid is not null)")) or 0.0

        events_orphan = s.scalar(text(
            "select count(*) from crm_order_events e left join companies c "
            "on c.id = e.company_id where c.id is null")) or 0

        roles = {}
        for label, col in (("kaeufer", CrmOpportunity.parent_account_crm_id),
                           ("architekt", CrmOpportunity.architect_crm_id),
                           ("endkunde", CrmOpportunity.end_customer_crm_id)):
            vals = [v for (v,) in s.execute(select(col)) if v and v.strip()]
            miss = sum(1 for v in vals if v.lower() not in crm_ids)
            roles[label] = {"set": len(vals), "unresolvable": miss,
                            "share": round(miss / len(vals), 4) if vals else 0}
        no_buyer = s.scalar(text(
            'select count(*) from crm_opportunities where parent_account_crm_id '
            'is null or parent_account_crm_id = ""')) or 0
        # what the unattributable deals are WORTH — the number that decides
        # whether "per company" analyses can be trusted
        lost_value = s.scalar(text(
            'select coalesce(sum(order_value),0) from crm_opportunities '
            'where state = "gewonnen" and (parent_account_crm_id is null '
            'or parent_account_crm_id = "" or lower(parent_account_crm_id) not in '
            '(select lower(crm_id) from companies where crm_id is not null))')) or 0.0
        won_value = s.scalar(text(
            'select coalesce(sum(order_value),0) from crm_opportunities '
            'where state = "gewonnen"')) or 0.0

    return {
        "opportunity_products": {
            "rows": prod_total, "orphan_rows": prod_orphan,
            "orphan_share": round(prod_orphan / prod_total, 4) if prod_total else 0,
            "distinct_guids": len(prod_guids),
            "guids_resolved": len(prod_guids & opp_guids),
            "orphan_value_eur": round(prod_orphan_value, 2)},
        "order_events_without_company": events_orphan,
        "opportunity_roles": roles,
        "opportunities_without_buyer": no_buyer,
        "unattributable_won_value_eur": round(lost_value, 2),
        "won_value_total_eur": round(won_value, 2),
        "unattributable_won_share": (round(lost_value / won_value, 4)
                                     if won_value else 0),
    }


def architect_field() -> dict:
    """How often `architect_crm_id` is the buyer itself.

    The field is `slx_executingarchitect_accountid` — the AUSFÜHRENDER Architekt.
    A dealer that plans in-house enters itself, correctly. 60.7% of the filled
    values are that case, so "opportunities that name an architect" overstates
    architect involvement by a factor of 2.5. See
    insights/projekte.specifying_architect().
    """
    with SessionLocal() as s:
        segs = {(c or "").lower(): seg for c, seg in
                s.execute(select(Company.crm_id, Company.segment)
                          .where(Company.crm_id.is_not(None)))}
        same, third = Counter(), Counter()
        total = 0
        for a, p in s.execute(select(CrmOpportunity.architect_crm_id,
                                     CrmOpportunity.parent_account_crm_id)):
            if not a or not a.strip():
                continue
            total += 1
            seg = segs.get(a.lower()) or "(nicht in companies)"
            if p and a.lower() == p.lower():
                same[seg] += 1
            else:
                third[seg] += 1
        n_opps = s.scalar(select(func.count()).select_from(CrmOpportunity)) or 1
    n_same = sum(same.values())
    return {"filled": total, "share_of_all_opportunities": round(total / n_opps, 4),
            "architect_is_buyer": n_same,
            "self_reference_share": round(n_same / total, 4) if total else 0,
            "third_party": total - n_same,
            "third_party_share_of_all": round((total - n_same) / n_opps, 4),
            "segments_when_self": same.most_common(5),
            "segments_when_third_party": third.most_common(5)}


def censoring() -> dict:
    """Open share per creation year, and per number of Verkaufschancen on the
    Objekt.

    Both matter and the second is the trap: the open share RISES with the VC
    count (18.8% at one VC to 37.2% at five-to-nine), so right-censoring bites
    hardest exactly where the win-rate-by-VC-count signal is read off. The
    signal survives cohort stratification anyway (checked 2026-08-10), but any
    fresh reading of it has to exclude immature cohorts.
    """
    with SessionLocal() as s:
        rows = list(s.execute(select(CrmOpportunity.project_id,
                                     CrmOpportunity.opportunity_guid,
                                     CrmOpportunity.crm_id,
                                     CrmOpportunity.state,
                                     CrmOpportunity.lost_reason,
                                     CrmOpportunity.created_on)))
    by_year: dict[str, Counter] = defaultdict(Counter)
    groups: dict[str, list] = defaultdict(list)
    for pid, guid, cid, state, reason, created in rows:
        by_year[str(created)[:4] if created else "?"][state or "?"] += 1
        groups[pid or guid or cid].append((state, reason))
    cohorts = []
    for y in sorted(by_year):
        c = by_year[y]
        n = sum(c.values())
        dec = c["gewonnen"] + c["verloren"]
        cohorts.append({"year": y, "opportunities": n,
                        "open_share": round(c["offen"] / n, 4) if n else 0,
                        "win_rate_of_decided": round(c["gewonnen"] / dec, 4) if dec else None,
                        "mature": (c["offen"] / n) < 0.10 if n else False})

    def bucket(n):
        return "1" if n == 1 else "2" if n == 2 else "3-4" if n <= 4 else "5-9" if n <= 9 else "10+"

    buckets: dict[str, Counter] = defaultdict(Counter)
    for members in groups.values():
        won = any(st == "gewonnen" for st, _ in members) or any(
            (r or "") == "Zugehörige VC gewonnen" for _, r in members)
        out = "gewonnen" if won else ("offen" if any(st == "offen" for st, _ in members)
                                      else "verloren")
        buckets[bucket(len(members))][out] += 1
    bk = []
    for b in ("1", "2", "3-4", "5-9", "10+"):
        c = buckets[b]
        n = sum(c.values())
        if not n:
            continue
        bk.append({"vcs_per_objekt": b, "objekte": n,
                   "open_share": round(c["offen"] / n, 4)})
    return {"cohorts": cohorts, "by_vc_count": bk,
            "mature_cohorts": [c["year"] for c in cohorts if c["mature"]]}


def contradictions() -> dict:
    """Rows whose own fields disagree, and values that cannot be what they say."""
    checks = {
        "gewonnen_ohne_order_value":
            'select count(*) from crm_opportunities where state="gewonnen" '
            'and (order_value is null or order_value = 0)',
        "offen_mit_closed_on":
            'select count(*) from crm_opportunities where state="offen" '
            'and closed_on is not null',
        "closed_vor_created":
            'select count(*) from crm_opportunities where closed_on is not null '
            'and closed_on < created_on',
        "verloren_ohne_grund":
            'select count(*) from crm_opportunities where state="verloren" '
            'and (lost_reason is null or lost_reason = "")',
        "gewonnen_mit_verlustgrund":
            'select count(*) from crm_opportunities where state="gewonnen" '
            'and lost_reason is not null and lost_reason != ""',
        "negative_estimated_value":
            'select count(*) from crm_opportunities where estimated_value < 0',
        "beleg_count_null_trotz_events":
            'select count(distinct e.company_id) from crm_order_events e '
            'join companies c on c.id = e.company_id where c.beleg_count = 0',
        "conversion_rate_ueber_1":
            'select count(*) from companies where conversion_rate > 1',
        "enriched_ohne_website":
            'select count(*) from companies where enrichment_status = "enriched" '
            'and (website_domain is null or website_domain = "")',
        "kaeufer_gleich_endkunde":
            'select count(*) from crm_opportunities where end_customer_crm_id '
            'is not null and lower(end_customer_crm_id) = lower(parent_account_crm_id)',
    }
    with SessionLocal() as s:
        return {k: (s.scalar(text(q)) or 0) for k, q in checks.items()}


def scope_leaks() -> dict:
    """Out-of-scope rows (consumers, competitors) that still carry a score.

    `health` is excluded on purpose — it is a fact about the row, not a ranking.
    Everything else here puts a company somewhere a colleague might call it.
    """
    fields = ("fit_score", "opportunity_score", "target_score", "winback_score")
    out = {}
    with SessionLocal() as s:
        for f in fields:
            out[f] = s.scalar(text(
                f"select count(*) from companies where {f} is not null and "
                f'(segment = "Private Endkunden" or is_competitor = 1)')) or 0
        out["intercompany_flagged"] = s.scalar(
            select(func.count()).select_from(Company)
            .where(Company.is_intercompany.is_(True))) or 0
        # own-group companies the name patterns WOULD catch but that carry no flag
        from .customers import looks_intercompany
        unflagged = [(c.name, round(c.beleg_sum or 0)) for c in s.scalars(
            select(Company).where(Company.is_intercompany.is_(False)))
            if looks_intercompany(c.name)]
        unflagged.sort(key=lambda x: -x[1])
    out["intercompany_unflagged"] = len(unflagged)
    out["intercompany_unflagged_top"] = unflagged[:10]
    return out


def counts() -> dict:
    """Row counts, so a delta after an import is visible rather than assumed."""
    with SessionLocal() as s:
        n = {t: (s.scalar(text(f"select count(*) from {t}")) or 0) for t in (
            "companies", "crm_opportunities", "crm_order_events",
            "crm_opportunity_products", "crm_company_products", "crm_showrooms",
            "weekly_company_metrics", "ads", "company_pages", "company_enrichment")}
        n["order_events_belege"] = s.scalar(
            select(func.sum(CrmOrderEvent.beleg_count))) or 0
        n["companies_in_scope"] = s.scalar(
            scope.apply(select(func.count()).select_from(Company))) or 0
    return n


def report() -> dict:
    """Everything. Nothing is written."""
    return {"generated": dt.date.today().isoformat(),
            "counts": counts(),
            "orphans": orphans(),
            "outcome_leakage": outcome_leakage(),
            "architect_field": architect_field(),
            "censoring": censoring(),
            "contradictions": contradictions(),
            "scope_leaks": scope_leaks()}
