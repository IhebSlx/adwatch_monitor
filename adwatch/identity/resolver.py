"""PART 1 — Identity: link a company to its Facebook page(s), deterministically.

Everything that decides "which page belongs to which company" lives in this
package. Once a page is linked here, Part 2 (collect) fetches it by exact
page_id — so a `0 active ads` result is a trustworthy fact, never a
name-mismatch guess.

A company can own several pages (see partner_linker.py for the automatic
partner-account discovery); this module handles the MAIN page: name search,
candidate matching, confirm/unlink, and manual overrides.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from .. import config
from ..db import SessionLocal
from ..models import Company, CompanyPage


# ---------------------------------------------------------------------------
# Page CRUD (all identity mutations go through these)
# ---------------------------------------------------------------------------

def add_page(company_id: int, page_id: str, page_name: str | None = None,
             role: str = "main", status: str = "manual",
             evidence: dict | None = None) -> None:
    """Link a page to a company. Raises if the page is already linked elsewhere."""
    page_id = (page_id or "").strip()
    if not page_id:
        raise ValueError("A page ID is required")
    with SessionLocal() as s:
        existing = s.scalar(select(CompanyPage).where(
            CompanyPage.source == "meta", CompanyPage.page_id == page_id))
        if existing and existing.company_id != company_id:
            other = s.get(Company, existing.company_id)
            raise ValueError(f"Page {page_id} is already linked to “{other.name if other else '?'}” "
                             "— unlink it there first.")
        if existing:
            existing.page_name = page_name or existing.page_name
            existing.role, existing.status = role, status
            existing.evidence = evidence or existing.evidence
            existing.active = True
        else:
            s.add(CompanyPage(company_id=company_id, source="meta", page_id=page_id,
                              page_name=page_name, role=role, status=status,
                              evidence=evidence))
        if role == "main":
            # Replacing an existing main page (not just updating the same one) —
            # drop the old CompanyPage row so the company never ends up with
            # two rows both marked role="main" (which would double-fetch ads).
            # Scoped to source="meta": this function only ever manages the Meta
            # page, so a Google advertiser (source="google") main row must be
            # left untouched — the two platforms coexist.
            for old in s.scalars(select(CompanyPage).where(
                    CompanyPage.company_id == company_id, CompanyPage.source == "meta",
                    CompanyPage.role == "main", CompanyPage.page_id != page_id)):
                s.delete(old)
            c = s.get(Company, company_id)
            if c:
                c.page_id, c.page_name = page_id, page_name
                if status == "locked":
                    c.resolution_status = "locked"
                elif status in ("confirmed", "manual"):
                    c.resolution_status = "confirmed"
                else:
                    c.resolution_status = "ambiguous"
                c.confirmed_at = dt.datetime.utcnow()
                c.candidates = None
        s.commit()


def unlink_page(page_row_id: int) -> None:
    """Remove one page link (the company itself is untouched)."""
    with SessionLocal() as s:
        p = s.get(CompanyPage, page_row_id)
        if not p:
            return
        if p.role == "main":
            c = s.get(Company, p.company_id)
            if c and c.page_id == p.page_id:
                c.page_id = None
                c.page_name = None
                c.resolution_status = "pending"
        s.delete(p)
        s.commit()


def set_main_page(company_id: int, page_id: str, page_name: str | None = None,
                  page_category: str | None = None) -> None:
    """Human-confirmed main page (the manual fix for the identity problem)."""
    add_page(company_id, page_id, page_name, role="main", status="manual",
             evidence={"method": "manual_confirm"})
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if c and page_category:
            c.page_category = page_category
            s.commit()


def unlink_main(company_id: int) -> None:
    """Remove the company's current main-page association (a wrong link) WITHOUT
    discarding the candidate list — so the row drops back into review with its
    alternatives still visible. Clears the Meta main CompanyPage row(s) and the
    Company's page_id/name/url; status becomes 'ambiguous' if candidates remain,
    else 'pending'. Works for handle-only confirms too (no CompanyPage row)."""
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if not c:
            return
        for p in s.scalars(select(CompanyPage).where(
                CompanyPage.company_id == company_id, CompanyPage.source == "meta",
                CompanyPage.role == "main")):
            s.delete(p)
        c.page_id = None
        c.page_name = None
        c.page_url = None
        c.confirmed_at = None
        c.resolution_status = "ambiguous" if c.candidates else "pending"
        s.commit()


def clear_resolution(company_id: int) -> None:
    """Forget everything we know about a company's pages (back to pending)."""
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if not c:
            return
        for p in s.scalars(select(CompanyPage).where(CompanyPage.company_id == company_id)):
            s.delete(p)
        c.page_id = None
        c.page_name = None
        c.resolution_status = "pending"
        c.candidates = None
        s.commit()


