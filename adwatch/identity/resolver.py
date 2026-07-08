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
            c = s.get(Company, company_id)
            if c:
                c.page_id, c.page_name = page_id, page_name
                c.resolution_status = "confirmed" if status in ("confirmed", "manual") else "ambiguous"
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

def find_candidates(term: str, country: str | None = None) -> dict:
    """Live keyword search that returns candidate pages WITHOUT storing anything.
    Costs one Apify call."""
    from ..collect.meta_source import MetaAdSource
    src = MetaAdSource()
    res = src.search_and_resolve(term, country=country or config.DEFAULT_COUNTRY, max_ads=60)
    return {"status": res["status"], "search_term": res.get("search_term", term),
            "candidates": res.get("candidates", [])}


# ---------------------------------------------------------------------------
# Pipeline hook: resolve an unresolved company during a collection run
# ---------------------------------------------------------------------------

def resolve_and_record(source, session, company) -> dict:
    """Run the one-shot search+resolve for a company with no linked main page,
    record the outcome on the Company row (and CompanyPage when confirmed).
    Returns the raw resolver result (status, page_id, page_name, ads, candidates).

    Uses the CALLER's session — the pipeline commits per company."""
    result = source.search_and_resolve(company.name, country=company.country)
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
