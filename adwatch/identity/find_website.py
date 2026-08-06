"""Find the website of a company that has none — and only keep it when PROVEN.

The instruction this implements: "do not add the website unless you are very sure
it belongs to them." So this is deliberately stricter than the enrichment gate.

`enrich.validate.validate_site` auto-accepts four outcomes. This module accepts
only the three that rest on the company's OWN address or phone:

    phone       the company's number is on the site        -> accept
    plz_street  same postcode AND same street              -> accept
    plz_name    same postcode AND a distinctive name token  -> accept
    ----------------------------------------------------------------------
    domain_plus_name   name in the domain + a name token on the page
                       -> NOT accepted. Stored for a human to confirm.

Why that line is drawn there: `domain_plus_name` proves a name coincidence, not a
locality. "Premial" or "Al-Andalus" would match a namesake in another province,
and a wrong website is worse than none — it silently produces a description, a
product list and an ad history for the wrong firm, and nothing downstream can
tell. A candidate a human confirms in ten seconds costs far less than that.

For the Spain market list specifically, phone is unavailable (0 of 39 rows carry
one) and only 26 of 39 have a postcode. So a low acceptance rate is the EXPECTED
outcome here, not a failure — the rest surface as review candidates.

Cost: one or two Serper queries per company (~EUR 0.001 each). Fetching the
candidate pages is free.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from sqlalchemy import select

from ..db import SessionLocal
from ..enrich import validate
from ..enrich.fetchpage import page_bundle
from ..enrich.website_finder import search_candidates
from ..models import Company

log = logging.getLogger("adwatch.identity.find_website")

# Outcomes strong enough to write a domain without a human looking.
# `domain_in_name` is first because it needs no search and no inference: the
# source record literally names the domain, e.g. 'CBF (calviabalear.com)'. That is
# the researcher stating the website, not a match we guessed.
PROVEN = ("domain_in_name", "phone", "plz_street", "plz_name")

# A domain written inside the company name. Restricted to real TLDs so a sentence
# fragment or a filename cannot be mistaken for one.
_DOMAIN_IN_NAME = re.compile(
    r"\b([a-z0-9][a-z0-9\-]{1,60}\.(?:com|es|cat|eu|net|org|de|pt|fr|it))\b", re.I)

# Never accepted as a company's own site: a social or directory profile is not a
# website we can enrich from or attribute ads to.
_NOT_OWN_SITE = ("linkedin.", "facebook.", "instagram.", "twitter.", "x.com",
                 "youtube.", "tiktok.", "pinterest.", "wa.me", "google.")


def domain_from_name(name: str | None) -> str | None:
    """The domain the source record states inside the company name, if any."""
    m = _DOMAIN_IN_NAME.search(name or "")
    if not m:
        return None
    d = m.group(1).lower()
    if any(bad in d for bad in _NOT_OWN_SITE):
        return None
    return d

# Statuses this module writes.
VERIFIED = "verified"
NEEDS_REVIEW = "needs_review"     # a plausible candidate, not proven
NOT_FOUND = "not_found"           # searched, nothing usable — do not re-spend

# How many search hits to validate per company. Beyond this the results are
# ranked so poorly that fetching them mostly burns time.
MAX_CANDIDATES = 4


def _company_info(c: Company) -> dict:
    return {"name": c.name, "phone": c.phone, "postal_code": c.postal_code,
            "street": c.street, "city": c.city,
            # country drives the postcode format in validate.plz_matches
            "country": c.country}


def find_for(company_id: int, *, max_candidates: int = MAX_CANDIDATES) -> dict:
    """Search, validate, and write ONLY on a proven match.

    Returns the decision and every candidate considered, so a rejection is as
    auditable as an acceptance.
    """
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if c is None:
            return {"company_id": company_id, "status": None, "error": "not found"}
        if c.website_domain:
            return {"company_id": company_id, "status": "skipped",
                    "reason": "already has a domain"}
        info = _company_info(c)
        country = (c.country or "DE").upper()
        city = c.city
        name = c.name

    considered: list[dict] = []
    accepted: dict | None = None
    review: dict | None = None

    # Free pre-pass: the record may already state the domain in the name. No
    # search, no cost, and stronger evidence than anything a search could return.
    named = domain_from_name(name)
    if named:
        bundle = page_bundle(named)
        text = (bundle or {}).get("text") or ""
        res = validate.validate_site(info, named, text or None)
        accepted = {"domain": named, "matched_by": "domain_in_name",
                    "signals": res["signals"], "fetched": bool(text),
                    "also_validated_as": res["matched_by"]}
        considered.append(accepted)

    if accepted is None:
        try:
            cands = search_candidates(name, city, country, limit=max_candidates)
        except RuntimeError as exc:        # no Serper key
            return {"company_id": company_id, "status": None, "error": str(exc)}
    else:
        cands = []

    for cand in cands[:max_candidates]:
        domain = cand.get("domain")
        if not domain or any(bad in domain for bad in _NOT_OWN_SITE):
            continue      # search already filters these; belt and braces
        bundle = page_bundle(domain)
        text = (bundle or {}).get("text") or ""
        res = validate.validate_site(info, domain, text or None)
        row = {"domain": domain, "matched_by": res["matched_by"],
               "signals": res["signals"], "fetched": bool(text),
               "title": cand.get("title")}
        considered.append(row)
        if res["matched_by"] in PROVEN and accepted is None:
            accepted = row
            break                          # strongest outcome wins, stop paying
        if res["matched_by"] and review is None:
            review = row                   # domain_plus_name -> human decides

    now = dt.datetime.utcnow()
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if accepted:
            c.website_domain = accepted["domain"]
            c.website_source = "serper"
            c.identity_status = VERIFIED
            c.identity_matched_by = accepted["matched_by"]
            status = VERIFIED
        elif review:
            # deliberately NOT written to website_domain
            c.identity_status = NEEDS_REVIEW
            status = NEEDS_REVIEW
        else:
            c.identity_status = NOT_FOUND
            status = NOT_FOUND
        c.identity_evidence = {"searched": True, "candidates": considered,
                               "accepted": accepted["domain"] if accepted else None,
                               "review_candidate": review["domain"] if review else None}
        c.identity_checked_at = now
        s.commit()

    return {"company_id": company_id, "name": name, "status": status,
            "domain": (accepted or review or {}).get("domain"),
            "matched_by": (accepted or {}).get("matched_by"),
            "candidates": len(considered)}


def pending_ids(lead_source: str | None = None, country: str | None = None,
                limit: int | None = None, *, include_competitors: bool = False,
                retry_not_found: bool = False) -> list[int]:
    """Companies with no domain that have not been searched yet.

    `identity_status = 'not_found'` and `'needs_review'` are both treated as done
    so a re-run does not pay Serper twice for the same company.
    """
    from .. import scope
    done = [NEEDS_REVIEW, NOT_FOUND]
    if retry_not_found:
        done = [NEEDS_REVIEW]
    with SessionLocal() as s:
        stmt = select(Company.id).where(Company.website_domain.is_(None))
        if not include_competitors:
            stmt = scope.apply(stmt)
        stmt = stmt.where(
            (Company.identity_status.is_(None))
            | (Company.identity_status.not_in(done)))
        if lead_source:
            stmt = stmt.where(Company.lead_source == lead_source)
        if country:
            stmt = stmt.where(Company.country == country.upper())
        # value first: with a long queue, spend the search budget on the
        # companies that matter rather than alphabetically
        stmt = stmt.order_by(Company.beleg_sum.desc(), Company.id)
        if limit:
            stmt = stmt.limit(limit)
        return [cid for (cid,) in s.execute(stmt)]


def run(lead_source: str | None = None, country: str | None = None,
        limit: int = 50, *, progress=None) -> dict:
    """Search for up to `limit` companies. Each costs ~1-2 Serper queries."""
    ids = pending_ids(lead_source, country, limit)
    counts = {VERIFIED: 0, NEEDS_REVIEW: 0, NOT_FOUND: 0, "error": 0}
    by_signal: dict[str, int] = {}
    accepted: list[dict] = []
    for i, cid in enumerate(ids, 1):
        try:
            r = find_for(cid)
        except Exception:
            log.exception("website search failed for company %s", cid)
            counts["error"] += 1
            continue
        st = r.get("status")
        counts[st] = counts.get(st, 0) + 1
        if st == VERIFIED:
            by_signal[r["matched_by"]] = by_signal.get(r["matched_by"], 0) + 1
            accepted.append({"name": r["name"], "domain": r["domain"],
                             "matched_by": r["matched_by"]})
        if progress:
            progress(i, len(ids))
    out = {"searched": len(ids), **counts, "accepted_by": by_signal,
           "accepted": accepted}
    log.info("find_website.run: %s", {k: v for k, v in out.items() if k != "accepted"})
    return out


def review_queue(lead_source: str | None = None, limit: int = 100) -> list[dict]:
    """Candidates that were plausible but not proven — for a human to confirm.

    Deliberately surfaced rather than auto-accepted: the whole point is that a
    name coincidence is not proof of identity.
    """
    from .. import scope
    with SessionLocal() as s:
        stmt = scope.apply(select(Company)).where(
            Company.identity_status == NEEDS_REVIEW)
        if lead_source:
            stmt = stmt.where(Company.lead_source == lead_source)
        rows = list(s.scalars(stmt.limit(limit)))
        return [{
            "company_id": c.id, "name": c.name, "city": c.city,
            "postal_code": c.postal_code, "street": c.street,
            "import_type": c.import_type,
            "candidate": (c.identity_evidence or {}).get("review_candidate"),
            "candidates": (c.identity_evidence or {}).get("candidates") or [],
        } for c in rows]
