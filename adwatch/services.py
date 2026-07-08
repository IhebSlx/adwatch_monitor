"""Thin data-access layer for the dashboard: company CRUD + metric queries."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from . import config
from .db import SessionLocal
from .models import Ad, CollectionRun, Company, WeeklyCompanyMetric

STATUS_LABELS = {
    "pending": "Not fetched yet",
    "confirmed": "Confirmed page",
    "ambiguous": "Multiple matches — best guess used, please verify",
    "no_ads_found": "No ads found under this name — check spelling or verify manually",
}


def list_companies() -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(select(Company).order_by(Company.name)).all()
        return [{
            "id": c.id, "name": c.name,
            "resolution_status": c.resolution_status,
            "status_label": STATUS_LABELS.get(c.resolution_status, c.resolution_status),
            "page_name": c.page_name,
            "page_id": c.page_id,
            "candidates": c.candidates,
        } for c in rows]


def add_company(name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Name required")
    with SessionLocal() as s:
        if s.scalar(select(Company).where(Company.name == name)):
            raise ValueError("A company with that name already exists")
        c = Company(name=name, country=config.DEFAULT_COUNTRY, source="meta", resolution_status="pending")
        s.add(c)
        s.commit()
        return {"id": c.id, "name": c.name}


def update_company(cid: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name required")
    with SessionLocal() as s:
        c = s.get(Company, cid)
        if not c:
            raise ValueError("Not found")
        dupe = s.scalar(select(Company).where(Company.name == name, Company.id != cid))
        if dupe:
            raise ValueError("A company with that name already exists")
        if name != c.name:
            # Renaming invalidates any prior page match — re-resolve next run.
            c.name = name
            c.resolution_status = "pending"
            c.page_id = None
            c.page_name = None
            c.candidates = None
        s.commit()


def set_company_page(cid: int, page_id: str, page_name: str | None = None,
                     page_category: str | None = None) -> None:
    """Manually lock a company to a specific Facebook page id (the human-confirm path
    for the identity problem). Marks it confirmed so future fetches hit this exact page."""
    page_id = (page_id or "").strip()
    if not page_id:
        raise ValueError("A page ID is required")
    with SessionLocal() as s:
        c = s.get(Company, cid)
        if not c:
            raise ValueError("Not found")
        c.page_id = page_id
        c.page_name = (page_name or "").strip() or None
        c.page_category = page_category
        c.resolution_status = "confirmed"
        c.confirmed_at = dt.datetime.utcnow()
        c.candidates = None
        s.commit()


def clear_resolution(cid: int) -> None:
    """Reset a company back to pending (forget the matched page)."""
    with SessionLocal() as s:
        c = s.get(Company, cid)
        if not c:
            return
        c.page_id = None
        c.page_name = None
        c.resolution_status = "pending"
        c.candidates = None
        s.commit()


def find_candidates(term: str, country: str | None = None) -> dict:
    """Live keyword search that returns candidate pages WITHOUT storing anything —
    powers the manual 'find the right page' step. Costs one Apify call."""
    from .sources.meta import MetaAdSource
    src = MetaAdSource()
    res = src.search_and_resolve(term, country=country or config.DEFAULT_COUNTRY, max_ads=60)
    return {"status": res["status"], "search_term": res.get("search_term", term),
            "candidates": res.get("candidates", [])}


def delete_company(cid: int) -> None:
    with SessionLocal() as s:
        c = s.get(Company, cid)
        if not c:
            return
        run_ids = [r.id for r in s.scalars(select(CollectionRun).where(CollectionRun.company_id == cid))]
        if run_ids:
            s.query(Ad).filter(Ad.run_id.in_(run_ids)).delete(synchronize_session=False)
        s.query(CollectionRun).filter(CollectionRun.company_id == cid).delete(synchronize_session=False)
        s.query(WeeklyCompanyMetric).filter(WeeklyCompanyMetric.company_id == cid).delete(synchronize_session=False)
        s.delete(c)
        s.commit()


def latest_metrics() -> list[dict]:
    """Latest weekly metric per company + previous week delta on ad count."""
    with SessionLocal() as s:
        companies = s.scalars(select(Company).order_by(Company.name)).all()
        out = []
        for c in companies:
            metrics = s.scalars(
                select(WeeklyCompanyMetric)
                .where(WeeklyCompanyMetric.company_id == c.id)
                .order_by(WeeklyCompanyMetric.week_start.desc())
            ).all()
            latest = metrics[0] if metrics else None
            prev = metrics[1] if len(metrics) > 1 else None
            out.append({
                "company_id": c.id,
                "company": c.name,
                "resolution_status": c.resolution_status,
                "status_label": STATUS_LABELS.get(c.resolution_status, c.resolution_status),
                "page_name": c.page_name,
                "has_data": latest is not None,
                "week_start": latest.week_start.isoformat() if latest else None,
                "total_active_ads": latest.total_active_ads if latest else None,
                "delta_ads": (latest.total_active_ads - prev.total_active_ads)
                             if (latest and prev) else None,
                "ads_by_category": latest.ads_by_category if latest else {},
                "products": latest.products if latest else [],
                "spend_low": latest.estimated_spend_low if latest else None,
                "spend_high": latest.estimated_spend_high if latest else None,
                "spend_method": latest.spend_method if latest else None,
                "status": latest.status if latest else None,
            })
        return out


def company_history(company_id: int) -> list[dict]:
    """All weekly metric rows for one company, oldest first — for trend charts."""
    with SessionLocal() as s:
        rows = s.scalars(
            select(WeeklyCompanyMetric)
            .where(WeeklyCompanyMetric.company_id == company_id)
            .order_by(WeeklyCompanyMetric.week_start)
        ).all()
        out = []
        for m in rows:
            cats = m.ads_by_category or {}
            out.append({
                "week_start": m.week_start.isoformat(),
                "total_active_ads": m.total_active_ads,
                "recruitment": cats.get("recruitment", 0),
                "product_sale": cats.get("product_sale", 0),
                "brand_awareness": cats.get("brand_awareness", 0),
                "event_promo": cats.get("event_promo", 0),
                "spend_low": m.estimated_spend_low,
                "spend_high": m.estimated_spend_high,
            })
        return out


def latest_run_ads(company_id: int) -> dict:
    """The individual ads from a company's most recent collection run (drill-down)."""
    with SessionLocal() as s:
        run = s.scalar(
            select(CollectionRun)
            .where(CollectionRun.company_id == company_id)
            .order_by(CollectionRun.run_date.desc())
            .limit(1)
        )
        if not run:
            return {"has_run": False, "ads": []}
        ads = s.scalars(select(Ad).where(Ad.run_id == run.id).order_by(Ad.category)).all()
        return {
            "has_run": True,
            "run_date": run.run_date.isoformat(timespec="minutes"),
            "week_start": run.week_start.isoformat(),
            "status": run.status,
            "ads_scraped": run.ads_scraped,
            "ads": [{
                "category": a.category,
                "product": a.product,
                "cta": a.cta,
                "media_type": a.media_type,
                "reach": a.reach,
                "start_date": a.start_date.isoformat() if a.start_date else None,
                "ad_text": (a.ad_text or "")[:400],
                "classifier": a.classifier,
            } for a in ads],
        }
