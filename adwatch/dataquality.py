"""Data-quality repairs that must be re-runnable, not one-off scripts.

Every function here is idempotent and reports what it touched. They exist
because an import can only be as good as its source, and three sources feed this
database (a CRM export, a colleague's market list, the web) with different
notions of what a filled field means.

Run them all with `audit()` for a report, `repair()` to apply.
"""
from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select

from .db import SessionLocal
from .models import Company

# Fields that only ever come FROM a company's own website. If the identity check
# did not prove the site belongs to the company, none of them are backed by
# anything and must not sit in the row pretending they are.
_SITE_DERIVED = ("description", "products", "founded_year", "employee_hint",
                 "legal_form", "service_area", "competitor_brands",
                 "mentions_solarlux", "assessment", "certifications",
                 "own_fabrication", "has_showroom", "project_focus",
                 "positioning", "solarlux_relevance", "office_type",
                 "decision_role", "reference_scale", "solarlux_fit",
                 "partner_of", "installs", "site_language")

# An identity verdict that does NOT license keeping extracted facts.
_UNBACKED = ("conflict", "not_found", "unreachable")


def clear_unbacked_enrichment(apply: bool = False) -> dict:
    """Drop website-derived facts from rows whose website was never proven.

    The pipeline writes facts and the identity verdict in the same run, so they
    normally agree. They drift when a verdict is REVISED later — a domain that
    passed once and was demoted to `conflict` after a better check keeps the
    description, products and brands it produced. D3 Outdoor Girona still
    carried a full profile (products, Corradi as a brand) read off
    d3barcelona.com, a site the checker had already ruled was not theirs.

    Fifteen more rows sat on `not_found`: a description with no website at all.

    The domain and the verdict are deliberately KEPT. They are the evidence that
    the check happened, downstream consumers already exclude `conflict`, and
    deleting them would only invite the same domain to be found again tomorrow.
    """
    hit = []
    with SessionLocal() as s:
        for c in s.scalars(select(Company).where(Company.identity_status.in_(_UNBACKED))):
            dirty = [f for f in _SITE_DERIVED
                     if getattr(c, f, None) not in (None, "", [], {})]
            if not dirty:
                continue
            hit.append({"id": c.id, "name": c.name, "status": c.identity_status,
                        "domain": c.website_domain, "fields": dirty})
            if apply:
                for f in dirty:
                    setattr(c, f, None)
                # the row was never really enriched, so stop claiming it was
                c.enrichment_status = "none"
        if apply:
            s.commit()
    return {"rows": len(hit), "examples": hit[:5],
            "fields_cleared": sum(len(h["fields"]) for h in hit)}


def normalise_website_domains(apply: bool = False) -> dict:
    """Store a domain in the domain column, not an e-mail address.

    81 rows hold `info@holz9.com` or `http://am@am2.es`. The crawler copes —
    normalize_domain() strips the local part — but the Explorer, the export and
    the report all render the raw column, so a colleague sees a mailbox where a
    website should be and cannot tell whether we hold one.
    """
    from .enrich.domains import normalize_domain
    changed = []
    with SessionLocal() as s:
        for c in s.scalars(select(Company).where(Company.website_domain.is_not(None))):
            raw = (c.website_domain or "").strip()
            if not raw:
                continue
            clean = normalize_domain(raw)
            if clean and clean != raw:
                changed.append((c.id, raw, clean))
                if apply:
                    c.website_domain = clean
        if apply:
            s.commit()
    return {"rows": len(changed), "with_at": sum(1 for _, r, _ in changed if "@" in r),
            "examples": changed[:5]}


