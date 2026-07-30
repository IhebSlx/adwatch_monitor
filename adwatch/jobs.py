"""Scoped, resumable background fetch jobs — the Phase-2 bridge between the
Customer Explorer's selection and the ad-tracking pipeline.

Deliberately SEQUENTIAL, not concurrent: SQLite allows only one writer at a
time, and this is a local single-user tool, so concurrent Apify calls would
just contend on the DB lock for no real speed-up. Each (company, source) unit
reuses the exact same, already-tested per-company entrypoints as the manual
"Fetch" button (collect.pipeline.run_once / run_once_google with a single
company_id) — no fetch logic is duplicated here.

Resumability: progress (the `completed` cursor + a capped log) is committed
to the FetchJob row after EVERY unit, not just at the end. If the app process
dies mid-job, `reconcile_on_startup()` marks it 'interrupted' instead of
silently losing it — a human then clicks Resume, which continues from the
cursor rather than re-doing (and re-billing) already-completed companies.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading

from sqlalchemy import select

from . import config
from .db import SessionLocal
from .models import Company, FetchJob, WeeklyCompanyMetric

logger = logging.getLogger("adwatch.jobs")

_LOG_CAP = 200            # keep the job row small — cap retained log lines
_cancel_flags: dict[int, bool] = {}   # job_id -> True once a cancel is requested

# One shared lock for ANYTHING that fetches — the manual "Fetch latest ads"
# button (web.py) and scoped jobs (below) both go through this, so they can
# never run concurrently and contend on SQLite's single-writer lock.
_busy_lock = threading.Lock()
_busy_owner: str | None = None   # None | "manual" | "job:<id>"


def try_acquire(owner: str) -> bool:
    global _busy_owner
    with _busy_lock:
        if _busy_owner is not None:
            return False
        _busy_owner = owner
        return True


def release(owner: str) -> None:
    global _busy_owner
    with _busy_lock:
        if _busy_owner == owner:
            _busy_owner = None


def is_busy() -> bool:
    return _busy_owner is not None


def busy_owner() -> str | None:
    return _busy_owner

# Rough constants for the pre-flight estimate — labelled as estimates in the
# UI, not promises. Apify pricing is real and confirmed (checked live against
# the actor pricing API); duration is a typical-case approximation since
# actual run time depends on how many ads a page/advertiser has.
_PRICE_PER_AD_USD = {"meta": 0.00075, "google": 0.0019}
_SECONDS_PER_COMPANY = {"meta": 20, "google": 110}
_DEFAULT_EXPECTED_ADS = {"meta": 5, "google": 3}   # fallback when a company has no fetch history yet


def _historical_avg_ads(source: str) -> float:
    """Average total_active_ads across the most recent metric row per company
    for this source — used to estimate cost/scale for companies never fetched
    before, from whatever pattern already exists in this dataset."""
    with SessionLocal() as s:
        rows = s.scalars(select(WeeklyCompanyMetric).where(WeeklyCompanyMetric.source == source)).all()
    if not rows:
        return _DEFAULT_EXPECTED_ADS.get(source, 5)
    latest_per_company: dict[int, WeeklyCompanyMetric] = {}
    for r in rows:
        cur = latest_per_company.get(r.company_id)
        if cur is None or r.week_start > cur.week_start:
            latest_per_company[r.company_id] = r
    vals = [r.total_active_ads for r in latest_per_company.values()]
    return (sum(vals) / len(vals)) if vals else _DEFAULT_EXPECTED_ADS.get(source, 5)


def _meta_fetchable_ids(s, company_ids: list[int]) -> set[int]:
    """Which of these companies the Meta ad lookup will actually fetch: a
    confirmed Meta identity — either a linked meta CompanyPage (numeric id) or
    a confirmed handle-only row (exact page name known). Everything else is
    skipped by the pipeline, never name-searched. A company with no Meta page
    found therefore never runs a Meta fetch."""
    from .models import CompanyPage
    with_pages = set(s.scalars(select(CompanyPage.company_id).where(
        CompanyPage.company_id.in_(company_ids), CompanyPage.active,
        CompanyPage.source == "meta")))
    handle_only = set(s.scalars(select(Company.id).where(
        Company.id.in_(company_ids),
        Company.resolution_status.in_(["confirmed", "locked"]),
        Company.page_name.is_not(None))))
    return with_pages | handle_only


def _google_fetchable_ids(s, company_ids: list[int]) -> set[int]:
    """Which of these companies the Google ad lookup will actually fetch: any
    with a website domain set. Google resolves the advertiser straight from the
    domain (no name search), so a website is both necessary and sufficient to
    try; a company without one is skipped by the pipeline (status='no_domain')."""
    return set(s.scalars(select(Company.id).where(
        Company.id.in_(company_ids),
        Company.website_domain.is_not(None), Company.website_domain != "")))


def _plan_units(s, company_ids: list[int], sources: list[str]) -> list[tuple[int, str]]:
    """The ordered (company, source) work units for a fetch job, routed per
    company by what is actually fetchable:
      - a Meta unit only where a Meta page has been found,
      - a Google unit only where a website is set,
      - a company that qualifies for NEITHER contributes no units at all
        (it drops out of the job — nothing is fetched for it).
    Order is deterministic — company_ids order, Meta before Google — so a
    resumed job's `completed` cursor still lines up with the same unit list."""
    want_meta, want_google = "meta" in sources, "google" in sources
    meta_ok = _meta_fetchable_ids(s, company_ids) if want_meta else set()
    google_ok = _google_fetchable_ids(s, company_ids) if want_google else set()
    units: list[tuple[int, str]] = []
    for cid in company_ids:
        if want_meta and cid in meta_ok:
            units.append((cid, "meta"))
        if want_google and cid in google_ok:
            units.append((cid, "google"))
    return units


