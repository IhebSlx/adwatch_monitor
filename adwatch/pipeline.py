"""Pipeline orchestration: seed companies, run one collection cycle.

For a company with no confirmed page yet, one Apify run both resolves identity
(by grouping returned ads by page and matching the name) AND returns this
week's ads for that page — so identity-checking costs nothing extra. Once
confirmed, later weeks fetch directly by page_id (cheaper, precise, and a
`0 active ads` result is then a trustworthy fact rather than a name-mismatch
guess)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from . import config
from .aggregate import aggregate
from .classify import classify_ad
from .db import SessionLocal, init_db
from .models import Ad, CollectionRun, Company, WeeklyCompanyMetric
from .sources.meta import MetaAdSource


def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def seed_companies_if_empty() -> int:
    with SessionLocal() as s:
        if s.scalar(select(Company).limit(1)):
            return 0
        added = 0
        for c in config.load_companies():
            s.add(Company(
                name=c["name"], country=c.get("country", config.DEFAULT_COUNTRY), source="meta",
                page_id=c.get("page_id"), resolution_status=c.get("resolution_status", "pending"),
                notes=c.get("notes"),
            ))
            added += 1
        s.commit()
        return added


def reseed_from_file() -> int:
    """Destructive: wipe companies + derived data and reload from the YAML file."""
    with SessionLocal() as s:
        s.query(Ad).delete()
        s.query(CollectionRun).delete()
        s.query(WeeklyCompanyMetric).delete()
        s.query(Company).delete()
        s.commit()
    return seed_companies_if_empty()


def _store_ads(s, run, raw_ads) -> list[dict]:
    classified = []
    for ad in raw_ads:
        result = classify_ad(ad.ad_text or "")
        s.add(Ad(
            run_id=run.id, source="meta", external_ad_id=ad.external_ad_id,
            ad_text=ad.ad_text, cta=ad.cta, start_date=ad.start_date, end_date=ad.end_date,
            is_active=ad.is_active, media_type=ad.media_type, reach=ad.reach,
            real_spend=ad.real_spend, category=result["category"], product=result["product"],
            classifier=result["classifier"], classifier_raw=result, source_raw=ad.source_raw,
        ))
        classified.append({"raw": ad, "category": result["category"], "product": result["product"]})
    return classified


def _store_metrics(s, company, week_start, classified, status) -> None:
    metrics = aggregate(classified)
    existing = s.scalar(select(WeeklyCompanyMetric).where(
        WeeklyCompanyMetric.company_id == company.id,
        WeeklyCompanyMetric.source == "meta",
        WeeklyCompanyMetric.week_start == week_start,
    ))
    target = existing or WeeklyCompanyMetric(company_id=company.id, source="meta", week_start=week_start)
    for k, v in metrics.items():
        setattr(target, k, v)
    target.status = status
    if existing is None:
        s.add(target)


def run_once() -> dict:
    """Collect + resolve + classify + aggregate + store for all companies."""
    init_db()
    seed_companies_if_empty()
    source = MetaAdSource()
    week_start = monday_of(dt.date.today())
    summary = {"week_start": week_start.isoformat(), "backend": source.backend,
               "companies": 0, "collected": 0, "errors": 0}

    with SessionLocal() as s:
        companies = list(s.scalars(select(Company)))
        summary["companies"] = len(companies)

        for company in companies:
            run = CollectionRun(company_id=company.id, source="meta", week_start=week_start)
            s.add(run)
            s.flush()

            try:
                if company.resolution_status == "confirmed" and company.page_id:
                    raw_ads = source.fetch_ads(company.page_id, country=company.country, active_only=True)
                    run.status = "ok" if raw_ads else "no_active_ads"
                else:
                    result = source.search_and_resolve(company.name, country=company.country)
                    raw_ads = result["ads"]
                    company.candidates = result["candidates"] or None
                    if result["status"] == "confirmed":
                        company.page_id = result["page_id"]
                        company.page_name = result["page_name"]
                        company.resolution_status = "confirmed"
                        run.status = "ok" if raw_ads else "no_active_ads"
                    elif result["status"] == "ambiguous":
                        company.resolution_status = "ambiguous"
                        company.page_id = result.get("page_id")
                        company.page_name = result.get("page_name")
                        run.status = "ambiguous_match"
                    else:  # no_ads_found
                        company.resolution_status = "no_ads_found"
                        run.status = "no_ads_found"
            except Exception as exc:  # noqa: BLE001
                run.status = "error"
                run.error = str(exc)[:1000]
                summary["errors"] += 1
                s.commit()
                continue

            run.ads_scraped = len(raw_ads)
            classified = _store_ads(s, run, raw_ads)
            _store_metrics(s, company, week_start, classified, run.status)

            summary["collected"] += 1
            s.commit()

    return summary
