"""Thin data-access layer for the dashboard: company CRUD + metric queries."""
from __future__ import annotations

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