def estimate(company_ids: list[int], sources: list[str]) -> dict:
    """Pre-flight numbers shown before a job is created — count, rough time,
    rough cost range. Uses each company's own last known ad count per source
    when available, else the dataset's average for that source. Only the units
    that will actually run are counted (see `_plan_units`): Meta only where a
    page was found, Google only where a website is set — so the numbers match
    what the job really does, never the raw company × source product."""
    with SessionLocal() as s:
        rows = s.execute(select(WeeklyCompanyMetric.company_id, WeeklyCompanyMetric.source,
                                WeeklyCompanyMetric.total_active_ads,
                                WeeklyCompanyMetric.week_start)
                         .where(WeeklyCompanyMetric.company_id.in_(company_ids))).all()
        meta_fetchable = _meta_fetchable_ids(s, company_ids) if "meta" in sources else set()
        google_fetchable = _google_fetchable_ids(s, company_ids) if "google" in sources else set()
        units = _plan_units(s, company_ids, sources)
    latest: dict[tuple[int, str], tuple[dt.date, int]] = {}
    for cid, src, total, week in rows:
        key = (cid, src)
        if key not in latest or week > latest[key][0]:
            latest[key] = (week, total)

    avg_by_source = {src: _historical_avg_ads(src) for src in sources}
    est_seconds = 0.0
    cost_low = cost_high = 0.0
    for cid, src in units:
        known = latest.get((cid, src))
        expected = known[1] if known else avg_by_source[src]
        price = _PRICE_PER_AD_USD.get(src, 0.001)
        cost_low += expected * 0.6 * price    # ± spread since this is inherently approximate
        cost_high += expected * 1.4 * price
        est_seconds += _SECONDS_PER_COMPANY.get(src, 30)

    return {
        "company_count": len(company_ids),
        "sources": sources,
        "total_units": len(units),
        "meta_fetchable": len(meta_fetchable) if "meta" in sources else None,
        "meta_skipped": (len(company_ids) - len(meta_fetchable)) if "meta" in sources else None,
        "google_fetchable": len(google_fetchable) if "google" in sources else None,
        "google_skipped": (len(company_ids) - len(google_fetchable)) if "google" in sources else None,
        "est_seconds_low": round(est_seconds * 0.7),
        "est_seconds_high": round(est_seconds * 1.3),
        "est_cost_usd_low": round(cost_low, 2),
        "est_cost_usd_high": round(cost_high, 2),
    }


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------

