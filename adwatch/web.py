"""AdWatch dashboard — FastAPI backend serving a vanilla HTML/CSS/JS frontend.

Run:  python run.py serve   (or: uvicorn adwatch.web:app)

No build step, no Node.js — the frontend is a static page (templates/index.html
+ static/app.css + static/app.js) driven entirely by this JSON/SSE API. Every
route is a thin wrapper around the already-tested backend packages:
  identity/  — page linking            collect/  — fetch pipeline
  insights/  — classify/score/flags    services  — read-model for the UI
"""
from __future__ import annotations

import json
import queue
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import appsettings, config, customers, jobs, services
from .collect.meta_source import search_term
from .collect.pipeline import run_once, run_once_google, seed_companies_if_empty
from .db import init_db
from .identity import resolver
from .insights.flags import compute_flags

app = FastAPI(title="AdWatch")


@app.middleware("http")
async def _require_auth(request, call_next):
    """HTTP Basic auth gate — active only when config.ACCESS_PASSWORD is set.
    Unset (the default) = no auth, which is only safe because the server binds
    127.0.0.1 by default (see cli.py). Set the password before hosting."""
    import base64
    import secrets as _secrets
    from starlette.responses import Response as _Resp
    if config.ACCESS_PASSWORD:
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = (_secrets.compare_digest(user, config.ACCESS_USER)
                      and _secrets.compare_digest(pw, config.ACCESS_PASSWORD))
            except Exception:
                ok = False
        if not ok:
            return _Resp(status_code=401, headers={"WWW-Authenticate": 'Basic realm="AdWatch"'})
    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(config.ROOT / "static")), name="static")

# Single-user local tool: one fetch at a time (manual OR a scoped job — see
# jobs.py's shared try_acquire/release), tracked via jobs.is_busy().
_runs: dict[str, queue.Queue] = {}


class CompanyIn(BaseModel):
    name: str
    website_domain: str | None = None   # used to resolve the Google Ads advertiser


class PageIn(BaseModel):
    page_id: str
    role: str = "main"


class SearchIn(BaseModel):
    term: str


class FetchIn(BaseModel):
    company_id: int | None = None
    sources: list[str] | None = None   # meta | google, any combo — None defaults to ["meta"]


class EmailReportIn(BaseModel):
    report: str = "top5"                    # top5 | full
    recipient_ids: list[int] | None = None  # selected saved recipients (see ReportRecipient)
    recipient: str | None = None            # ad-hoc single address, combined with recipient_ids
    subject: str | None = None              # defaults to 'Bericht-KW-<n>' from the filename
    filters: dict | None = None             # optional Companies Explorer filter to scope the report to


class SendExistingIn(BaseModel):
    recipient_ids: list[int] | None = None
    recipient: str | None = None
    subject: str | None = None              # defaults to 'Bericht-KW-<n>' from the filename


class RecipientIn(BaseModel):
    email: str
    name: str | None = None


class ScheduleIn(BaseModel):
    fetch_enabled: bool | None = None
    fetch_day: int | None = None      # 0=Mon .. 6=Sun
    fetch_time: str | None = None     # 'HH:MM'
    fetch_sources: list[str] | None = None  # meta | google, any combo
    send_enabled: bool | None = None
    send_day: int | None = None
    send_time: str | None = None
    send_report: str | None = None    # top5 | full


class ReportDefIn(BaseModel):
    name: str | None = None
    report_type: str | None = None          # full | top5
    filters: dict | None = None             # a Companies-Explorer filter blob
    recipient_ids: list[int] | None = None
    schedule_enabled: bool | None = None
    schedule_day: int | None = None         # 0=Mon .. 6=Sun
    schedule_time: str | None = None        # 'HH:MM'


class RunReportDefIn(BaseModel):
    send: bool = True


class ConfirmIn(BaseModel):
    page_id: str
    page_name: str | None = None
    category: str | None = None


class SelectTopIn(BaseModel):
    filters: dict = {}
    sort: str | None = None
    direction: str = "asc"
    n: int = 30