def lock_identity(company_id: int, page_id: str, page_name: str | None = None) -> None:
    """Human-verified, LOCKED main page — the highest, protected status. Once
    locked, no automatic API resolution (the identity-check job) will ever
    touch it again; only an explicit unlock reopens it."""
    add_page(company_id, page_id, page_name, role="main", status="locked",
             evidence={"method": "manual_lock"})


def unlock_identity(company_id: int) -> None:
    """Reopen a locked identity for editing / re-checking. The page link is
    kept (drops to a plain confirmed/manual link); only the lock is removed."""
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if not c or c.resolution_status != "locked":
            return
        main = s.scalar(select(CompanyPage).where(
            CompanyPage.company_id == company_id, CompanyPage.role == "main",
            CompanyPage.status == "locked"))
        if main:
            main.status = "manual"
            c.resolution_status = "confirmed"
        else:
            c.resolution_status = "pending"   # locked flag without a page row (shouldn't happen) -> reset
        s.commit()


def list_pages(company_id: int) -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(select(CompanyPage)
                         .where(CompanyPage.company_id == company_id, CompanyPage.active)
                         .order_by(CompanyPage.role, CompanyPage.linked_at)).all()
        return [{
            "id": p.id, "page_id": p.page_id, "page_name": p.page_name,
            "role": p.role, "status": p.status, "evidence": p.evidence,
            "linked_at": p.linked_at.isoformat(timespec="minutes") if p.linked_at else None,
        } for p in rows]


# ---------------------------------------------------------------------------
# Live candidate search (used by the UI's manual verify flow)
# ---------------------------------------------------------------------------

def find_candidates(term: str, country: str | None = None, website_domain: str | None = None) -> dict:
    """Live keyword search that returns candidate pages WITHOUT storing anything.
    Costs one Apify call. `website_domain`, if given, lets a candidate whose ads
    link to that site outrank a same-named-but-unrelated page (see meta_source.py)."""
    from ..collect.meta_source import MetaAdSource
    src = MetaAdSource()
    res = src.search_and_resolve(term, country=country or config.DEFAULT_COUNTRY, max_ads=60,
                                 website_domain=website_domain)
    return {"status": res["status"], "search_term": res.get("search_term", term),
            "candidates": res.get("candidates", [])}


# ---------------------------------------------------------------------------
# Pipeline hook: resolve an unresolved company during a collection run
# ---------------------------------------------------------------------------

def resolve_and_record_google(source, session, company) -> dict:
    """Domain-based Google Ads resolution — the Google equivalent of
    resolve_and_record(), but keyed on company.website_domain instead of a
    name search (Google has no name search; see collect/google_source.py).
    Exact, not fuzzy: a domain either resolves to one advertiser or it
    doesn't, so there's no 'ambiguous' state to record here.

    Company.page_id/page_name/resolution_status stay Meta's fields — a
    confirmed Google advertiser lives purely as a CompanyPage row with
    source='google', so this never touches those Meta-specific columns."""
    if not company.website_domain:
        return {"status": "no_domain", "page_id": None, "page_name": None, "ads": []}

    candidates = source.resolve_company(company.website_domain, country=company.country)
    if not candidates:
        return {"status": "no_ads_found", "page_id": None, "page_name": None, "ads": []}

    best = candidates[0]
    existing = session.scalar(select(CompanyPage).where(
        CompanyPage.source == "google", CompanyPage.page_id == best.page_id))
    if existing:
        if existing.company_id != company.id:
            return {"status": "no_ads_found", "page_id": None, "page_name": None, "ads": [],
                   "error": f"Advertiser {best.page_id} is already linked to another company"}
    else:
        session.add(CompanyPage(
            company_id=company.id, source="google", page_id=best.page_id,
            page_name=best.name, role="main", status="confirmed",
            evidence={"method": "domain_lookup", "domain": company.website_domain},
        ))
    return {"status": "confirmed", "page_id": best.page_id, "page_name": best.name, "ads": []}