def _job_to_dict(j: FetchJob) -> dict:
    return {
        "id": j.id, "created_at": j.created_at.isoformat(timespec="minutes"),
        "started_at": j.started_at.isoformat(timespec="minutes") if j.started_at else None,
        "finished_at": j.finished_at.isoformat(timespec="minutes") if j.finished_at else None,
        "status": j.status, "kind": getattr(j, "kind", "fetch") or "fetch",
        "sources": j.sources, "company_ids": j.company_ids, "label": j.label,
        "plan": getattr(j, "plan", None),
        "total": j.total, "completed": j.completed, "errors": j.errors,
        "ads_collected": j.ads_collected, "log": j.log,
    }


def create_job(company_ids: list[int], sources: list[str], label: str | None = None,
               kind: str = "fetch") -> dict:
    if not company_ids:
        raise ValueError("No companies selected")
    with SessionLocal() as s:
        if kind == "identity":
            # Identity resolution is Meta-only (Google has no name search) and is
            # one unit of work per company — no per-source fan-out, and no
            # page/website gate (its whole job is to FIND the page).
            sources = ["meta"]
            total = len(company_ids)
        elif kind == "enrich":
            # Enrichment is one unit per company and platform-agnostic: it works
            # on the company's OWN website (finding it first if needed), so no
            # Meta page / existing website is required to queue it.
            sources = ["web"]
            total = len(company_ids)
        else:
            bad = [x for x in sources if x not in ("meta", "google")]
            if bad or not sources:
                raise ValueError("sources must be a non-empty list of 'meta'/'google'")
            # total counts only units that will actually run (Meta needs a page,
            # Google needs a website) — never the raw company × source product.
            total = len(_plan_units(s, company_ids, sources))
            if total == 0:
                raise ValueError("Nothing to fetch — none of the selected companies has a "
                                 "fetchable source (a Meta page for Meta, a website for Google).")
        job = FetchJob(sources=sources, company_ids=company_ids, label=label, kind=kind,
                       total=total, status="queued")
        s.add(job)
        s.commit()
        return _job_to_dict(job)


def get_job(job_id: int) -> dict | None:
    with SessionLocal() as s:
        j = s.get(FetchJob, job_id)
        return _job_to_dict(j) if j else None


def list_jobs(limit: int = 20, kind: str | None = None) -> list[dict]:
    with SessionLocal() as s:
        stmt = select(FetchJob)
        if kind:
            stmt = stmt.where(FetchJob.kind == kind)
        rows = s.scalars(stmt.order_by(FetchJob.created_at.desc()).limit(limit)).all()
        return [_job_to_dict(j) for j in rows]


def _append_log(s, job: FetchJob, text: str) -> None:
    entry = {"ts": dt.datetime.utcnow().isoformat(timespec="seconds"), "text": text}
    job.log = (job.log or []) + [entry]
    if len(job.log) > _LOG_CAP:
        job.log = job.log[-_LOG_CAP:]


def _run(job_id: int) -> None:
    from .collect.pipeline import run_once, run_once_google
    runners = {"meta": run_once, "google": run_once_google}
    try:
        _run_body(job_id, runners)
    finally:
        release(f"job:{job_id}")   # every exit path (done/cancelled/exception) frees the lock exactly once


def _run_body(job_id: int, runners: dict) -> None:
    with SessionLocal() as s:
        job = s.get(FetchJob, job_id)
        if not job:
            return
        job.status = "running"
        job.started_at = job.started_at or dt.datetime.utcnow()
        s.commit()
        company_ids, sources, kind = job.company_ids, job.sources, getattr(job, "kind", "fetch") or "fetch"

    # Identity jobs are one unit per company (Meta only, no ad fetch); fetch
    # jobs fan out per (company, source) but ROUTED — a Meta unit only where a
    # page was found, a Google unit only where a website is set, nothing for a
    # company with neither. Recomputed from the same stored inputs as at create
    # time, so a resumed job lines up with its `completed` cursor.
    if kind == "identity":
        units = [(cid, "meta") for cid in company_ids]
    elif kind == "enrich":
        units = [(cid, "enrich") for cid in company_ids]
    else:
        with SessionLocal() as s:
            units = _plan_units(s, company_ids, sources)

    for idx, (cid, src) in enumerate(units):
        with SessionLocal() as s:
            job = s.get(FetchJob, job_id)
            if job.completed > idx:
                continue  # already done in a prior run (resume)
        if _cancel_flags.get(job_id):
            with SessionLocal() as s:
                job = s.get(FetchJob, job_id)
                job.status = "cancelled"
                job.finished_at = dt.datetime.utcnow()
                _append_log(s, job, "Cancelled by user.")
                s.commit()
            _cancel_flags.pop(job_id, None)
            return

        with SessionLocal() as s:
            company = s.get(Company, cid)
        company_name = company.name if company else f"#{cid}"

        if kind == "identity":
            _run_identity_unit(job_id, idx, cid, company_name)
        elif kind == "enrich":
            _run_enrich_unit(job_id, idx, cid, company_name)
        else:
            _run_fetch_unit(job_id, idx, cid, src, company_name, runners)

    with SessionLocal() as s:
        job = s.get(FetchJob, job_id)
        if job.status not in ("cancelled",):
            job.status = "done"
            job.finished_at = dt.datetime.utcnow()
            _append_log(s, job, "Job complete.")
            s.commit()


