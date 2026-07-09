"""Thin data-access layer for the dashboard: company CRUD + metric queries.

Identity mutations (linking/unlinking pages) live in adwatch.identity.resolver;
this module only reads/aggregates for display, plus basic company CRUD."""
from __future__ import annotations

from sqlalchemy import select

from . import config
from .db import SessionLocal
from .models import Ad, CollectionRun, Company, CompanyPage, ReportRecipient, WeeklyCompanyMetric

STATUS_LABELS = {
    "pending": "Not fetched yet",
    "confirmed": "Confirmed page",
    "ambiguous": "Multiple matches — best guess used, please verify",
    "no_ads_found": "No ads found under this name — check spelling or verify manually",
}

PAGE_STATUS_LABELS = {"confirmed": "confirmed", "auto": "auto-linked", "manual": "manually set"}


def list_companies() -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(select(Company).order_by(Company.name)).all()
        out = []
        for c in rows:
            pages = s.scalars(select(CompanyPage)
                              .where(CompanyPage.company_id == c.id, CompanyPage.active)
                              .order_by(CompanyPage.role, CompanyPage.linked_at)).all()
            out.append({
                "id": c.id, "name": c.name,
                "resolution_status": c.resolution_status,
                "status_label": STATUS_LABELS.get(c.resolution_status, c.resolution_status),
                "page_name": c.page_name,
                "page_id": c.page_id,
                "candidates": c.candidates,
                "pages": [{
                    "id": p.id, "page_id": p.page_id, "page_name": p.page_name,
                    "role": p.role, "status": p.status,
                    "status_label": PAGE_STATUS_LABELS.get(p.status, p.status),
                    "evidence": p.evidence,
                } for p in pages],
            })
        return out


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
            # Renaming invalidates every prior page match — re-resolve next run.
            c.name = name
            c.resolution_status = "pending"
            c.page_id = None
            c.page_name = None
            c.candidates = None
            for p in s.scalars(select(CompanyPage).where(CompanyPage.company_id == cid)):
                s.delete(p)
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
        s.query(CompanyPage).filter(CompanyPage.company_id == cid).delete(synchronize_session=False)
        s.delete(c)
        s.commit()


def latest_metrics() -> list[dict]:
    """Latest weekly metric per company + previous-week context, score, freshness."""
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
            n_pages = s.scalar(select(CompanyPage.id)
                               .where(CompanyPage.company_id == c.id, CompanyPage.active)
                               .limit(1))
            out.append({
                "company_id": c.id,
                "company": c.name,
                "resolution_status": c.resolution_status,
                "status_label": STATUS_LABELS.get(c.resolution_status, c.resolution_status),
                "page_name": c.page_name,
                "has_pages": n_pages is not None,
                "has_data": latest is not None,
                "week_start": latest.week_start.isoformat() if latest else None,
                "total_active_ads": latest.total_active_ads if latest else None,
                "prev_total": prev.total_active_ads if prev else None,
                "delta_ads": (latest.total_active_ads - prev.total_active_ads)
                             if (latest and prev) else None,
                "new_ads": latest.new_ads if latest else None,
                "score": latest.score if latest else None,
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
                "new_ads": m.new_ads,
                "score": m.score,
                "spend_low": m.estimated_spend_low,
                "spend_high": m.estimated_spend_high,
            })
        return out


def latest_week_detail(company_id: int) -> dict:
    """Everything collected for a company in its most recent week, grouped by
    the page each ad came from — the drill-down view."""
    with SessionLocal() as s:
        latest_run = s.scalar(
            select(CollectionRun)
            .where(CollectionRun.company_id == company_id)
            .order_by(CollectionRun.run_date.desc())
            .limit(1)
        )
        if not latest_run:
            return {"has_run": False, "pages": [], "ads": []}
        week = latest_run.week_start
        has_linked_page = s.scalar(select(CompanyPage.id).where(
            CompanyPage.company_id == company_id, CompanyPage.active).limit(1)) is not None

        runs = s.scalars(
            select(CollectionRun)
            .where(CollectionRun.company_id == company_id,
                   CollectionRun.week_start == week)
            .order_by(CollectionRun.run_date.desc())
        ).all()
        newest_per_page: dict[str, CollectionRun] = {}
        for r in runs:
            # A page_id=NULL run only ever means "identity not resolved yet" (an
            # early ambiguous_match attempt). Once the company has a real linked
            # page, that placeholder is stale and superseded — without dropping
            # it, its ads would double up alongside the real page's ads. Runs
            # with a REAL page_id (main, partner, or a per-ad hub attribution)
            # are never filtered here — each contributes its own ads.
            if r.page_id is None and has_linked_page:
                continue
            key = r.page_id or "?"
            if key not in newest_per_page:
                newest_per_page[key] = r
        # No confirmed pages yet (still ambiguous/pending) -> fall back to
        # whatever was last fetched, so the user still sees something.
        if not newest_per_page and runs:
            newest_per_page["?"] = runs[0]

        pages, ads = [], []
        for r in newest_per_page.values():
            r_ads = s.scalars(select(Ad).where(Ad.run_id == r.id).order_by(Ad.category)).all()
            pages.append({
                "page_id": r.page_id, "page_name": r.page_name, "role": r.page_role,
                "status": r.status, "ads": len(r_ads),
                "run_date": r.run_date.isoformat(timespec="minutes"),
            })
            for a in r_ads:
                ads.append({
                    "page_name": r.page_name, "page_role": r.page_role,
                    "category": a.category, "product": a.product, "cta": a.cta,
                    "media_type": a.media_type, "reach": a.reach,
                    "start_date": a.start_date.isoformat() if a.start_date else None,
                    "ad_text": (a.ad_text or "")[:400],
                    "classifier": a.classifier,
                    "ad_library_url": a.ad_library_url,
                    "landing_url": a.landing_url,
                })
        return {"has_run": True, "week_start": week.isoformat(), "pages": pages, "ads": ads}


# ---------------------------------------------------------------------------
# Report recipients — managed entirely in-app (see adwatch.emailer)
# ---------------------------------------------------------------------------

def list_recipients() -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(select(ReportRecipient).order_by(ReportRecipient.added_at)).all()
        return [{"id": r.id, "name": r.name, "email": r.email, "active": r.active} for r in rows]


def add_recipient(email: str, name: str | None = None) -> dict:
    email = (email or "").strip()
    if not email or "@" not in email:
        raise ValueError("A valid email address is required")
    with SessionLocal() as s:
        existing = s.scalar(select(ReportRecipient).where(ReportRecipient.email == email))
        if existing:
            if existing.active:
                raise ValueError("That recipient is already in the list")
            existing.active = True
            s.commit()
            return {"id": existing.id, "email": existing.email}
        r = ReportRecipient(email=email, name=(name or "").strip() or None, active=True)
        s.add(r)
        s.commit()
        return {"id": r.id, "email": r.email}


def delete_recipient(rid: int) -> None:
    with SessionLocal() as s:
        r = s.get(ReportRecipient, rid)
        if r:
            s.delete(r)
            s.commit()