def resolve_and_record(source, session, company) -> dict:
    """Run the one-shot search+resolve for a company with no linked main page,
    record the outcome on the Company row (and CompanyPage when confirmed).
    Returns the raw resolver result (status, page_id, page_name, ads, candidates).

    Uses the CALLER's session — the pipeline commits per company."""
    result = source.search_and_resolve(company.name, country=company.country,
                                       website_domain=company.website_domain)
    company.candidates = result["candidates"] or None

    if result["status"] == "confirmed":
        company.page_id = result["page_id"]
        company.page_name = result["page_name"]
        company.resolution_status = "confirmed"
        existing = session.scalar(select(CompanyPage).where(
            CompanyPage.source == "meta", CompanyPage.page_id == result["page_id"]))
        if not existing:
            session.add(CompanyPage(
                company_id=company.id, source="meta", page_id=result["page_id"],
                page_name=result["page_name"], role="main", status="auto",
                evidence={"method": "name_search", "search_term": result.get("search_term"),
                          "similarity": (result["candidates"][0].get("similarity")
                                         if result.get("candidates") else None)},
            ))
    elif result["status"] == "ambiguous":
        company.resolution_status = "ambiguous"
        company.page_id = result.get("page_id")
        company.page_name = result.get("page_name")
    else:
        company.resolution_status = "no_ads_found"
    return result


def resolve_confirmed_handle(source, session, company) -> dict:
    """Ad-lookup helper for a company whose identity is CONFIRMED but handle-only
    (page_name/page_url known, no numeric page id yet): search the Ad Library by
    the EXACT page name the identity check found — never the legal Firmenname,
    which would re-introduce the brand≠legal-name ambiguity the identity check
    already solved. On success the numeric id is stored (CompanyPage + Company),
    making the company fetch-ready for good.

    NEVER downgrades the confirmed identity: the Ad Library only knows pages
    that advertise, so "search found nothing" just means no ads — the identity
    stays confirmed and handle-only."""
    # FREE first: the embed endpoint resolves a facebook handle to its numeric
    # id without touching the Apify quota — and works even when the page
    # currently runs no ads (which the Ad Library search below cannot).
    if "facebook.com" in (company.page_url or ""):
        from .page_id_lookup import fb_page_id_from_handle
        from .serper_source import canonicalize_fb
        canon = canonicalize_fb(company.page_url)
        pid = fb_page_id_from_handle(canon["handle"]) if canon and canon.get("handle") else None
        if pid:
            existing = session.scalar(select(CompanyPage).where(
                CompanyPage.source == "meta", CompanyPage.page_id == pid))
            if not (existing and existing.company_id != company.id):
                company.page_id = pid
                if not existing:
                    session.add(CompanyPage(
                        company_id=company.id, source="meta", page_id=pid,
                        page_name=company.page_name, role="main", status="auto",
                        evidence={"method": "embed_page_id",
                                  "url": company.page_url}))
                return {"status": "confirmed", "page_id": pid,
                        "page_name": company.page_name, "ads": []}

    result = source.search_and_resolve(company.page_name, country=company.country,
                                       website_domain=company.website_domain)
    if result["status"] == "confirmed" and result.get("page_id"):
        existing = session.scalar(select(CompanyPage).where(
            CompanyPage.source == "meta", CompanyPage.page_id == result["page_id"]))
        if existing and existing.company_id != company.id:
            # that page belongs to another company — don't steal it; treat as no ads
            return {"status": "no_ads_found", "page_id": None,
                    "page_name": company.page_name, "ads": []}
        company.page_id = result["page_id"]
        company.page_name = result["page_name"] or company.page_name
        if not existing:
            session.add(CompanyPage(
                company_id=company.id, source="meta", page_id=result["page_id"],
                page_name=result["page_name"], role="main", status="auto",
                evidence={"method": "adlib_exact_page_name",
                          "search_term": result.get("search_term")}))
        return result
    # not confirmed by the ad search — identity stays confirmed/handle-only
    return {"status": "no_ads_found", "page_id": None,
            "page_name": company.page_name, "ads": []}