def _run_fetch_unit(job_id: int, idx: int, cid: int, src: str, company_name: str, runners: dict) -> None:
    try:
        summary = runners[src](company_id=cid)
        from .collect.pipeline import monday_of
        week_start = monday_of(dt.date.today())
        with SessionLocal() as s:
            m = s.scalar(select(WeeklyCompanyMetric).where(
                WeeklyCompanyMetric.company_id == cid, WeeklyCompanyMetric.source == src,
                WeeklyCompanyMetric.week_start == week_start))
            ads_this_unit = m.total_active_ads if m else 0
            job = s.get(FetchJob, job_id)
            job.completed = idx + 1
            job.ads_collected = (job.ads_collected or 0) + ads_this_unit
            job.errors = (job.errors or 0) + summary.get("errors", 0)
            if summary.get("skipped_no_identity"):
                _append_log(s, job, f"[{src}] ⏭ {company_name} — skipped: no confirmed "
                                    "Meta page (run the Identity check first)")
            else:
                _append_log(s, job, f"[{src}] ✓ {company_name} — {ads_this_unit} ads")
            s.commit()
    except Exception as exc:  # noqa: BLE001 — one company's failure must not kill the job
        with SessionLocal() as s:
            job = s.get(FetchJob, job_id)
            job.completed = idx + 1
            job.errors = (job.errors or 0) + 1
            _append_log(s, job, f"[{src}] ✗ {company_name} — {exc}")
            s.commit()
        logger.exception("Job %s: %s/%s failed", job_id, company_name, src)


# Human-readable one-liners for each identity outcome (see resolver.run_identity_check).
_IDENTITY_LOG = {
    "confirmed": lambda r: f"✓ confirmed → {r.get('page_name') or r.get('page_id')}",
    "locked": lambda r: f"🔒 locked → {r.get('page_name') or r.get('page_id')}",
    "ambiguous": lambda r: f"? ambiguous ({r.get('candidates', 0)} candidates) — needs review",
    "no_ads_found": lambda r: "✗ no page found under this name",
    "skipped_locked": lambda r: f"🔒 skipped (locked → {r.get('page_name') or r.get('page_id')})",
    "error": lambda r: f"✗ error: {r.get('error')}",
}


def _run_identity_unit(job_id: int, idx: int, cid: int, company_name: str) -> None:
    from .identity import resolver
    try:
        result = resolver.run_identity_check(cid)
        line = _IDENTITY_LOG.get(result["status"], lambda r: r["status"])(result)
        if result.get("backend") == "website":
            line += " · via website"   # authoritative: the company's own site
        with SessionLocal() as s:
            job = s.get(FetchJob, job_id)
            job.completed = idx + 1
            if result["status"] in ("error",):
                job.errors = (job.errors or 0) + 1
            _append_log(s, job, f"[identity] {company_name} — {line}")
            s.commit()
    except Exception as exc:  # noqa: BLE001 — one company's failure must not kill the job
        with SessionLocal() as s:
            job = s.get(FetchJob, job_id)
            job.completed = idx + 1
            job.errors = (job.errors or 0) + 1
            _append_log(s, job, f"[identity] ✗ {company_name} — {exc}")
            s.commit()
        logger.exception("Identity job %s: %s failed", job_id, company_name)