def find_domain_duplicates() -> dict:
    """Companies sharing one website — reported, never merged automatically.

    761 groups covering 1.927 rows. They are NOT all errors: a Solarlux dealer
    with three branches legitimately has one site and three account records with
    different SAP numbers. But they are also how the same firm gets counted
    twice in a market list, which is what happened to the Spanish import (CBF and
    Calvia Balear Fachadas, LUCOR twice, Schüco five times).

    Merging needs a human, so this only surfaces the groups and ranks them by how
    likely they are to be a genuine duplicate: same domain AND a similar name.
    """
    # Legal forms carry no identity: "CBF" and "CBF S.L." are one firm. Dots must
    # go BEFORE the token match, or "S.L." never matches `sl` — it reads as the
    # two tokens "s" and "l" — and the pair looks like two different companies,
    # which is exactly the duplicate we are trying to find.
    _FORMS = {"gmbh", "co", "kg", "ag", "sl", "slu", "sa", "sau", "bv", "nv",
              "ltd", "lda", "srl", "sarl", "spa", "ohg", "gbr", "ek", "kgaa",
              "ug", "se", "as", "ab", "oy", "aps", "plc", "sl p", "slp"}

    def key(n: str) -> str:
        squashed = re.sub(r"[.\-/&,]", "", (n or "").lower())   # S.L. -> sl, e.K. -> ek
        tokens = [t for t in re.split(r"[^a-z0-9]+", squashed) if t]
        return "".join(t for t in tokens if t not in _FORMS)

    groups = defaultdict(list)
    with SessionLocal() as s:
        for c in s.scalars(select(Company).where(Company.website_domain.is_not(None))):
            d = (c.website_domain or "").lower().strip()
            if d:
                groups[d].append(c)
        out = []
        for dom, rows in groups.items():
            if len(rows) < 2:
                continue
            # Collect the rows whose names collapse to the SAME name. Flagging the
            # whole group would be wrong: Lindner has 9 entities on one domain and
            # only two of them are the same firm twice. Report the pair, not the
            # group, or a human is sent to re-check eight correct records.
            by_key: dict[str, list] = defaultdict(list)
            for r in rows:
                by_key[key(r.name)].append(r)
            pairs = [[r.name for r in v] for v in by_key.values() if len(v) > 1]
            out.append({"domain": dom, "n": len(rows),
                        "duplicate_pairs": pairs,
                        "names": [r.name for r in rows][:4],
                        "ids": [r.id for r in rows]})
    out.sort(key=lambda x: (not x["duplicate_pairs"], -x["n"]))
    dupes = [o for o in out if o["duplicate_pairs"]]
    return {"groups": len(out), "rows": sum(o["n"] for o in out),
            "groups_with_a_duplicate_pair": len(dupes),
            "duplicate_rows": sum(sum(len(p) for p in o["duplicate_pairs"]) for o in dupes),
            "top": dupes[:10]}


# Product "families" that are really a single product, a sub-line, or a
# leftover. slx_product's family column mixes all three with the real families.
_FAMILY_PARENT = {
    "Highline": "Glas-Faltwand", "SL 25": "Glas-Faltwand",
    "SL 25XXL": "Glas-Faltwand", "Ecoline": "Glas-Faltwand",
    "Proline S": "Glas-Faltwand", "SDL Atrium": "Wintergarten",
    "SDL Acubis": "Wintergarten", "Varianda": "Glashaus und Terrassendach",
}


def fold_product_subfamilies(apply: bool = False) -> dict:
    """Fold product names into the family they belong to.

    "Highline" and "SL 25" are Glas-Faltwand systems, not families of their own,
    and they arrive in the same column as the families. Left alone they scatter a
    handful of companies into one-row families that no filter will ever find, and
    they make the family count look like 29 when the catalogue has 21.
    """
    from .models import CrmCompanyProduct
    moved = []
    with SessionLocal() as s:
        rows = list(s.scalars(select(CrmCompanyProduct)
                              .where(CrmCompanyProduct.family.in_(tuple(_FAMILY_PARENT)))))
        for r in rows:
            parent = _FAMILY_PARENT[r.family]
            moved.append((r.company_id, r.family, parent))
            if not apply:
                continue
            existing = s.scalars(select(CrmCompanyProduct).where(
                CrmCompanyProduct.company_id == r.company_id,
                CrmCompanyProduct.family == parent)).first()
            if existing:
                existing.positions += r.positions
                existing.value = (existing.value or 0) + (r.value or 0)
                s.delete(r)
            else:
                r.family = parent
        if apply:
            s.commit()
    return {"rows": len(moved), "examples": moved[:6]}


