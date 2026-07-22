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
import threading

from sqlalchemy import select

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
    skipped by the pipeline, never name-searched."""
    from .models import CompanyPage
    with_pages = set(s.scalars(select(CompanyPage.company_id).where(
        CompanyPage.company_id.in_(company_ids), CompanyPage.active,
        CompanyPage.source == "meta")))
    handle_only = set(s.scalars(select(Company.id).where(
        Company.id.in_(company_ids),
        Company.resolution_status.in_(["confirmed", "locked"]),
        Company.page_name.is_not(None))))
    return with_pages | handle_only


def estimate(company_ids: list[int], sources: list[str]) -> dict:
    """Pre-flight numbers shown before a job is created — count, rough time,
    rough cost range. Uses each company's own last known ad count per source
    when available, else the dataset's average for that source. For Meta, only
    companies with a confirmed identity count (the rest will be skipped)."""
    with SessionLocal() as s:
        rows = s.execute(select(WeeklyCompanyMetric.company_id, WeeklyCompanyMetric.source,
                                WeeklyCompanyMetric.total_active_ads,
                                WeeklyCompanyMetric.week_start)
                         .where(WeeklyCompanyMetric.company_id.in_(company_ids))).all()
        meta_fetchable = _meta_fetchable_ids(s, company_ids) if "meta" in sources else set()
    latest: dict[tuple[int, str], tuple[dt.date, int]] = {}
    for cid, src, total, week in rows:
        key = (cid, src)
        if key not in latest or week > latest[key][0]:
            latest[key] = (week, total)

    avg_by_source = {src: _historical_avg_ads(src) for src in sources}
    est_seconds = 0.0
    cost_low = cost_high = 0.0
    for cid in company_ids:
        for src in sources:
            if src == "meta" and cid not in meta_fetchable:
                continue   # skipped by the pipeline — no time, no cost
            known = latest.get((cid, src))
            expected = known[1] if known else avg_by_source[src]
            price = _PRICE_PER_AD_USD.get(src, 0.001)
            cost_low += expected * 0.6 * price    # ± spread since this is inherently approximate
            cost_high += expected * 1.4 * price
            est_seconds += _SECONDS_PER_COMPANY.get(src, 30)

    return {
        "company_count": len(company_ids),
        "sources": sources,
        "total_units": len(company_ids) * len(sources),
        "meta_fetchable": len(meta_fetchable) if "meta" in sources else None,
        "meta_skipped": (len(company_ids) - len(meta_fetchable)) if "meta" in sources else None,
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
        "total": j.total, "completed": j.completed, "errors": j.errors,
        "ads_collected": j.ads_collected, "log": j.log,
    }


def create_job(company_ids: list[int], sources: list[str], label: str | None = None,
               kind: str = "fetch") -> dict:
    if not company_ids:
        raise ValueError("No companies selected")
    if kind == "identity":
        # Identity resolution is Meta-only (Google has no name search) and is one
        # unit of work per company — no per-source fan-out.
        sources = ["meta"]
        total = len(company_ids)
    else:
        bad = [s for s in sources if s not in ("meta", "google")]
        if bad or not sources:
            raise ValueError("sources must be a non-empty list of 'meta'/'google'")
        total = len(company_ids) * len(sources)
    with SessionLocal() as s:
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
    # jobs fan out per (company, source).
    units = [(cid, "meta") for cid in company_ids] if kind == "identity" \
        else [(cid, src) for cid in company_ids for src in sources]

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