def _apply_identity_result(session, company, result: dict, method: str) -> None:
    """Persist an online-identity-check outcome onto Company (+ a main CompanyPage
    when we have a numeric page_id, which is what the later Ad Lookup needs).

    A confirmed match with only a vanity HANDLE (no numeric id yet) still records
    the verified name + URL and marks the company confirmed, but creates NO
    CompanyPage — the identity is known but not yet "fetch-ready" (the Ad Lookup
    needs the numeric id). This keeps the two checks cleanly separate."""
    company.candidates = result.get("candidates") or None
    status = result["status"]

    main = session.scalar(select(CompanyPage).where(
        CompanyPage.company_id == company.id, CompanyPage.role == "main"))
    # A human-set page (manually linked, or a status the check doesn't touch) is
    # never clobbered by an automatic result — only provisional AUTO links are.
    human_set = main is not None and main.status in ("manual", "locked")

    def _drop_stale_auto():
        """Downgrade path: clear a provisional AUTO link so a wrong auto-confirm
        can't silently linger after the check no longer confirms it."""
        if main is not None and main.status == "auto":
            session.delete(main)
            company.page_id = None

    if status == "confirmed":
        if human_set:
            company.resolution_status = "confirmed"   # respect the human's page; nothing to overwrite
            return
        company.page_name = result.get("page_name")
        company.page_url = result.get("page_url")
        page_id = result.get("page_id")
        if not page_id and "facebook.com" in (result.get("page_url") or ""):
            # handle-only Facebook confirm — the embed endpoint usually yields
            # the numeric id for free, making the row fetch-ready immediately
            from .page_id_lookup import fb_page_id_from_handle
            from .serper_source import canonicalize_fb
            canon = canonicalize_fb(result["page_url"])
            if canon and canon.get("handle"):
                page_id = fb_page_id_from_handle(canon["handle"])
        if page_id:
            existing = session.scalar(select(CompanyPage).where(
                CompanyPage.source == "meta", CompanyPage.page_id == page_id))
            if existing and existing.company_id != company.id:
                # that page already belongs to another company — don't steal it;
                # downgrade to flag-for-review and drop our stale auto link
                _drop_stale_auto()
                company.resolution_status = "ambiguous"
                return
            # replace any stale AUTO main page pointing elsewhere
            for old in session.scalars(select(CompanyPage).where(
                    CompanyPage.company_id == company.id, CompanyPage.role == "main",
                    CompanyPage.status == "auto", CompanyPage.page_id != page_id)):
                session.delete(old)
            company.page_id = page_id
            company.resolution_status = "confirmed"
            company.confirmed_at = dt.datetime.utcnow()
            if not existing:
                session.add(CompanyPage(
                    company_id=company.id, source="meta", page_id=page_id,
                    page_name=result.get("page_name"), role="main", status="auto",
                    evidence={"method": method, "url": result.get("page_url"),
                              "search_term": result.get("search_term")}))
        else:
            # handle-only confirm: identity known but not fetch-ready (no numeric
            # id). Drop any stale auto link whose numeric id is now unverified.
            _drop_stale_auto()
            company.resolution_status = "confirmed"
            company.confirmed_at = dt.datetime.utcnow()
    elif status == "ambiguous":
        if human_set:
            return   # keep the human's confirmed page; just refreshed candidates
        company.page_name = result.get("page_name")
        company.page_url = result.get("page_url")
        _drop_stale_auto()
        company.resolution_status = "ambiguous"
    else:
        if human_set:
            return
        _drop_stale_auto()
        # an honest "no page found" must not keep showing a stale link
        company.page_name = None
        company.page_url = None
        company.resolution_status = "no_ads_found"   # "no page found" in the identity context