# Human-readable one-liners per enrichment outcome (see enrich/service.py).
_ENRICH_LOG = {
    "enriched": lambda r: (f"✓ {r['website']} ({r['website_source']}/{r['validated_by']})"
                           f" · {r['fields_found']} Felder"),
    "needs_review": lambda r: f"? {r['candidates']} Website-Vorschlag/-Vorschläge — bitte prüfen",
    "no_website_found": lambda r: "✗ keine Website gefunden",
    "error": lambda r: f"✗ {r.get('error') or 'Fehler'}",
}


def _run_enrich_unit(job_id: int, idx: int, cid: int, company_name: str) -> None:
    """One company's enrichment (website + facts). A single company failing must
    never kill the job — it's logged and the run moves on."""
    from .enrich import service as enrich_service
    try:
        result = enrich_service.enrich_company(cid)
        line = _ENRICH_LOG.get(result["status"], lambda r: r["status"])(result)
        with SessionLocal() as s:
            job = s.get(FetchJob, job_id)
            job.completed = idx + 1
            if result["status"] == "error":
                job.errors = (job.errors or 0) + 1
            _append_log(s, job, f"[enrich] {company_name} — {line}")
            s.commit()
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as s:
            job = s.get(FetchJob, job_id)
            job.completed = idx + 1
            job.errors = (job.errors or 0) + 1
            _append_log(s, job, f"[enrich] ✗ {company_name} — {exc}")
            s.commit()
        logger.exception("Enrich job %s: %s failed", job_id, company_name)


# ---------------------------------------------------------------------------
# PIPELINE — several steps for one company set, in the only order that works.
#
# The order is not cosmetic. Enrichment fills website_domain; the identity
# check's cheapest and strongest tier is crawling the company's OWN website for
# a social link, and the Google ad lookup needs a domain at all. So:
#
#   1 enrich   (finds the website + facts)      -> makes 2 and 3 far more effective
#   2 identity (finds the Meta page)            -> makes the Meta lookup possible
#   3 ads      (Meta needs a page, Google a domain)
#   4 report   (needs whatever 1-3 produced)
#   5 send     (needs the report)
#
# Everything runs inside ONE FetchJob (kind='pipeline'), so the existing progress
# overlay, cancel and job history work unchanged — and the log says exactly which
# step is doing what.
# ---------------------------------------------------------------------------

PIPELINE_STEPS = ("enrich", "identity", "ads", "report", "send")


def pipeline_units(company_ids: list[int], plan: dict) -> int:
    """Total work units for a plan: one per company per active company-step,
    plus one each for report/send. Recomputed for the ads step at run time,
    because enrichment/identity change how many companies are fetchable."""
    n = len(company_ids)
    total = 0
    if plan.get("enrich"):
        total += n
    if plan.get("identity"):
        total += n
    if plan.get("ads"):
        total += n * len(plan["ads"])      # upper bound; refined when the step starts
    if plan.get("report"):
        total += 1
    if plan.get("send_to"):
        total += 1
    return max(total, 1)


def create_pipeline_job(company_ids: list[int], plan: dict, label: str | None = None) -> dict:
    if not company_ids:
        raise ValueError("No companies selected")
    plan = {k: v for k, v in (plan or {}).items() if v}
    if not any(plan.get(k) for k in PIPELINE_STEPS):
        raise ValueError("Kein Schritt ausgewählt")
    bad = [s for s in (plan.get("ads") or []) if s not in ("meta", "google")]
    if bad:
        raise ValueError("ads must contain only 'meta'/'google'")
    if plan.get("send_to") and not plan.get("report"):
        raise ValueError("Zum Senden muss auch ein Bericht erstellt werden")
    with SessionLocal() as s:
        job = FetchJob(kind="pipeline", sources=list(plan.get("ads") or []),
                       company_ids=company_ids, label=label, plan=plan,
                       total=pipeline_units(company_ids, plan), status="queued")
        s.add(job)
        s.commit()
        return _job_to_dict(job)


def _pl_log(job_id: int, text: str, advance: bool = True) -> None:
    with SessionLocal() as s:
        job = s.get(FetchJob, job_id)
        if not job:
            return
        if advance:
            job.completed = (job.completed or 0) + 1
        _append_log(s, job, text)
        s.commit()


def _pl_cancelled(job_id: int) -> bool:
    return bool(_cancel_flags.get(job_id))


