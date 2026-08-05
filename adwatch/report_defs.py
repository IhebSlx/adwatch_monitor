"""Saved, re-runnable (and schedulable) reports over a custom Explorer filter.

See models.ReportDefinition. This module only persists the saved scope +
recipients + schedule and ties the pieces together: building the PDF is
delegated to report.build_*, emailing to emailer.send_report_email. The
in-process scheduler (scheduler.py) calls run_definition() for any definition
whose weekly cron is due; the UI calls it for a one-click "run & send now".
"""
from __future__ import annotations

import datetime as dt
import os

from sqlalchemy import select

from .db import SessionLocal
from .models import ReportDefinition, ReportRecipient

_VALID_TYPES = ("full", "top5")


def _to_dict(d: ReportDefinition) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "report_type": d.report_type,
        "filters": d.filters or {},
        "recipient_ids": d.recipient_ids or [],
        "schedule_enabled": bool(d.schedule_enabled),
        "schedule_day": d.schedule_day,
        "schedule_time": d.schedule_time,
        "created_at": d.created_at.isoformat(timespec="minutes") if d.created_at else None,
        "last_run_at": d.last_run_at.isoformat(timespec="minutes") if d.last_run_at else None,
        "last_status": d.last_status,
    }


def _validate(report_type: str, schedule_time: str, schedule_day: int) -> None:
    if report_type not in _VALID_TYPES:
        raise ValueError("report_type must be 'full' or 'top5'")
    try:
        h, m = schedule_time.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError
    except Exception:
        raise ValueError("schedule_time must be 'HH:MM'")
    if not (0 <= int(schedule_day) <= 6):
        raise ValueError("schedule_day must be 0 (Mon) .. 6 (Sun)")


def list_definitions() -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(select(ReportDefinition).order_by(ReportDefinition.name)).all()
        return [_to_dict(d) for d in rows]


def get_definition(def_id: int) -> dict | None:
    with SessionLocal() as s:
        d = s.get(ReportDefinition, def_id)
        return _to_dict(d) if d else None


def create_definition(name: str, filters: dict, report_type: str = "full",
                      recipient_ids: list[int] | None = None,
                      schedule_enabled: bool = False, schedule_day: int = 0,
                      schedule_time: str = "07:00") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("A name is required")
    _validate(report_type, schedule_time, schedule_day)
    with SessionLocal() as s:
        d = ReportDefinition(
            name=name, report_type=report_type, filters=filters or {},
            recipient_ids=[int(r) for r in (recipient_ids or [])],
            schedule_enabled=bool(schedule_enabled),
            schedule_day=int(schedule_day), schedule_time=schedule_time,
        )
        s.add(d)
        s.commit()
        return _to_dict(d)


def update_definition(def_id: int, **fields) -> dict:
    with SessionLocal() as s:
        d = s.get(ReportDefinition, def_id)
        if not d:
            raise ValueError("Report definition not found")
        rt = fields.get("report_type", d.report_type)
        st = fields.get("schedule_time", d.schedule_time)
        sd = fields.get("schedule_day", d.schedule_day)
        _validate(rt, st, sd)
        if "name" in fields:
            name = (fields["name"] or "").strip()
            if not name:
                raise ValueError("A name is required")
            d.name = name
        for k in ("report_type", "filters", "schedule_time"):
            if k in fields and fields[k] is not None:
                setattr(d, k, fields[k])
        if "recipient_ids" in fields and fields["recipient_ids"] is not None:
            d.recipient_ids = [int(r) for r in fields["recipient_ids"]]
        if "schedule_enabled" in fields and fields["schedule_enabled"] is not None:
            d.schedule_enabled = bool(fields["schedule_enabled"])
        if "schedule_day" in fields and fields["schedule_day"] is not None:
            d.schedule_day = int(fields["schedule_day"])
        s.commit()
        return _to_dict(d)


def delete_definition(def_id: int) -> None:
    with SessionLocal() as s:
        d = s.get(ReportDefinition, def_id)
        if d:
            s.delete(d)
            s.commit()


def run_definition(def_id: int, send: bool = True) -> dict:
    """Build this definition's report from current data and (optionally) email
    it to its saved recipients. Records the outcome on the row so the UI/log
    can show when it last ran and whether it went out. Returns
    {filename, sent, recipients?/reason?}."""
    from .report import build_report, build_top5_report, subject_for_filename, write_report_meta

    with SessionLocal() as s:
        d = s.get(ReportDefinition, def_id)
        if not d:
            raise ValueError("Report definition not found")
        name, rtype, filters, rids = d.name, d.report_type, d.filters or {}, d.recipient_ids or []

    path = build_report(filters=filters) if rtype == "full" else build_top5_report(filters=filters)
    write_report_meta(path, filters=filters, definition_name=name)
    result: dict = {"filename": os.path.basename(path), "sent": False}

    if send:
        with SessionLocal() as s:
            recips = s.scalars(select(ReportRecipient).where(
                ReportRecipient.id.in_(rids))).all() if rids else []
            emails = [r.email for r in recips if r.active]
        if emails:
            from .emailer import send_report_email
            subject = f"{name} — {subject_for_filename(result['filename'])}"
            send_report_email(path, recipient=emails, subject=subject,
                              source="definition")
            result.update(sent=True, recipients=emails, subject=subject)
        else:
            result["reason"] = "no active recipients set on this report"

    with SessionLocal() as s:
        d = s.get(ReportDefinition, def_id)
        if d:
            d.last_run_at = dt.datetime.utcnow()
            d.last_status = (f"sent to {len(result.get('recipients', []))}" if result["sent"]
                             else f"generated, not sent ({result.get('reason', 'send off')})")
            s.commit()
    return result
