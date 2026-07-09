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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, services
from .collect.meta_source import search_term
from .collect.pipeline import reseed_from_file, run_once, seed_companies_if_empty
from .db import init_db
from .identity import resolver
from .insights.flags import compute_flags

app = FastAPI(title="AdWatch")
app.mount("/static", StaticFiles(directory=str(config.ROOT / "static")), name="static")

# Single-user local tool: one fetch at a time, tracked via a plain module flag.
_fetch_lock = threading.Lock()
_fetch_running = False
_runs: dict[str, queue.Queue] = {}


class CompanyIn(BaseModel):
    name: str


class ModeIn(BaseModel):
    mode: str


class PageIn(BaseModel):
    page_id: str
    role: str = "main"


class SearchIn(BaseModel):
    term: str


class FetchIn(BaseModel):
    company_id: int | None = None


class EmailReportIn(BaseModel):
    report: str = "top5"          # top5 | full
    recipient: str | None = None  # falls back to REPORT_EMAIL_DEFAULT_RECIPIENT
    subject: str = "AdWatch Weekly Report"


class ConfirmIn(BaseModel):
    page_id: str
    page_name: str | None = None
    category: str | None = None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return (config.ROOT / "templates" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# State + mode
# ---------------------------------------------------------------------------

@app.get("/api/state")
def state():
    metrics = services.latest_metrics()
    companies = services.list_companies()
    return {
        "mode": config.MODE,
        "backend": config.LIVE_SOURCE if config.is_live() else "mock",
        "country": config.DEFAULT_COUNTRY,
        "classifier": "llm" if (config.is_live() and config.ANTHROPIC_API_KEY) else "keywords",
        "apify_configured": bool(config.APIFY_API_TOKEN),
        "email_configured": bool(config.POWER_AUTOMATE_WEBHOOK_URL),
        "email_default_recipient": config.REPORT_EMAIL_DEFAULT_RECIPIENT,
        "fetch_running": _fetch_running,
        "companies": companies,
        "metrics": metrics,
        "flags": compute_flags(metrics),
    }


@app.post("/api/mode")
def set_mode(payload: ModeIn):
    if payload.mode not in ("live", "mock"):
        raise HTTPException(400, "mode must be 'live' or 'mock'")
    if _fetch_running:
        raise HTTPException(409, "A fetch is currently running — wait for it to finish.")
    config.MODE = payload.mode
    init_db()
    seed_companies_if_empty()
    return {"mode": config.MODE}


@app.post("/api/reseed")
def reseed():
    if _fetch_running:
        raise HTTPException(409, "A fetch is currently running — wait for it to finish.")
    n = reseed_from_file()
    return {"reseeded": n}


# ---------------------------------------------------------------------------
# Fetch (Part 2) — background thread + Server-Sent Events progress
# ---------------------------------------------------------------------------

@app.post("/api/fetch")
def start_fetch(payload: FetchIn = FetchIn()):
    global _fetch_running
    if config.MODE == "live" and not config.APIFY_API_TOKEN:
        raise HTTPException(400, "Live mode needs APIFY_API_TOKEN in .env")
    if payload.company_id is not None:
        companies = services.list_companies()
        if not any(c["id"] == payload.company_id for c in companies):
            raise HTTPException(404, "Company not found")
    with _fetch_lock:
        if _fetch_running:
            raise HTTPException(409, "A fetch is already running.")
        _fetch_running = True

    run_id = uuid.uuid4().hex
    q: queue.Queue = queue.Queue()
    _runs[run_id] = q
    mode_at_start = config.MODE  # pin the mode this run started in
    company_id = payload.company_id

    def worker():
        global _fetch_running
        prev_mode = config.MODE
        config.MODE = mode_at_start
        try:
            summary = run_once(progress=lambda evt: q.put(evt), company_id=company_id)
            q.put({"phase": "result", "summary": summary})
        except Exception as exc:  # noqa: BLE001
            q.put({"phase": "result", "error": str(exc)})
        finally:
            config.MODE = prev_mode
            _fetch_running = False
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
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/companies/{cid}")
def remove_company(cid: int):
    services.delete_company(cid)
    return {"ok": True}


@app.get("/api/companies/{cid}/detail")
def company_detail(cid: int):
    metrics = services.latest_metrics()
    m = next((x for x in metrics if x["company_id"] == cid), None)
    if m is None:
        raise HTTPException(404, "Company not found")
    return {
        "metric": m,
        "history": services.company_history(cid),
        "week": services.latest_week_detail(cid),
    }


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
    if config.MODE != "live":
        raise HTTPException(400, "Switch to Live mode to search the real Ad Library.")
    try:
        return resolver.find_candidates(payload.term)
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
# Reports
# ---------------------------------------------------------------------------

@app.get("/api/report/top5")
def report_top5():
    from .report import build_top5_report
    path = build_top5_report()
    return FileResponse(path, media_type="application/pdf", filename=path.split("/")[-1].split("\\")[-1])


@app.get("/api/report/full")
def report_full():
    from .report import build_report
    path = build_report()
    return FileResponse(path, media_type="application/pdf", filename=path.split("/")[-1].split("\\")[-1])


@app.post("/api/report/send-email")
def send_report_email_route(payload: EmailReportIn = EmailReportIn()):
    from .emailer import send_report_email
    from .report import build_report, build_top5_report

    path = build_report() if payload.report == "full" else build_top5_report()
    try:
        send_report_email(path, recipient=payload.recipient, subject=payload.subject)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "sent_to": payload.recipient or config.REPORT_EMAIL_DEFAULT_RECIPIENT}


@app.on_event("startup")
def _startup() -> None:
    init_db()
    seed_companies_if_empty()
