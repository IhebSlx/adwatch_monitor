"""In-process weekly scheduler — runs inside the `serve` process (uvicorn),
so it only fires while the dashboard is running. Config lives in the
schedule_config DB row (see models.ScheduleConfig / services.get_schedule),
editable from the dashboard's Settings panel; apply_schedule() re-reads it
and re-registers jobs any time the config is saved.

day_of_week here is 0=Monday..6=Sunday, matching Python's date.weekday()
and APScheduler's own convention — no translation needed either direction."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger("adwatch.scheduler")

_scheduler = BackgroundScheduler(daemon=True)


def _job_fetch() -> None:
    from .collect.pipeline import run_once
    try:
        summary = run_once()
        logger.info("Scheduled fetch complete: %s", summary)
    except Exception:
        logger.exception("Scheduled fetch failed")


def _job_send() -> None:
    from . import services
    from .emailer import send_weekly_report
    try:
        cfg = services.get_schedule()
        result = send_weekly_report(full=(cfg["send_report"] == "full"))
        logger.info("Scheduled send: %s", result)
    except Exception:
        logger.exception("Scheduled send failed")


def apply_schedule() -> dict:
    """Re-read schedule_config and (re)register the two jobs. Safe to call
    repeatedly — existing jobs are replaced, not duplicated."""
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
    return cfg


def start() -> None:
    if not _scheduler.running:
        _scheduler.start()
    apply_schedule()


def next_run_times() -> dict:
    out = {}
    for job in _scheduler.get_jobs():
        out[job.id] = job.next_run_time.isoformat() if job.next_run_time else None
    return out