def _run_pipeline(job_id: int) -> None:
    """Execute the plan step by step. A failing company never aborts the run; a
    failing STEP is logged and the pipeline continues with the next one, so a
    broken ad lookup still yields a report from whatever was gathered."""
    from .enrich import service as enrich_service
    from .identity import resolver

    with SessionLocal() as s:
        job = s.get(FetchJob, job_id)
        if not job:
            return
        job.status = "running"
        job.started_at = job.started_at or dt.datetime.utcnow()
        s.commit()
        cids, plan = list(job.company_ids or []), dict(job.plan or {})
        _append_log(s, job, f"Pipeline gestartet — {len(cids)} Firmen, Schritte: "
                            + ", ".join(k for k in PIPELINE_STEPS if plan.get(k)))
        s.commit()

    def _name(cid: int) -> str:
        with SessionLocal() as s:
            c = s.get(Company, cid)
            return c.name if c else f"#{cid}"

    # ---- 1 enrich -----------------------------------------------------------
    if plan.get("enrich") and not _pl_cancelled(job_id):
        _pl_log(job_id, "── Schritt 1/5: Daten anreichern", advance=False)
        for cid in cids:
            if _pl_cancelled(job_id):
                break
            try:
                r = enrich_service.enrich_company(cid)
                line = _ENRICH_LOG.get(r["status"], lambda x: x["status"])(r)
                _pl_log(job_id, f"[enrich] {_name(cid)} — {line}")
            except Exception as exc:  # noqa: BLE001
                _pl_log(job_id, f"[enrich] ✗ {_name(cid)} — {exc}")

    # ---- 2 identity ---------------------------------------------------------
    if plan.get("identity") and not _pl_cancelled(job_id):
        _pl_log(job_id, "── Schritt 2/5: Identitätsprüfung (Meta-Seite)", advance=False)
        for cid in cids:
            if _pl_cancelled(job_id):
                break
            try:
                r = resolver.run_identity_check(cid)
                line = _IDENTITY_LOG.get(r["status"], lambda x: x["status"])(r)
                _pl_log(job_id, f"[identity] {_name(cid)} — {line}")
            except Exception as exc:  # noqa: BLE001
                _pl_log(job_id, f"[identity] ✗ {_name(cid)} — {exc}")

    # ---- 3 ads --------------------------------------------------------------
    if plan.get("ads") and not _pl_cancelled(job_id):
        from .collect.pipeline import run_once, run_once_google
        runners = {"meta": run_once, "google": run_once_google}
        with SessionLocal() as s:
            units = _plan_units(s, cids, list(plan["ads"]))   # now that 1+2 have run
            job = s.get(FetchJob, job_id)
            # replace the upper-bound estimate with the real routed count
            job.total = (job.total or 0) - len(cids) * len(plan["ads"]) + len(units)
            _append_log(s, job, f"── Schritt 3/5: Anzeigen abrufen — {len(units)} von "
                                f"{len(cids) * len(plan['ads'])} möglichen Abrufen "
                                "(nur mit Meta-Seite bzw. Website)")
            s.commit()
        for cid, src in units:
            if _pl_cancelled(job_id):
                break
            try:
                runners[src](company_id=cid)
                _pl_log(job_id, f"[{src}] ✓ {_name(cid)}")
            except Exception as exc:  # noqa: BLE001
                _pl_log(job_id, f"[{src}] ✗ {_name(cid)} — {str(exc)[:120]}")

    # ---- 4 report -----------------------------------------------------------
    filename = None
    if plan.get("report") and not _pl_cancelled(job_id):
        _pl_log(job_id, "── Schritt 4/5: Bericht erstellen", advance=False)
        try:
            from .report import build_report, build_top5_report, write_report_meta
            filters = {"ids": cids}
            path = (build_report(filters=filters) if plan["report"] == "full"
                    else build_top5_report(filters=filters))
            write_report_meta(path, filters=filters, definition_name=None)
            filename = os.path.basename(path)
            _pl_log(job_id, f"[report] ✓ {filename}")
        except Exception as exc:  # noqa: BLE001
            _pl_log(job_id, f"[report] ✗ {exc}")

    # ---- 5 send -------------------------------------------------------------
    if plan.get("send_to") and filename and not _pl_cancelled(job_id):
        _pl_log(job_id, "── Schritt 5/5: Bericht senden", advance=False)
        try:
            from .emailer import send_report_email
            from .report import subject_for_filename
            from .models import ReportRecipient
            with SessionLocal() as s:
                emails = [r.email for r in s.scalars(select(ReportRecipient).where(
                    ReportRecipient.id.in_(list(plan["send_to"])))) if r.active]
            if not emails:
                _pl_log(job_id, "[send] ✗ keine aktiven Empfänger ausgewählt")
            else:
                send_report_email(str(config.OUTPUT_DIR / filename), recipient=emails,
                                  subject=subject_for_filename(filename))
                _pl_log(job_id, f"[send] ✓ an {', '.join(emails)}")
        except Exception as exc:  # noqa: BLE001
            _pl_log(job_id, f"[send] ✗ {exc}")
    elif plan.get("send_to") and not filename:
        _pl_log(job_id, "[send] ✗ übersprungen — kein Bericht vorhanden", advance=False)

    with SessionLocal() as s:
        job = s.get(FetchJob, job_id)
        cancelled = _pl_cancelled(job_id)
        job.status = "cancelled" if cancelled else "done"
        job.finished_at = dt.datetime.utcnow()
        job.completed = max(job.completed or 0, 0)
        _append_log(s, job, "Pipeline abgebrochen." if cancelled else "Pipeline abgeschlossen.")
        s.commit()
    _cancel_flags.pop(job_id, None)


