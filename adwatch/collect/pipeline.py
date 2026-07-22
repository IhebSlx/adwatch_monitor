"""PART 2 — collection pipeline: fetch + store one weekly cycle.

Per company:
  - every linked page (main + partner accounts) is fetched by exact page_id
  - a company with no linked page yet is resolved first (identity.resolver),
    which costs the same single Apify call as the data pull
Then one partner-hub sweep (identity.partner_linker) discovers partner
accounts and attributes their ads. Finally each company's weekly metric row
is computed across ALL of its pages, including score (insights.score).

Every fetch is recorded as a CollectionRun carrying page attribution, so the
UI can show exactly which pages contributed which ads."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from .. import config
from ..db import SessionLocal, init_db
from ..identity import partner_linker, resolver
from ..insights.aggregate import aggregate
from ..insights.classify import classify_ad
from ..insights.score import company_score
from ..models import Ad, CollectionRun, Company, CompanyPage, WeeklyCompanyMetric
from .meta_source import ApifyQuotaError, MetaAdSource


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
        s.query(CompanyPage).delete()
        s.query(Company).delete()
        s.commit()
    return seed_companies_if_empty()


# ---------------------------------------------------------------------------
# storage helpers
# ---------------------------------------------------------------------------

def _llm_result_cache(s) -> dict[str, dict]:
    """external_ad_id -> previous LLM classification. Ads repeat week to week;
    reusing the stored LLM verdict avoids paying to classify the same text twice.
    (Deterministic results are free, so those are always recomputed and pick up
    rule improvements.)"""
    rows = s.execute(select(Ad.external_ad_id, Ad.category, Ad.product, Ad.classifier_raw)
                     .where(Ad.classifier == "llm", Ad.external_ad_id.is_not(None))).all()
    return {r[0]: {"category": r[1], "product": r[2], "classifier": "llm",
                   **({"cached": True})} for r in rows}


def _store_ads(s, run, raw_ads, llm_cache, source: str = "meta") -> list[dict]:
    classified = []
    for ad in raw_ads:
        cached = llm_cache.get(ad.external_ad_id) if ad.external_ad_id else None
        result = cached or classify_ad(ad.ad_text or "")
        s.add(Ad(
            run_id=run.id, source=source, external_ad_id=ad.external_ad_id,
            ad_text=ad.ad_text, cta=ad.cta, start_date=ad.start_date, end_date=ad.end_date,
            is_active=ad.is_active, media_type=ad.media_type, reach=ad.reach,
            real_spend=ad.real_spend, ad_library_url=ad.ad_library_url, landing_url=ad.landing_url,
            category=result["category"], product=result.get("product"),
            classifier=result["classifier"], classifier_raw=result, source_raw=ad.source_raw,
        ))
        classified.append({"raw": ad, "category": result["category"], "product": result.get("product")})
    return classified


def _store_metrics(s, company, week_start, classified, status, source: str = "meta") -> None:
    # A FAILED fetch must never be written as a metric row: doing so records a
    # phantom "0 active ads" that overwrites a good same-week result and quietly
    # corrupts the divergence ranking. The company simply keeps its prior state
    # (shown as no-data / stale), which is the honest outcome of a failed fetch.
    if status == "error":
        return
    metrics = aggregate(classified)

    prev = s.scalar(select(WeeklyCompanyMetric).where(
        WeeklyCompanyMetric.company_id == company.id,
        WeeklyCompanyMetric.source == source,
        WeeklyCompanyMetric.week_start < week_start,
    ).order_by(WeeklyCompanyMetric.week_start.desc()).limit(1))
    cats = metrics["ads_by_category"]
    categories_active = sum(1 for c in ("recruitment", "product_sale",
                                        "brand_awareness", "event_promo") if cats.get(c))
    metrics["score"] = company_score(
        metrics["total_active_ads"],
        prev.total_active_ads if prev else None,
        metrics["new_ads"], categories_active,
    )

    existing = s.scalar(select(WeeklyCompanyMetric).where(
        WeeklyCompanyMetric.company_id == company.id,
        WeeklyCompanyMetric.source == source,
        WeeklyCompanyMetric.week_start == week_start,
    ))
    target = existing or WeeklyCompanyMetric(company_id=company.id, source=source, week_start=week_start)
    for k, v in metrics.items():
        setattr(target, k, v)
    target.status = status
    if existing is None:
        s.add(target)


# ---------------------------------------------------------------------------
# the weekly cycle
# ---------------------------------------------------------------------------

def run_once(progress=None, company_id: int | None = None) -> dict:
    """Collect + resolve + classify + aggregate + store.

    By default runs the full weekly cycle for every company. Pass `company_id`
    to fetch just that one company — the partner-hub sweep still runs (needed
    to catch that company's own partner pages) but is scoped to only match
    this company, so no other company's data is touched.

    `progress`, if given, receives dict events for a live UI:
      {"phase":"begin","total":N,"backend":...}
      {"phase":"company_start"/"company_done", ...}
      {"phase":"sweep_start"} / {"phase":"sweep_done","linked":n,"attributed":n}
      {"phase":"end","summary":{...}}
    """
    def emit(evt):
        if progress:
            try:
                progress(evt)
            except Exception:  # noqa: BLE001 — a UI callback must never break a run
                pass

    init_db()
    seed_companies_if_empty()
    source = MetaAdSource()
    week_start = monday_of(dt.date.today())
    summary = {"week_start": week_start.isoformat(), "backend": source.backend,
               "companies": 0, "collected": 0, "errors": 0, "skipped_no_identity": 0,
               "partner_pages_linked": 0, "partner_ads_attributed": 0}

    with SessionLocal() as s:
        query = select(Company)
        if company_id is not None:
            query = query.where(Company.id == company_id)
        companies = list(s.scalars(query))
        if company_id is not None and not companies:
            summary["errors"] += 1
            emit({"phase": "end", "summary": summary, "error": "Company not found"})
            return summary
        summary["companies"] = len(companies)
        total = len(companies)
        emit({"phase": "begin", "total": total, "backend": source.backend})

        llm_cache = _llm_result_cache(s)
        week_ads: dict[int, list[dict]] = {}       # company_id -> classified ads
        week_status: dict[int, str] = {}           # company_id -> overall status
        seen_ad_ids: dict[int, set[str]] = {}      # company_id -> external ids stored this cycle

        for idx, company in enumerate(companies, start=1):
            emit({"phase": "company_start", "i": idx, "total": total, "company": company.name})
            collected: list[dict] = []
            status = "ok"
            run = None   # the CollectionRun in flight — reused (not duplicated) on error
            try:
                # Meta pages only — a company can also own a Google advertiser
                # (source="google") row; fetching that AR-id through the Meta
                # source would waste quota on a guaranteed miss.
                pages = list(s.scalars(select(CompanyPage).where(
                    CompanyPage.company_id == company.id, CompanyPage.active,
                    CompanyPage.source == "meta")))
                if pages:
                    for page in pages:
                        if partner_linker.is_excluded_page(page.page_name):
                            continue  # excluded advertiser page — don't fetch/count its ads
                        run = CollectionRun(company_id=company.id, source="meta",
                                            week_start=week_start, page_id=page.page_id,
                                            page_name=page.page_name, page_role=page.role)
                        s.add(run)
                        s.flush()
                        raw_ads = source.fetch_ads(page.page_id, country=company.country,
                                                   active_only=True)
                        run.status = "ok" if raw_ads else "no_active_ads"
                        run.ads_scraped = len(raw_ads)
                        collected += _store_ads(s, run, raw_ads, llm_cache)
                    status = "ok" if collected else "no_active_ads"
                elif company.resolution_status == "confirmed" and company.page_name:
                    # CONFIRMED identity, handle-only (no numeric id yet): one
                    # search on the EXACT page name the identity check found —
                    # never the legal Firmenname. Success stores the id, so the
                    # next fetch takes the direct-page branch above.
                    run = CollectionRun(company_id=company.id, source="meta",
                                        week_start=week_start)
                    s.add(run)
                    s.flush()
                    result = resolver.resolve_confirmed_handle(source, s, company)
                    raw_ads = []
                    if result["status"] == "confirmed" and result.get("page_id"):
                        # authoritative count: direct page fetch, not the search subset
                        raw_ads = source.fetch_ads(result["page_id"], country=company.country,
                                                   active_only=True)
                        run.status = "ok" if raw_ads else "no_active_ads"
                    else:
                        run.status = "no_ads_found"
                    run.page_id = result.get("page_id")
                    run.page_name = result.get("page_name")
                    run.page_role = "main" if result.get("page_id") else None
                    run.ads_scraped = len(raw_ads)
                    collected += _store_ads(s, run, raw_ads, llm_cache)
                    status = run.status
                else:
                    # NO confirmed Meta page (pending / ambiguous / no page found)
                    # -> SKIPPED by design. The Ad lookup never name-searches an
                    # unconfirmed company — that's the Identity check's job. This
                    # both protects the Apify quota and makes a wrong-page fetch
                    # impossible.
                    summary["skipped_no_identity"] = summary.get("skipped_no_identity", 0) + 1
                    emit({"phase": "company_done", "i": idx, "total": total,
                          "company": company.name, "status": "skipped_no_identity",
                          "ads": 0})
                    continue
            except ApifyQuotaError as exc:
                # Batch-fatal: every remaining company would hit the same 403.
                # Stop now, flag it, and let the UI show one clear banner instead
                # of an error per company.
                summary["errors"] += 1
                summary["quota_exceeded"] = True
                if run is not None:
                    run.status = "error"
                    run.error = "Apify usage/hard limit reached"
                    run.ads_scraped = 0
                s.commit()
                emit({"phase": "quota_exceeded", "i": idx, "total": total,
                      "company": company.name, "detail": str(exc)[:200]})
                break
            except Exception as exc:  # noqa: BLE001
                status = "error"
                summary["errors"] += 1
                # Reuse the run already created before the fetch (avoids a
                # phantom 'ok' row sitting next to the 'error' row); only add a
                # fresh row if the failure happened before any run was created.
                if run is not None:
                    run.status = "error"
                    run.error = str(exc)[:1000]
                    run.ads_scraped = 0
                else:
                    s.add(CollectionRun(company_id=company.id, source="meta",
                                        week_start=week_start, status="error",
                                        error=str(exc)[:1000]))
                s.commit()
                emit({"phase": "company_done", "i": idx, "total": total,
                      "company": company.name, "status": "error", "ads": 0,
                      "error": str(exc)[:200]})
                week_status[company.id] = status
                continue

            week_ads[company.id] = collected
            week_status[company.id] = status
            seen_ad_ids[company.id] = {c["raw"].external_ad_id for c in collected
                                       if c["raw"].external_ad_id}
            summary["collected"] += 1
            s.commit()
            emit({"phase": "company_done", "i": idx, "total": total,
                  "company": company.name, "status": status,
                  "ads": len(collected), "page_name": company.page_name})

        # ---- partner-hub sweep -------------------------------------------
        # OFF by design: AdWatch targets win-back / prospect dealers, not ones
        # already advertising THROUGH the Solarlux partner program. Gated by
        # config/partner_discovery.yaml -> enabled (default false), so the sweep
        # never runs and never spends the per-search Apify quota unless turned on
        # deliberately.
        _sweep_on = partner_linker.is_enabled() and not summary.get("quota_exceeded")
        try:
            if not _sweep_on:
                groups = []
            else:
                emit({"phase": "sweep_start"})
                groups = partner_linker.run_sweep(source, s, companies)
            for g in groups:
                cid = g["company_id"]
                fresh = [a for a in g["ads"]
                         if a.is_active and a.external_ad_id not in seen_ad_ids.setdefault(cid, set())]
                if not fresh and g["role"] != "partner":
                    continue
                run = CollectionRun(company_id=cid, source="meta", week_start=week_start,
                                    page_id=g["page_id"], page_name=g["page_name"],
                                    page_role=g["role"],
                                    status="ok" if fresh else "no_active_ads")
                s.add(run)
                s.flush()
                run.ads_scraped = len(fresh)
                classified = _store_ads(s, run, fresh, llm_cache)
                week_ads.setdefault(cid, []).extend(classified)
                seen_ad_ids[cid] |= {a.external_ad_id for a in fresh if a.external_ad_id}
                if g["role"] == "partner":
                    summary["partner_pages_linked"] += 1
                summary["partner_ads_attributed"] += len(fresh)
                if week_status.get(cid) in (None, "no_active_ads", "no_ads_found") and fresh:
                    week_status[cid] = "ok"
            s.commit()
            if _sweep_on:
                emit({"phase": "sweep_done", "linked": summary["partner_pages_linked"],
                      "attributed": summary["partner_ads_attributed"]})
        except Exception as exc:  # noqa: BLE001 — sweep failure must not lose the cycle
            summary["errors"] += 1
            emit({"phase": "sweep_done", "linked": 0, "attributed": 0,
                  "error": str(exc)[:200]})

        # ---- weekly metrics (per company, across ALL its pages) ------------
        for company in companies:
            if company.id in week_ads or week_status.get(company.id):
                _store_metrics(s, company, week_start,
                               week_ads.get(company.id, []),
                               week_status.get(company.id, "ok"))
        s.commit()

    emit({"phase": "end", "summary": summary})
    return summary


def run_once_google(progress=None, company_id: int | None = None) -> dict:
    """Google's equivalent of run_once(), simplified: no partner-hub sweep (no
    landing_url data to sweep with — see collect/google_source.py) and no
    'ambiguous' identity state (domain resolution is exact, not fuzzy).

    A company with no website_domain set is skipped with status='no_domain'
    rather than erroring, since not every tracked company needs to opt into
    Google tracking."""
    from ..identity.resolver import resolve_and_record_google
    from .google_source import GoogleAdSource

    def emit(evt):
        if progress:
            try:
                progress(evt)
            except Exception:  # noqa: BLE001
                pass

    init_db()
    source = GoogleAdSource()
    week_start = monday_of(dt.date.today())
    summary = {"week_start": week_start.isoformat(), "backend": source.backend,
               "companies": 0, "collected": 0, "errors": 0, "skipped_no_domain": 0}

    with SessionLocal() as s:
        query = select(Company)
        if company_id is not None:
            query = query.where(Company.id == company_id)
        companies = list(s.scalars(query))
        if company_id is not None and not companies:
            summary["errors"] += 1
            emit({"phase": "end", "summary": summary, "error": "Company not found"})
            return summary
        summary["companies"] = len(companies)
        total = len(companies)
        emit({"phase": "begin", "total": total, "backend": source.backend, "source": "google"})

        llm_cache = _llm_result_cache(s)

        for idx, company in enumerate(companies, start=1):
            emit({"phase": "company_start", "i": idx, "total": total, "company": company.name})
            if not company.website_domain:
                summary["skipped_no_domain"] += 1
                emit({"phase": "company_done", "i": idx, "total": total, "company": company.name,
                     "status": "no_domain", "ads": 0})
                continue

            try:
                page = s.scalar(select(CompanyPage).where(
                    CompanyPage.company_id == company.id, CompanyPage.source == "google",
                    CompanyPage.active))
                run = CollectionRun(company_id=company.id, source="google", week_start=week_start)
                s.add(run)
                s.flush()
                if page:
                    raw_ads = source.fetch_ads(page.page_id, country=company.country, active_only=True)
                    run.page_id, run.page_name, run.page_role = page.page_id, page.page_name, page.role
                    status = "ok" if raw_ads else "no_active_ads"
                else:
                    result = resolve_and_record_google(source, s, company)
                    if result["status"] == "confirmed":
                        raw_ads = source.fetch_ads(result["page_id"], country=company.country,
                                                   active_only=True)
                        run.page_id, run.page_name, run.page_role = (
                            result["page_id"], result["page_name"], "main")
                        status = "ok" if raw_ads else "no_active_ads"
                    else:
                        raw_ads = []
                        status = result["status"]
                run.status = status
                run.ads_scraped = len(raw_ads)
                classified = _store_ads(s, run, raw_ads, llm_cache, source="google")
                _store_metrics(s, company, week_start, classified, status, source="google")
                summary["collected"] += 1
                s.commit()
                emit({"phase": "company_done", "i": idx, "total": total, "company": company.name,
                     "status": status, "ads": len(classified)})
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                s.add(CollectionRun(company_id=company.id, source="google", week_start=week_start,
                                    status="error", error=str(exc)[:1000]))
                s.commit()
                emit({"phase": "company_done", "i": idx, "total": total, "company": company.name,
                     "status": "error", "ads": 0, "error": str(exc)[:200]})

    emit({"phase": "end", "summary": summary})
    return summary
