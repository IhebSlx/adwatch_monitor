"""Thin data-access layer for the dashboard: company CRUD + metric queries.

Identity mutations (linking/unlinking pages) live in adwatch.identity.resolver;
this module only reads/aggregates for display, plus basic company CRUD."""
from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import func, select

from . import config
from .db import SessionLocal
from .models import (
    Ad, CollectionRun, Company, CompanyPage, ReportRecipient, ScheduleConfig, WeeklyCompanyMetric,
)

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
                "website_domain": c.website_domain,
                "resolution_status": c.resolution_status,
                "status_label": STATUS_LABELS.get(c.resolution_status, c.resolution_status),
                "page_name": c.page_name,
                "page_id": c.page_id,
                "candidates": c.candidates,
                "google_status": next((p.status for p in pages if p.source == "google"), None),
                "pages": [{
                    "id": p.id, "source": p.source, "page_id": p.page_id, "page_name": p.page_name,
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


def update_company_domain(cid: int, domain: str | None) -> None:
    """Website domain used to resolve the company's Google Ads advertiser (see
    collect/google_source.py) — independent of the name/Meta identity, so
    changing it does NOT reset any existing page links."""
    domain = (domain or "").strip().lower() or None
    if domain:
        for prefix in ("https://", "http://"):
            if domain.startswith(prefix):
                domain = domain[len(prefix):]
        domain = domain.split("/")[0].removeprefix("www.")
    with SessionLocal() as s:
        c = s.get(Company, cid)
        if not c:
            raise ValueError("Not found")
        c.website_domain = domain
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


def _merge_week_rows(rows: list[WeeklyCompanyMetric]) -> dict:
    """Combine one week's metric rows across sources (meta + google) into one
    view — sums counts/spend, unions categories/products — so a company
    tracked on both platforms gets one coherent weekly number instead of the
    dashboard arbitrarily picking whichever source's row happened to be last."""
    cats = Counter()
    products: list[str] = []
    for r in rows:
        for k, v in (r.ads_by_category or {}).items():
            cats[k] += v
        for p in (r.products or []):
            if p not in products:
                products.append(p)
    methods = {r.spend_method for r in rows if r.spend_method}
    by_source = {r.source: r.total_active_ads for r in rows}
    return {
        "week_start": rows[0].week_start,
        "total_active_ads": sum(r.total_active_ads for r in rows),
        "meta_active_ads": by_source.get("meta", 0),
        "google_active_ads": by_source.get("google", 0),
        "new_ads": sum(r.new_ads for r in rows),
        "ads_by_category": dict(cats),
        "products": products,
        "estimated_spend_low": sum(r.estimated_spend_low for r in rows),
        "estimated_spend_high": sum(r.estimated_spend_high for r in rows),
        "spend_method": methods.pop() if len(methods) == 1 else ("mixed" if methods else None),
        "status": next((r.status for r in rows if r.status == "ok"), rows[0].status),
    }


def _merged_weekly_series(s, company_id: int) -> list[dict]:
    """Every week for one company, oldest first, merged across sources, with
    score recomputed on the combined totals (score is a function of total ads
    + momentum, so it must be computed AFTER merging, not per-source)."""
    from .insights.score import company_score
    rows = s.scalars(
        select(WeeklyCompanyMetric)
        .where(WeeklyCompanyMetric.company_id == company_id)
        .order_by(WeeklyCompanyMetric.week_start)
    ).all()
    by_week: dict = {}
    for r in rows:
        by_week.setdefault(r.week_start, []).append(r)

    out = []
    prev_total = None
    for week_start in sorted(by_week):
        merged = _merge_week_rows(by_week[week_start])
        cats = merged["ads_by_category"]
        categories_active = sum(1 for c in ("recruitment", "product_sale",
                                            "brand_awareness", "event_promo") if cats.get(c))
        merged["score"] = company_score(merged["total_active_ads"], prev_total,
                                         merged["new_ads"], categories_active)
        prev_total = merged["total_active_ads"]
        out.append(merged)
    return out


def latest_metrics(company_ids: list[int] | None = None) -> list[dict]:
    """Latest weekly metric per company (merged across sources) + previous-week
    context, score, freshness. `company_ids` restricts the result to that set
    (e.g. a Companies Explorer filter), in DB order (name) when omitted."""
    with SessionLocal() as s:
        stmt = select(Company).order_by(Company.name)
        if company_ids is not None:
            stmt = stmt.where(Company.id.in_(company_ids))
        companies = s.scalars(stmt).all()
        out = []
        for c in companies:
            weeks = _merged_weekly_series(s, c.id)
            latest = weeks[-1] if weeks else None
            prev = weeks[-2] if len(weeks) > 1 else None
            n_pages = s.scalar(select(CompanyPage.id)
                               .where(CompanyPage.company_id == c.id, CompanyPage.active)
                               .limit(1))
            out.append({
                "company_id": c.id,
                "company": c.name,
                "resolution_status": c.resolution_status,
                "status_label": STATUS_LABELS.get(c.resolution_status, c.resolution_status),
                "page_name": c.page_name,
                "segment": c.segment,
                "sub_segment": c.sub_segment,
                "kv": c.kv,
                "revenue_y0": c.revenue_y0,
                "revenue_y1": c.revenue_y1,
                "revenue_y2": c.revenue_y2,
                "revenue_y3": c.revenue_y3,
                "revenue_y4": c.revenue_y4,
                "imported_at": c.imported_at.isoformat() if c.imported_at else None,
                "has_pages": n_pages is not None,
                "has_data": latest is not None,
                "week_start": latest["week_start"].isoformat() if latest else None,
                "total_active_ads": latest["total_active_ads"] if latest else None,
                "meta_active_ads": latest["meta_active_ads"] if latest else 0,
                "google_active_ads": latest["google_active_ads"] if latest else 0,
                "prev_total": prev["total_active_ads"] if prev else None,
                "delta_ads": (latest["total_active_ads"] - prev["total_active_ads"])
                             if (latest and prev) else None,
                "new_ads": latest["new_ads"] if latest else None,
                "score": latest["score"] if latest else None,
                "ads_by_category": latest["ads_by_category"] if latest else {},
                "products": latest["products"] if latest else [],
                "spend_low": latest["estimated_spend_low"] if latest else None,
                "spend_high": latest["estimated_spend_high"] if latest else None,
                "spend_method": latest["spend_method"] if latest else None,
                "status": latest["status"] if latest else None,
            })
        return out


def company_history(company_id: int) -> list[dict]:
    """All weekly metric rows for one company (merged across sources), oldest
    first — for trend charts."""
    with SessionLocal() as s:
        weeks = _merged_weekly_series(s, company_id)
        out = []
        for m in weeks:
            cats = m["ads_by_category"]
            out.append({
                "week_start": m["week_start"].isoformat(),
                "total_active_ads": m["total_active_ads"],
                "recruitment": cats.get("recruitment", 0),
                "product_sale": cats.get("product_sale", 0),
                "brand_awareness": cats.get("brand_awareness", 0),
                "event_promo": cats.get("event_promo", 0),
                "new_ads": m["new_ads"],
                "score": m["score"],
                "spend_low": m["estimated_spend_low"],
                "spend_high": m["estimated_spend_high"],
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
                "source": r.source, "page_id": r.page_id, "page_name": r.page_name, "role": r.page_role,
                "status": r.status, "ads": len(r_ads),
                "run_date": r.run_date.isoformat(timespec="minutes"),
            })
            for a in r_ads:
                ads.append({
                    "source": r.source, "page_name": r.page_name, "page_role": r.page_role,
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
# Logs — every fetch attempt (CollectionRun), across all companies. This is
# the raw truth behind a FetchJob's "N errors" count: the job log only ever
# shows a per-company ✓ (it didn't crash the job) with no error text, so this
# is the only place the actual failure reason (e.g. an Apify quota error) is
# visible — previously only reachable by querying the database directly.
# ---------------------------------------------------------------------------

def list_logs(status: str | None = None, source: str | None = None, q: str | None = None,
             page: int = 1, page_size: int = 50) -> dict:
    with SessionLocal() as s:
        stmt = select(CollectionRun, Company.name).join(Company, Company.id == CollectionRun.company_id)
        if status:
            stmt = stmt.where(CollectionRun.status == status)
        if source:
            stmt = stmt.where(CollectionRun.source == source)
        if q:
            stmt = stmt.where(Company.name.ilike(f"%{q.strip()}%"))
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        stmt = stmt.order_by(CollectionRun.run_date.desc()).limit(page_size).offset((page - 1) * page_size)
        rows = [{
            "id": run.id, "company_id": run.company_id, "company": name,
            "source": run.source, "run_date": run.run_date.isoformat(timespec="minutes"),
            "week_start": run.week_start.isoformat(), "page_id": run.page_id,
            "page_name": run.page_name, "page_role": run.page_role,
            "status": run.status, "ads_scraped": run.ads_scraped, "error": run.error,
        } for run, name in s.execute(stmt).all()]
        return {"total": total, "page": page, "page_size": page_size, "rows": rows}


def clear_logs() -> dict:
    """Clear the fetch log: delete every CollectionRun that carries NO stored
    ads (errors, no-ads runs, skips — the noise). Runs WITH ads are kept —
    they anchor the collected ad copies (ads.run_id), which clearing a log
    view must never destroy. Returns {deleted, kept}."""
    from sqlalchemy import delete as _delete
    from .models import Ad
    with SessionLocal() as s:
        with_ads = select(Ad.run_id).where(Ad.run_id.is_not(None)).distinct()
        doomed = list(s.scalars(select(CollectionRun.id).where(CollectionRun.id.not_in(with_ads))))
        if doomed:
            s.execute(_delete(CollectionRun).where(CollectionRun.id.in_(doomed)))
            s.commit()
        kept = s.scalar(select(func.count()).select_from(CollectionRun)) or 0
        return {"deleted": len(doomed), "kept": kept}


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


# ---------------------------------------------------------------------------
# Schedule config — single row (id=1), read by the in-process scheduler
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _schedule_to_dict(r: ScheduleConfig) -> dict:
    return {
        "fetch_enabled": r.fetch_enabled, "fetch_day": r.fetch_day, "fetch_time": r.fetch_time,
        "fetch_sources": r.fetch_sources or ["meta"],
        "send_enabled": r.send_enabled, "send_day": r.send_day, "send_time": r.send_time,
        "send_report": r.send_report,
    }


def get_schedule() -> dict:
    with SessionLocal() as s:
        r = s.get(ScheduleConfig, 1)
        if not r:
            r = ScheduleConfig(id=1)
            s.add(r)
            s.commit()
        return _schedule_to_dict(r)


def save_schedule(**fields) -> dict:
    for key in ("fetch_time", "send_time"):
        if key in fields and fields[key] is not None and not _TIME_RE.match(fields[key]):
            raise ValueError(f"{key} must be 'HH:MM' 24h format")
    for key in ("fetch_day", "send_day"):
        if key in fields and fields[key] is not None and not (0 <= int(fields[key]) <= 6):
            raise ValueError(f"{key} must be 0 (Mon) .. 6 (Sun)")
    if fields.get("send_report") not in (None, "top5", "full"):
        raise ValueError("send_report must be 'top5' or 'full'")
    if fields.get("fetch_sources") is not None:
        bad = [s for s in fields["fetch_sources"] if s not in ("meta", "google")]
        if bad or not fields["fetch_sources"]:
            raise ValueError("fetch_sources must be a non-empty list of 'meta'/'google'")
    with SessionLocal() as s:
        r = s.get(ScheduleConfig, 1)
        if not r:
            r = ScheduleConfig(id=1)
            s.add(r)
        for key, value in fields.items():
            if value is not None and hasattr(r, key):
                setattr(r, key, value)
        s.commit()
        return _schedule_to_dict(r)
