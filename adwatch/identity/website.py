"""Prove that a claimed website really belongs to the company — for free.

The distinction this module enforces is the one the whole app rests on. A domain
in `Company.website_domain` is a CLAIM. Two things read that column and treat it
as fact: the enrichment crawler (which writes a description and a product list
from it) and the Google-Ads advertiser lookup (which attributes an ad history to
it). If the claim is wrong, both produce confident output about the wrong company,
and nothing downstream can tell.

The state that made this urgent: 24,077 companies had a domain, but only 1,426 had
ever passed a check. 22,696 came straight from CRM in one bulk fill — good
evidence, since a colleague typed them, but not proof, and indistinguishable from
verified ones until `website_source` and `identity_status` existed.

Verification costs NO API money. It is an HTTP fetch plus enrich.validate:

    company's own phone on the page          -> 'phone'        (strongest)
    same PLZ and same street                 -> 'plz_street'
    same PLZ and a distinctive name token    -> 'plz_name'
    name in domain AND a name token on page  -> 'domain_plus_name'

Anything else is a `conflict` — the site was read and does not match. That verdict
matters as much as a match: it is how a portal page, a parent brand or a namesake
gets caught before enrichment spends money describing the wrong firm.

CRM supplies the evidence for this: a phone for 42,256 accounts and a street for
46,141. That is why `crm_import.import_contact_data` runs before this.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import or_, select

from ..db import SessionLocal
from ..enrich import validate
from ..enrich.fetchpage import page_bundle
from ..models import Company

log = logging.getLogger("adwatch.identity.website")

# Verdicts written to Company.identity_status
UNVERIFIED, VERIFIED, CONFLICT, UNREACHABLE = (
    "unverified", "verified", "conflict", "unreachable")

# A verdict older than this is worth re-checking: companies move, sites get
# rebuilt, domains get sold. Not enforced automatically — callers decide.
STALE_AFTER_DAYS = 365


def _company_dict(c: Company) -> dict:
    return {"name": c.name, "phone": c.phone, "postal_code": c.postal_code,
            "street": c.street, "city": c.city,
            # country drives the postcode format in validate.plz_matches
            "country": c.country}


def verify_one(company_id: int) -> dict:
    """Fetch the claimed domain and decide. Writes the verdict and its evidence.

    Never clears `website_domain` on a conflict — the domain may still be useful
    to a human, and silently deleting master data hides the problem instead of
    surfacing it. The STATUS is what gates downstream work.
    """
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if c is None:
            return {"company_id": company_id, "status": None, "error": "not found"}
        domain = (c.website_domain or "").strip()
        if not domain:
            return {"company_id": company_id, "status": None, "error": "no domain"}
        info = _company_dict(c)

    bundle = page_bundle(domain)
    now = dt.datetime.utcnow()
    if not bundle or not (bundle.get("text") or "").strip():
        with SessionLocal() as s:
            c = s.get(Company, company_id)
            c.identity_status = UNREACHABLE
            c.identity_checked_at = now
            c.identity_evidence = {"reason": "not fetchable or empty"}
            s.commit()
        return {"company_id": company_id, "status": UNREACHABLE, "domain": domain}

    res = validate.validate_site(info, domain, bundle.get("text"))
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        c.identity_status = VERIFIED if res["ok"] else CONFLICT
        c.identity_matched_by = res["matched_by"]
        c.identity_evidence = {"signals": res["signals"], "domain": domain,
                               "pages": bundle.get("pages")}
        c.identity_checked_at = now
        s.commit()
    return {"company_id": company_id, "status": VERIFIED if res["ok"] else CONFLICT,
            "matched_by": res["matched_by"], "domain": domain}


def pending_ids(limit: int | None = None, *, monitored_only: bool = False,
                recheck_conflicts: bool = False) -> list[int]:
    """Companies with a claimed domain and no usable verdict yet.

    Ordered by Beleg value so the companies that matter get verified first — with
    46,000 accounts the queue is long, and a run that stops early should have
    spent its time on the €250k dealers rather than alphabetically.
    """
    wanted = [None, UNVERIFIED, UNREACHABLE]
    if recheck_conflicts:
        wanted.append(CONFLICT)
    with SessionLocal() as s:
        stmt = (select(Company.id)
                .where(Company.website_domain.is_not(None),
                       or_(*[Company.identity_status.is_(None) if w is None
                             else Company.identity_status == w for w in wanted]))
                .order_by(Company.beleg_sum.desc(), Company.id))
        if monitored_only:
            stmt = stmt.where(Company.monitored.is_(True))
        if limit:
            stmt = stmt.limit(limit)
        return [cid for (cid,) in s.execute(stmt)]


def verify_batch(limit: int = 100, *, monitored_only: bool = False,
                 progress=None) -> dict:
    """Verify up to `limit` companies. Pure HTTP — no paid API is touched."""
    ids = pending_ids(limit, monitored_only=monitored_only)
    counts = {VERIFIED: 0, CONFLICT: 0, UNREACHABLE: 0, "skipped": 0}
    by_signal: dict[str, int] = {}
    for i, cid in enumerate(ids, 1):
        try:
            r = verify_one(cid)
        except Exception:
            log.exception("identity verify failed for company %s", cid)
            counts["skipped"] += 1
            continue
        st = r.get("status")
        if st in counts:
            counts[st] += 1
        else:
            counts["skipped"] += 1
        if r.get("matched_by"):
            by_signal[r["matched_by"]] = by_signal.get(r["matched_by"], 0) + 1
        if progress:
            progress(i, len(ids))
    out = {"checked": len(ids), **counts, "matched_by": by_signal}
    log.info("identity.verify_batch: %s", out)
    return out


def overview() -> dict:
    """How much of the base has a PROVEN website — the number that says whether
    enrichment and ad attribution can be trusted at all."""
    from sqlalchemy import func
    with SessionLocal() as s:
        total = s.scalar(select(func.count()).select_from(Company))
        with_domain = s.scalar(select(func.count()).select_from(Company)
                               .where(Company.website_domain.is_not(None)))
        by_status = dict(s.execute(
            select(Company.identity_status, func.count())
            .where(Company.website_domain.is_not(None))
            .group_by(Company.identity_status)).all())
        by_source = dict(s.execute(
            select(Company.website_source, func.count())
            .where(Company.website_domain.is_not(None))
            .group_by(Company.website_source)).all())
        by_signal = dict(s.execute(
            select(Company.identity_matched_by, func.count())
            .where(Company.identity_matched_by.is_not(None))
            .group_by(Company.identity_matched_by)).all())
    return {"companies": total, "with_domain": with_domain,
            "by_status": by_status, "by_source": by_source,
            "verified_by_signal": by_signal,
            "verified_share": round((by_status.get(VERIFIED, 0) / with_domain), 4)
            if with_domain else 0.0}