class CustomerExportIn(BaseModel):
    filters: dict = {}
    ids: list[int] | None = None   # explicit selection; falls back to the filtered set
    sort: str | None = None
    direction: str = "asc"


class JobEstimateIn(BaseModel):
    company_ids: list[int]
    sources: list[str] = ["meta"]


class JobCreateIn(BaseModel):
    company_ids: list[int]
    sources: list[str] = ["meta"]
    label: str | None = None


class IdentityJobIn(BaseModel):
    company_ids: list[int]
    label: str | None = None


class LockIdentityIn(BaseModel):
    page_id: str
    page_name: str | None = None


class SettingsIn(BaseModel):
    settings: dict


class TestConnIn(BaseModel):
    which: str
    value: str | None = None    # test a just-typed (unsaved) key, if provided


class RevealIn(BaseModel):
    key: str


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    html = (config.ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    # Cache-bust the static assets so a browser holding an old app.js/app.css
    # in cache always picks up the latest version after a normal reload.
    for name in ("app.js", "app.css"):
        v = int((config.ROOT / "static" / name).stat().st_mtime)
        html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return html


# ---------------------------------------------------------------------------
# State + mode
# ---------------------------------------------------------------------------

@app.get("/api/state")
def state():
    metrics = services.latest_metrics()
    companies = services.list_companies()
    return {
        "backend": config.LIVE_SOURCE,
        "country": config.DEFAULT_COUNTRY,
        "classifier": "llm" if config.ANTHROPIC_API_KEY else "keywords",
        "apify_configured": bool(config.APIFY_API_TOKEN),
        "email_configured": bool(config.POWER_AUTOMATE_WEBHOOK_URL),
        "email_default_recipient": config.REPORT_EMAIL_DEFAULT_RECIPIENT,
        "fetch_running": jobs.is_busy(),
        "companies": companies,
        "metrics": metrics,
        "flags": compute_flags(metrics),
    }


@app.get("/api/settings")
def get_settings_route():
    return appsettings.get_settings()


@app.put("/api/settings")
def save_settings_route(payload: SettingsIn):
    return appsettings.save_settings(payload.settings)


@app.post("/api/settings/test")
def test_connection_route(payload: TestConnIn):
    return appsettings.test_connection(payload.which, payload.value)


@app.post("/api/settings/reveal")
def reveal_setting_route(payload: RevealIn):
    return appsettings.reveal(payload.key)


@app.get("/api/divergence")
def divergence():
    """Ranked divergence list — Marketing-Aktivität × Umsatz-Lücke per fetched
    company (see insights/divergence.py). The dashboard's 'Interessante
    Partner' section."""
    from .insights.divergence import compute_divergence
    return compute_divergence()


# ---------------------------------------------------------------------------
# Fetch (Part 2) — background thread + Server-Sent Events progress
# ---------------------------------------------------------------------------

_SOURCE_RUNNERS = {"meta": run_once, "google": run_once_google}


@app.post("/api/fetch")
def start_fetch(payload: FetchIn = FetchIn()):
    sources = payload.sources or ["meta"]
    unknown = [s for s in sources if s not in _SOURCE_RUNNERS]
    if unknown:
        raise HTTPException(400, f"Unknown source(s): {', '.join(unknown)}")
    if not config.APIFY_API_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN is not set in .env")
    if "google" in sources and not config.GOOGLE_ADS_ACTOR_ID:
        raise HTTPException(400, "Google Ads fetching needs GOOGLE_ADS_ACTOR_ID in .env")
    if payload.company_id is not None:
        companies = services.list_companies()
        if not any(c["id"] == payload.company_id for c in companies):
            raise HTTPException(404, "Company not found")
    if not jobs.try_acquire("manual"):
        raise HTTPException(409, "A fetch (or scoped job) is already running.")

    run_id = uuid.uuid4().hex
    q: queue.Queue = queue.Queue()
    _runs[run_id] = q
    company_id = payload.company_id

    def worker():
        summaries = {}
        try:
            for src in sources:
                q.put({"phase": "source_start", "source": src})
                summaries[src] = _SOURCE_RUNNERS[src](
                    progress=lambda evt, src=src: q.put({**evt, "source": src}),
                    company_id=company_id)
                q.put({"phase": "source_done", "source": src})
            q.put({"phase": "result", "summary": summaries})
        except Exception as exc:  # noqa: BLE001
            q.put({"phase": "result", "error": str(exc), "summary": summaries})
        finally:
            jobs.release("manual")
            q.put(None)  # sentinel: stream end

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/fetch/stream/{run_id}")
def fetch_stream(run_id: str):
    q = _runs.get(run_id)
    if q is None:
        raise HTTPException(404, "Unknown run_id")

    def gen():
        try:
            while True:
                evt = q.get()
                if evt is None:
                    yield "event: done\ndata: {}\n\n"
                    return
                yield f"data: {json.dumps(evt)}\n\n"
        finally:
            _runs.pop(run_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Companies (Part 1: identity) — CRUD
# ---------------------------------------------------------------------------

@app.post("/api/companies")
def create_company(payload: CompanyIn):
    try:
        return services.add_company(payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/companies/{cid}")
def rename_company(cid: int, payload: CompanyIn):
    try:
        services.update_company(cid, payload.name)
        if payload.website_domain is not None:
            services.update_company_domain(cid, payload.website_domain)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/companies/{cid}")
def remove_company(cid: int):
    services.delete_company(cid)
    return {"ok": True}


@app.get("/api/companies/{cid}")
def get_company_route(cid: int):
    """Full master-data row incl. candidates + fit_breakdown (drawer only)."""
    d = customers.get_company(cid)
    if not d:
        raise HTTPException(404, "Company not found")
    return d


@app.get("/api/companies/{cid}/detail")
def company_detail(cid: int):
    metrics = services.latest_metrics([cid])   # scoped — not the whole base
    m = next((x for x in metrics if x["company_id"] == cid), None)
    if m is None:
        raise HTTPException(404, "Company not found")
    return {
        "company": customers.get_company(cid),   # full master data — drawer works from any tab
        "metric": m,
        "history": services.company_history(cid),
        "week": services.latest_week_detail(cid),
    }


@app.get("/api/logs")
def list_logs_route(status: str | None = None, source: str | None = None, q: str | None = None,
                    page: int = 1, page_size: int = 50):
    return services.list_logs(status, source, q, page, page_size)


@app.post("/api/logs/clear")
def clear_logs_route():
    """Clear the fetch log — removes every run WITHOUT stored ads; runs with
    ads are kept (they anchor the collected ad copies)."""
    return services.clear_logs()


# ---------------------------------------------------------------------------
# Pages (Part 1: identity) — link / unlink / search / confirm
# ---------------------------------------------------------------------------

@app.post("/api/companies/{cid}/pages")
def link_page(cid: int, payload: PageIn):
    try:
        resolver.add_page(cid, payload.page_id, role=payload.role, status="manual",
                          evidence={"method": "manual"})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/pages/{page_row_id}")
def unlink_page(page_row_id: int):
    resolver.unlink_page(page_row_id)
    return {"ok": True}


@app.post("/api/companies/{cid}/search")
def search_pages(cid: int, payload: SearchIn):
    companies = services.list_companies()
    c = next((x for x in companies if x["id"] == cid), None)
    website_domain = c["website_domain"] if c else None
    try:
        return resolver.find_candidates(payload.term, website_domain=website_domain)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Search failed: {exc}")


@app.get("/api/companies/{cid}/search-term")
def default_search_term(cid: int):
    companies = services.list_companies()
    c = next((x for x in companies if x["id"] == cid), None)
    if not c:
        raise HTTPException(404, "Company not found")
    return {"term": search_term(c["name"])}


@app.post("/api/companies/{cid}/confirm")
def confirm_page(cid: int, payload: ConfirmIn):
    try:
        resolver.set_main_page(cid, payload.page_id, payload.page_name, payload.category)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Reports — generate, browse history, download or email any past report
# ---------------------------------------------------------------------------

def _safe_report_path(filename: str):
    """Resolve a report filename to a path strictly inside OUTPUT_DIR — never
    trust a path parameter directly (directory traversal)."""
    from .report import _REPORT_FILENAME_RE  # whitelist: only our own naming scheme
    if not _REPORT_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid report filename")
    path = (config.OUTPUT_DIR / filename).resolve()
    if path.parent != config.OUTPUT_DIR.resolve() or not path.exists():
        raise HTTPException(404, "Report not found")
    return path


def _resolve_recipients(recipient_ids: list[int] | None, recipient: str | None) -> list[str]:
    emails: list[str] = []
    if recipient_ids:
        by_id = {r["id"]: r["email"] for r in services.list_recipients()}
        emails += [by_id[i] for i in recipient_ids if i in by_id]
    if recipient:
        emails.append(recipient)
    if not emails and config.REPORT_EMAIL_DEFAULT_RECIPIENT:
        emails.append(config.REPORT_EMAIL_DEFAULT_RECIPIENT)
    return emails


@app.get("/api/reports")
def list_reports():
    from .report import list_reports as _list
    return {"reports": _list(), "recipients": services.list_recipients()}


@app.post("/api/reports/generate")
def generate_report(payload: EmailReportIn = EmailReportIn()):
    from .report import build_report, build_top5_report, write_report_meta
    path = build_report(filters=payload.filters) if payload.report == "full" \
        else build_top5_report(filters=payload.filters)
    write_report_meta(path, filters=payload.filters)   # so the Reports list can show the filter used
    return {"filename": Path(path).name}


# --- Saved report definitions: a named filter + recipients + optional weekly
# --- schedule, so a custom filtered report is one-click (or automatic) to send.
@app.get("/api/report-defs")
def list_report_defs():
    from . import report_defs
    return {"definitions": report_defs.list_definitions(),
            "recipients": services.list_recipients()}


@app.post("/api/report-defs")
def create_report_def(payload: ReportDefIn):
    from . import report_defs, scheduler
    try:
        d = report_defs.create_definition(
            name=payload.name, filters=payload.filters or {},
            report_type=payload.report_type or "full",
            recipient_ids=payload.recipient_ids or [],
            schedule_enabled=bool(payload.schedule_enabled),
            schedule_day=payload.schedule_day or 0,
            schedule_time=payload.schedule_time or "07:00")
    except ValueError as e:
        raise HTTPException(400, str(e))
    scheduler.apply_schedule()   # register/refresh this definition's cron if scheduled
    return d


@app.put("/api/report-defs/{def_id}")
def update_report_def(def_id: int, payload: ReportDefIn):
    from . import report_defs, scheduler
    try:
        d = report_defs.update_definition(def_id, **payload.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    scheduler.apply_schedule()
    return d


@app.delete("/api/report-defs/{def_id}")
def delete_report_def(def_id: int):
    from . import report_defs, scheduler
    report_defs.delete_definition(def_id)
    scheduler.apply_schedule()
    return {"ok": True}


@app.post("/api/report-defs/{def_id}/run")
def run_report_def(def_id: int, payload: RunReportDefIn = RunReportDefIn()):
    from . import report_defs
    if payload.send and not config.POWER_AUTOMATE_WEBHOOK_URL:
        raise HTTPException(400, "Email isn't configured (POWER_AUTOMATE_WEBHOOK_URL). "
                                 "Generate without sending, or set it in Settings.")
    try:
        return report_defs.run_definition(def_id, send=payload.send)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/reports/{filename}")
def download_report(filename: str):
    path = _safe_report_path(filename)
    return FileResponse(path, media_type="application/pdf", filename=filename)


@app.post("/api/reports/{filename}/send-email")
def send_existing_report(filename: str, payload: SendExistingIn = SendExistingIn()):
    from .emailer import send_report_email
    from .report import subject_for_filename
    path = _safe_report_path(filename)
    to = _resolve_recipients(payload.recipient_ids, payload.recipient)
    if not to:
        raise HTTPException(400, "No recipient given and no default is configured")
    subject = payload.subject or subject_for_filename(filename)
    try:
        send_report_email(str(path), recipient=to, subject=subject)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "sent_to": to}


# Legacy one-shot endpoint (generate + send immediately) — kept for the
# original "Send report by email" quick-action.
@app.post("/api/report/send-email")
def send_report_email_route(payload: EmailReportIn = EmailReportIn()):
    import os
    from .emailer import send_report_email
    from .report import build_report, build_top5_report, subject_for_filename

    path = build_report() if payload.report == "full" else build_top5_report()
    to = _resolve_recipients(payload.recipient_ids, payload.recipient)
    if not to:
        raise HTTPException(400, "No recipient given and no default is configured")
    subject = payload.subject or subject_for_filename(os.path.basename(path))
    try:
        send_report_email(path, recipient=to, subject=subject)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "sent_to": to}


# ---------------------------------------------------------------------------
# Company Explorer — bulk master-data import / filter / export. A company IS
# a customer (see models.Company docstring) — there's no separate promote
# step; a row becomes "tracked" the first time it's fetched. Per-row identity
# management (linking pages, confirming a match) stays in the /api/companies/*
# routes below, same as before the merge. See adwatch/customers.py.
# ---------------------------------------------------------------------------

@app.post("/api/customers/import")
async def import_customers_route(file: UploadFile = File(...)):
    data = await file.read()
    try:
        return customers.import_excel(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 — surface any parse failure to the UI
        raise HTTPException(400, f"Could not read the file: {e}")


@app.get("/api/customers/filter-options")
def customer_filter_options_route():
    return customers.filter_options()


@app.post("/api/customers/select-top")
def customer_select_top_route(payload: SelectTopIn):
    return {"ids": customers.top_ids(payload.filters, payload.sort, payload.direction, payload.n)}


@app.post("/api/customers/export")
def customer_export_route(payload: CustomerExportIn):
    data = customers.export_xlsx(payload.filters, payload.ids, payload.sort, payload.direction)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="companies_export.xlsx"'},
    )


# --- ICP: build the Ideal-Customer-Profile from a winners filter, preview it,
# --- apply it to score the whole base (fit/opportunity/target). Local compute,
# --- no API cost.
@app.post("/api/icp/preview")
def icp_preview_route(payload: SelectTopIn):
    from .insights import icp
    return icp.build_profile(payload.filters or None)


@app.post("/api/icp/apply")
def icp_apply_route(payload: SelectTopIn):
    from .insights import icp
    try:
        return icp.apply_profile(payload.filters or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/icp/diagnose")
def icp_diagnose_route(payload: SelectTopIn):
    """Is this winners filter worth building a profile from — and why/why not."""
    from .insights import icp
    return icp.diagnose(payload.filters or None)


@app.get("/api/icp/latest")
def icp_latest_route():
    from .insights import icp
    return icp.latest_profile() or {"id": None}


@app.get("/api/customers")
def list_customers_route(
    q: str | None = None,
    kv: list[str] = Query(default=[]), segment: list[str] = Query(default=[]),
    sub_segment: list[str] = Query(default=[]), sales_channel: list[str] = Query(default=[]),
    country: list[str] = Query(default=[]),
    has_website: bool = False, revenue_min: float | None = None, revenue_max: float | None = None,
    revenue_history: str | None = None,
    exclude_kv: list[str] = Query(default=[]), exclude_segment: list[str] = Query(default=[]),
    exclude_sub_segment: list[str] = Query(default=[]),
    resolution_status: list[str] = Query(default=[]),
    tracked: bool | None = None, page_id_state: str | None = None,
    ad_activity: str | None = None, ad_source: str | None = None,
    no_website: bool = False, enrichment_status: list[str] = Query(default=[]),
    customer_state: list[str] = Query(default=[]), fit_min: float | None = None,
    sort: str | None = None, direction: str = "asc", page: int = 1, page_size: int = 50,
):
    filters = {"q": q, "kv": kv, "segment": segment, "sub_segment": sub_segment,
               "sales_channel": sales_channel, "country": country,
               "has_website": has_website, "revenue_min": revenue_min,
               "revenue_max": revenue_max, "revenue_history": revenue_history,
               "exclude_kv": exclude_kv, "exclude_segment": exclude_segment,
               "exclude_sub_segment": exclude_sub_segment,
               "resolution_status": resolution_status, "tracked": tracked,
               "page_id_state": page_id_state, "ad_activity": ad_activity,
               "ad_source": ad_source, "no_website": no_website,
               "enrichment_status": enrichment_status,
               "customer_state": customer_state, "fit_min": fit_min}
    return customers.query_companies(filters, sort, direction, page, page_size)


# ---------------------------------------------------------------------------
# Scoped fetch jobs — resumable background fetches over a chosen set of
# companies (see jobs.py). Separate from the manual "Fetch latest ads" flow
# but shares its busy-lock so they never run concurrently.
# ---------------------------------------------------------------------------

@app.post("/api/fetch-jobs/estimate")
def estimate_job_route(payload: JobEstimateIn):
    try:
        return jobs.estimate(payload.company_ids, payload.sources)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/fetch-jobs")
def create_job_route(payload: JobCreateIn):
    if not config.APIFY_API_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN is not set in .env")
    if "google" in payload.sources and not config.GOOGLE_ADS_ACTOR_ID:
        raise HTTPException(400, "Google Ads fetching needs GOOGLE_ADS_ACTOR_ID in .env")
    try:
        job = jobs.create_job(payload.company_ids, payload.sources, payload.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        return jobs.start_job(job["id"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/fetch-jobs")
def list_jobs_route(kind: str | None = None):
    return jobs.list_jobs(kind=kind)


# --- Enrichment: find the missing website + pull useful facts off the company's
# --- own site (see enrich/). Costs ~$0.001 search + ~$0.003 LLM per company.
@app.post("/api/enrich-jobs")
def create_enrich_job_route(payload: IdentityJobIn):
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set — needed to extract company facts")
    try:
        job = jobs.create_job(payload.company_ids, ["web"], payload.label, kind="enrich")
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        return jobs.start_job(job["id"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))


class PipelineIn(BaseModel):
    company_ids: list[int]
    plan: dict                      # {enrich, identity, ads:[...], report:'full'|'top5', send_to:[ids]}
    label: str | None = None


@app.post("/api/pipeline-jobs")
def create_pipeline_job_route(payload: PipelineIn):
    """Run several steps for one company set in the only order that works
    (enrich -> identity -> ads -> report -> send). See jobs._run_pipeline."""
    plan = payload.plan or {}
    if plan.get("enrich") and not config.ANTHROPIC_API_KEY:
        raise HTTPException(400, "ANTHROPIC_API_KEY ist nicht gesetzt — für die Anreicherung nötig")
    if plan.get("ads") and not config.APIFY_API_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN ist nicht gesetzt — für den Ad lookup nötig")
    if plan.get("send_to") and not config.POWER_AUTOMATE_WEBHOOK_URL:
        raise HTTPException(400, "E-Mail ist nicht konfiguriert (POWER_AUTOMATE_WEBHOOK_URL)")
    try:
        job = jobs.create_pipeline_job(payload.company_ids, plan, payload.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        return jobs.start_pipeline_job(job["id"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/companies/{company_id}/enrichment")
def get_enrichment_route(company_id: int):
    from .enrich import service as enrich_service
    return enrich_service.get_enrichment(company_id) or {"company_id": company_id, "status": "none"}


@app.post("/api/companies/{company_id}/enrich")
def enrich_one_route(company_id: int):
    """Enrich a single company on demand (the drawer's button)."""
    from .enrich import service as enrich_service
    try:
        return enrich_service.enrich_company(company_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/companies/{company_id}/enrichment/accept")
def accept_website_route(company_id: int, payload: PageIn):
    """Human approves a review-queue website candidate (payload.page_id carries
    the domain), then re-enriches from it."""
    from .enrich import service as enrich_service
    try:
        return enrich_service.accept_candidate(company_id, payload.page_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/companies/{company_id}/enrichment/reject")
def reject_website_route(company_id: int):
    from .enrich import service as enrich_service
    enrich_service.reject_candidates(company_id)
    return {"ok": True}


@app.post("/api/identity-jobs")
def create_identity_job_route(payload: IdentityJobIn):
    """Run the Meta identity check (page resolution only — no ad fetch, no sweep)
    for the selected companies. Locked companies are skipped by the runner."""
    if not config.APIFY_API_TOKEN:
        raise HTTPException(400, "APIFY_API_TOKEN is not set in .env")
    try:
        job = jobs.create_job(payload.company_ids, ["meta"], payload.label, kind="identity")
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        return jobs.start_job(job["id"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/companies/{cid}/identity-check")
def recheck_identity_route(cid: int):
    """Re-run the identity check for ONE company, synchronously — the drawer's
    'Recheck identity' button. Uses the full pipeline (website → serper +
    domain query → AI judge → embed page-id). Blocked while a bulk job runs so
    the two never contend on the single SQLite writer."""
    if jobs.is_busy():
        raise HTTPException(409, "A fetch or job is running — wait for it to finish, then recheck.")
    if not config.SERPER_API_KEY:
        raise HTTPException(400, "No Serper API key set — add one under Settings to run identity checks.")
    return resolver.run_identity_check(cid)


@app.post("/api/companies/{cid}/lock")
def lock_identity_route(cid: int, payload: LockIdentityIn):
    try:
        resolver.lock_identity(cid, payload.page_id, payload.page_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/companies/{cid}/unlock")
def unlock_identity_route(cid: int):
    resolver.unlock_identity(cid)
    return {"ok": True}


@app.post("/api/companies/{cid}/unlink")
def unlink_main_route(cid: int):
    """Drop the company's current (wrong) main page but keep its candidates —
    the row returns to review. The drawer's 'Unlink' button."""
    resolver.unlink_main(cid)
    return {"ok": True}


@app.get("/api/fetch-jobs/{job_id}")
def get_job_route(job_id: int):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.post("/api/fetch-jobs/{job_id}/resume")
def resume_job_route(job_id: int):
    try:
        return jobs.resume_job(job_id)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/fetch-jobs/{job_id}/cancel")
def cancel_job_route(job_id: int):
    jobs.cancel_job(job_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Report recipients — managed entirely in-app, independent of Power Automate
# ---------------------------------------------------------------------------

@app.get("/api/recipients")
def get_recipients():
    return services.list_recipients()


@app.post("/api/recipients")
def add_recipient_route(payload: RecipientIn):
    try:
        return services.add_recipient(payload.email, payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/recipients/{rid}")
def delete_recipient_route(rid: int):
    services.delete_recipient(rid)
    return {"ok": True}


@app.get("/api/schedule")
def get_schedule_route():
    from . import scheduler
    return {**services.get_schedule(), "next_run": scheduler.next_run_times()}


@app.put("/api/schedule")
def save_schedule_route(payload: ScheduleIn):
    from . import scheduler
    try:
        cfg = services.save_schedule(**payload.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    scheduler.apply_schedule()
    return {**cfg, "next_run": scheduler.next_run_times()}


@app.on_event("startup")
def _startup() -> None:
    init_db()
    seed_companies_if_empty()
    n = jobs.reconcile_on_startup()
    if n:
        import logging
        logging.getLogger("adwatch.jobs").warning("%d fetch job(s) marked 'interrupted' after restart", n)
    from . import scheduler
    scheduler.start()