def start_pipeline_job(job_id: int) -> dict:
    if not try_acquire(f"job:{job_id}"):
        raise RuntimeError("Es läuft bereits ein Abruf/Job — bitte abwarten.")

    def _wrapped():
        try:
            _run_pipeline(job_id)
        finally:
            release(f"job:{job_id}")

    threading.Thread(target=_wrapped, daemon=True).start()
    return get_job(job_id)


def start_job(job_id: int) -> dict:
    if not try_acquire(f"job:{job_id}"):
        raise RuntimeError("Another fetch (or job) is already running — wait for it to finish.")
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return get_job(job_id)


def resume_job(job_id: int) -> dict:
    job = get_job(job_id)
    if not job:
        raise ValueError("Job not found")
    if job["status"] not in ("interrupted", "queued"):
        raise ValueError(f"Job is '{job['status']}', not resumable")
    return start_job(job_id)


def cancel_job(job_id: int) -> None:
    """Requests cancellation. The running loop only checks this between
    companies (not mid-Apify-call), so a job can take up to ~2 minutes to
    actually stop after this — set status to 'cancelling' right away so the
    UI can say so instead of looking stuck on 'running'."""
    _cancel_flags[job_id] = True
    with SessionLocal() as s:
        job = s.get(FetchJob, job_id)
        if not job:
            return
        if job.status == "queued":
            # Never actually started (e.g. another fetch held the lock) —
            # nothing running to wait on, so cancel it outright.
            job.status = "cancelled"
            job.finished_at = dt.datetime.utcnow()
            _append_log(s, job, "Cancelled by user before it started.")
            s.commit()
            _cancel_flags.pop(job_id, None)
        elif job.status == "running":
            job.status = "cancelling"
            _append_log(s, job, "Cancel requested — stopping after the in-progress company finishes.")
            s.commit()


def reconcile_on_startup() -> int:
    """Any job still 'running' when the app starts must have died with the
    previous process (no in-memory thread survives a restart) — mark it
    'interrupted' so a human decides whether to resume, rather than it
    silently appearing stuck forever or auto-resuming (and re-spending)
    without anyone choosing to."""
    with SessionLocal() as s:
        # No in-memory thread survives a restart, so ANY non-terminal job is
        # orphaned. A 'cancelling' one had a cancel already requested -> finalize
        # it as cancelled; 'running'/'queued' -> interrupted (a human decides).
        stuck = s.scalars(select(FetchJob).where(
            FetchJob.status.in_(["running", "queued", "cancelling"]))).all()
        for job in stuck:
            if job.status == "cancelling":
                job.status = "cancelled"
                job.finished_at = dt.datetime.utcnow()
                _append_log(s, job, "Cancelled — the app restarted while cancelling.")
            else:
                job.status = "interrupted"
                _append_log(s, job, "Interrupted — the app restarted mid-job.")
        total = len(stuck)
        s.commit()
    return total