def clear_out_of_scope_scores(apply: bool = False) -> dict:
    """Remove scores from rows that are out of scope (consumers, competitors).

    Scores are written by passes that DO respect scope.apply(), so nothing puts
    new ones there — but a score written before the scope rule existed simply
    stayed. Measured 2026-08-10: all 1.665 Private Endkunden carried a
    `fit_score` stamped 2026-07-30, while every in-scope segment had been
    rescored on 2026-08-05. A stale score is worse than none: it survives every
    filtered view and then reappears the moment someone queries the column
    directly, or adds a report that forgets the filter.

    `health` is deliberately KEPT — see insights/rfm.recompute. It is a fact
    about the row, not a position in a call list.

    Two different rules, because the two exclusions mean different things:

      out of scope (consumers, competitors) — not part of the business at all,
        so nothing descriptive OR ranked belongs on the row.
      intercompany (own group) — a real company we really sell to, so `fit_score`
        stays as a description. Only the RANKINGS go: we are never going to
        acquire or win back our own Dutch subsidiary.
    """
    from . import scope
    OUT_OF_SCOPE = ("fit_score", "opportunity_score", "target_score",
                    "fit_breakdown", "winback_score")
    OWN_GROUP = ("target_score", "winback_score")
    hit = []
    with SessionLocal() as s:
        for c in s.scalars(select(Company).where(
                ~scope.in_scope_clause() | Company.is_intercompany.is_(True))):
            fields = (OUT_OF_SCOPE if not scope.is_in_scope(c.segment, c.is_competitor)
                      else OWN_GROUP)
            dirty = [f for f in fields if getattr(c, f, None) is not None]
            if not dirty:
                continue
            hit.append({"id": c.id, "name": c.name, "segment": c.segment,
                        "reason": ("ausserhalb des Geschäfts"
                                   if not scope.is_in_scope(c.segment, c.is_competitor)
                                   else "eigene Gesellschaft"),
                        "fields": dirty})
            if apply:
                for f in dirty:
                    setattr(c, f, None)
        if apply:
            s.commit()
    return {"rows": len(hit),
            "values_cleared": sum(len(h["fields"]) for h in hit),
            "examples": hit[:5]}


def close_searched_not_found(apply: bool = False) -> dict:
    """Write the verdict for companies that WERE searched and yielded nothing.

    Enrichment's persist block had a branch for a proven site and one for a
    review candidate, and nothing at all for the third outcome. So a company that
    was searched properly and simply has no findable website kept
    identity_status NULL — the same value the column carries for a company nobody
    has ever looked at.

    That silence costs money. find_website.pending_ids counts NULL as pending and
    treats 'not_found' as "do not re-spend"; measured on job 57, 248 Spanish
    companies were queued to be searched a second time for an answer already on
    record. Same shape as the same-week ad re-fetch, different invoice.

    Only rows with a candidate trail are closed: that trail IS the evidence a
    search ran. A company with an empty trail might have been enriched with
    allow_search=False, which proves nothing about the wider web and must leave
    the question open. Nothing is deleted — these rows carry no domain and no
    website-derived facts (checked before this shipped), so the verdict adds
    knowledge without discarding any.
    """
    import datetime as dt

    from .models import CompanyEnrichment

    hit = []
    with SessionLocal() as s:
        rows = s.execute(
            select(Company, CompanyEnrichment.website_candidates)
            .join(CompanyEnrichment, CompanyEnrichment.company_id == Company.id)
            .where(Company.identity_status.is_(None),
                   CompanyEnrichment.status == "no_website_found")).all()
        for c, candidates in rows:
            tried = candidates or []
            if not tried:
                continue          # no proof a search ever ran — leave it open
            hit.append({"id": c.id, "name": c.name, "candidates": len(tried)})
            if apply:
                c.identity_status = "not_found"
                c.identity_evidence = {"searched": True, "candidates": tried,
                                       "accepted": None, "review_candidate": None,
                                       "closed_by": "dataquality.close_searched_not_found"}
                c.identity_checked_at = dt.datetime.utcnow()
        if apply:
            s.commit()
    return {"rows": len(hit), "examples": hit[:5]}


def audit() -> dict:
    """Everything, reported, nothing changed."""
    return {"unbacked_enrichment": clear_unbacked_enrichment(apply=False),
            "website_domains": normalise_website_domains(apply=False),
            "domain_duplicates": find_domain_duplicates(),
            "product_subfamilies": fold_product_subfamilies(apply=False),
            "searched_not_found": close_searched_not_found(apply=False),
            "out_of_scope_scores": clear_out_of_scope_scores(apply=False)}


def repair() -> dict:
    """Apply the repairs that are safe without a human. Duplicates are NOT
    merged — that needs judgement about branches versus double entries.

    Order matters: the verdict is written BEFORE clear_unbacked_enrichment reads
    it, so a freshly closed 'not_found' row is checked for unbacked facts in the
    same pass rather than a run later."""
    return {"searched_not_found": close_searched_not_found(apply=True),
            "unbacked_enrichment": clear_unbacked_enrichment(apply=True),
            "website_domains": normalise_website_domains(apply=True),
            "product_subfamilies": fold_product_subfamilies(apply=True),
            "out_of_scope_scores": clear_out_of_scope_scores(apply=True)}