def run_identity_check(company_id: int, backend: str = "serper") -> dict:
    """ONLINE IDENTITY CHECK for ONE company — resolve its Facebook page WITHOUT
    fetching ads (serper/Google by default; Apify name-search as fallback). No
    ad storage, no partner-hub sweep, no Apify ad quota when using serper.

    A `locked` company is skipped untouched — a locked identity is a human
    decision the check must never override."""
    with SessionLocal() as s:
        company = s.get(Company, company_id)
        if not company:
            return {"status": "error", "error": "company not found"}
        if company.resolution_status == "locked":
            return {"status": "skipped_locked", "page_id": company.page_id,
                    "page_name": company.page_name}

        # STEP 1 — crawl the company's OWN website once (when it has one): a
        # social link in it is the company declaring its own page — free, no
        # API/LLM, immune to name-search failures. The crawl's page text is
        # kept and later feeds the LLM stages of the serper tier. The website
        # tier itself is skipped when a human already fixed the page.
        main = s.scalar(select(CompanyPage).where(
            CompanyPage.company_id == company.id, CompanyPage.role == "main"))
        human_set = main is not None and main.status in ("manual", "locked")
        crawl = None
        wres = None
        if company.website_domain:
            try:
                from . import website_source
                crawl = website_source.crawl_site(company.website_domain)
                if crawl and not human_set:
                    wres = website_source.resolve_from_website(
                        company.website_domain, country=company.country, crawl=crawl)
            except Exception:
                wres = None
        if wres and not human_set:
            if wres["status"] == "confirmed":
                if wres.get("lock") and wres.get("page_id"):
                    # Authoritative AND fetch-ready → hard-lock so no future
                    # auto-check overrides the company's own declaration.
                    try:
                        add_page(company.id, wres["page_id"], wres.get("page_name"),
                                 role="main", status="locked",
                                 evidence={"method": "website_footer",
                                           "url": wres.get("page_url")})
                    except ValueError:
                        wres = None   # page already linked elsewhere → fall through
                    if wres:
                        s.expire_all()
                        company = s.get(Company, company_id)
                        company.page_url = wres.get("page_url")
                        s.commit()
                        return {"status": company.resolution_status,
                                "page_id": company.page_id, "page_name": company.page_name,
                                "page_url": company.page_url,
                                "candidates": len(wres.get("candidates") or []),
                                "backend": "website"}
                else:
                    # Handle-only match: confirmed but not locked (a later ad
                    # lookup can still resolve the numeric id).
                    _apply_identity_result(s, company, wres, method="website_footer")
                    s.commit()
                    return {"status": company.resolution_status, "page_id": company.page_id,
                            "page_name": company.page_name, "page_url": company.page_url,
                            "candidates": len(wres.get("candidates") or []),
                            "backend": "website"}

        use_serper = backend == "serper" and config.SERPER_API_KEY
        if use_serper:
            from . import serper_source
            # Everything we know about the company feeds the LLM stages —
            # the judge compares candidates against this profile, and the
            # keyword generator derives likely page names from it.
            profile = {k: getattr(company, k, None) for k in
                       ("street", "postal_code", "segment", "sub_segment",
                        "sales_channel", "email")}
            profile = {k: v for k, v in profile.items() if v}
            result = serper_source.resolve_identity(
                company.name, country=company.country,
                website_domain=company.website_domain, city=company.city,
                company=profile or None,
                site_text=crawl["text"] if crawl else None)
            _apply_identity_result(s, company, result, method="serper")
        else:
            from ..collect.meta_source import MetaAdSource
            result = resolve_and_record(MetaAdSource(), s, company)  # Apify fallback; ads discarded
        s.commit()
        return {"status": company.resolution_status, "page_id": company.page_id,
                "page_name": company.page_name, "page_url": company.page_url,
                "candidates": len(result.get("candidates") or []),
                "backend": "serper" if use_serper else "apify"}
