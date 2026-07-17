"""In-process weekly scheduler — runs inside the `serve` process (uvicorn),
so it only fires while the dashboard is running. Config lives in the
schedule_config DB row (see models.ScheduleConfig / services.get_schedule),
editable from the dashboard's Settings panel; apply_schedule() re-reads it
and re-registers jobs any time the config is saved.

day_of_week here is 0=Monday..6=Sunday, matching Python's date.weekday()
and APScheduler's own convention — no translation needed either direction.

Concurrency: scheduled fetches acquire the SAME shared busy-lock as manual
fetches and scoped jobs (jobs.try_acquire) so two writers never collide on
SQLite's single writer. If a fetch/job is already running when the cron fires,
the scheduled run is skipped (and recorded) rather than corrupting data.

Outcomes are logged to a rotating file under DATA_DIR/logs and kept in
`last_status` so the dashboard can show when a scheduled run last failed —
in-process scheduling on a workstation is fragile, so failures must be visible.
"""
from __future__ import annotations

import logging
import logging.handlers

from apscheduler.schedulers.background import BackgroundScheduler

from . import config

logger = logging.getLogger("adwatch.scheduler")

_scheduler = BackgroundScheduler(daemon=True)

# last outcome per job id, surfaced via services/state for the UI
last_status: dict[str, dict] = {}


def _record(job: str, ok: bool, detail: str) -> None:
    import datetime as dt
    last_status[job] = {"at": dt.datetime.now().isoformat(timespec="minutes"),
                        "ok": ok, "detail": detail[:300]}


def _setup_logging() -> None:
    """One rotating file handler so scheduled-run outcomes and exceptions are
    not lost to a closed console (previously nothing was configured)."""
    root = logging.getLogger("adwatch")
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    try:
        (config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(
            config.DATA_DIR / "logs" / "adwatch.log", maxBytes=2_000_000, backupCount=5,
            encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    except Exception:
        pass


def _job_fetch() -> None:
    from . import jobs, services
    from .collect.pipeline import run_once, run_once_google
    # Do NOT run concurrently with a manual fetch or a scoped job — share the lock.
    if not jobs.try_acquire("scheduled"):
        logger.warning("Scheduled fetch skipped: another fetch/job is running")
        _record("fetch", False, "skipped — another fetch was already running")
        return
    try:
        runners = {"meta": run_once, "google": run_once_google}
        cfg = services.get_schedule()
        summaries = []
        for src in cfg["fetch_sources"]:
            try:
                summary = runners[src]()
                summaries.append(f"{src}: {summary.get('collected', 0)} collected, "
                                 f"{summary.get('errors', 0)} errors")
                logger.info("Scheduled %s fetch complete: %s", src, summary)
            except Exception:
                logger.exception("Scheduled %s fetch failed", src)
                summaries.append(f"{src}: FAILED")
        ok = all("FAILED" not in s for s in summaries)
        _record("fetch", ok, "; ".join(summaries))
    finally:
        jobs.release("scheduled")


def _job_send() -> None:
    from . import services
    from .emailer import send_weekly_report
    try:
        cfg = services.get_schedule()
        result = send_weekly_report(full=(cfg["send_report"] == "full"))
        logger.info("Scheduled send: %s", result)
        _record("send", bool(result.get("sent")),
                "sent" if result.get("sent") else f"not sent — {result.get('reason', '?')}")
    except Exception as exc:
        logger.exception("Scheduled send failed")
        _record("send", False, f"failed: {exc}")


def _job_backup() -> None:
    from .backup import backup_now
    path = backup_now(tag="nightly")
    _record("backup", path is not None, path or "backup skipped/failed")


def apply_schedule() -> dict:
    """Re-read schedule_config and (re)register jobs. Safe to call repeatedly —
    existing jobs are replaced, not duplicated. The nightly backup is always
    scheduled regardless of the fetch/send toggles."""
    from . import services
    cfg = services.get_schedule()

    _scheduler.remove_all_jobs()
    if cfg["fetch_enabled"]:
        hour, minute = cfg["fetch_time"].split(":")
        _scheduler.add_job(_job_fetch, "cron", id="fetch", day_of_week=cfg["fetch_day"],
                           hour=int(hour), minute=int(minute))
    if cfg["send_enabled"]:
        hour, minute = cfg["send_time"].split(":")
        _scheduler.add_job(_job_send, "cron", id="send", day_of_week=cfg["send_day"],
                           hour=int(hour), minute=int(minute))
    # nightly DB backup at 03:30, always on
    _scheduler.add_job(_job_backup, "cron", id="backup", hour=3, minute=30)
    return cfg


def start() -> None:
    _setup_logging()
    if not _scheduler.running:
        _scheduler.start()
    apply_schedule()


def next_run_times() -> dict:
    out = {}
    for job in _scheduler.get_jobs():
        out[job.id] = job.next_run_time.isoformat() if job.next_run_time else None
    return out
