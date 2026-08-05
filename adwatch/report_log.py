"""Audit trail for reports: what was created, what was mailed, when, to whom.

Deliberately tiny and dependency-free so it can be called from anywhere without
import cycles — the emailer, the pipeline, the scheduler and the ad-hoc routes all
record through here, which is the point: there is exactly ONE place a delivery is
recorded, so no send path can quietly skip it.

Recording must never break the thing it observes. Every function swallows its own
errors: failing to write an audit row is not a reason to fail a send that already
went out (or to lose the real error from one that didn't).
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from .db import SessionLocal
from .models import ReportEvent

log = logging.getLogger("adwatch.report_log")


def record(kind: str, filename: str, *, report_type: str | None = None,
           scope: str | None = None, recipients: list[str] | None = None,
           subject: str | None = None, source: str | None = None,
           detail: str | None = None) -> None:
    """Append one immutable event. kind: created | sent | send_failed."""
    try:
        with SessionLocal() as s:
            s.add(ReportEvent(
                kind=kind, filename=(filename or "")[:200], report_type=report_type,
                scope=(scope or None), recipients=list(recipients or []) or None,
                subject=(subject or None), source=source,
                detail=(str(detail)[:600] if detail else None)))
            s.commit()
    except Exception:                                          # noqa: BLE001
        # An audit row is worth less than the operation it describes.
        log.exception("could not record report event %s for %s", kind, filename)


def history(limit: int = 200) -> list[dict]:
    """Newest first, for the Logs tab."""
    with SessionLocal() as s:
        rows = s.scalars(select(ReportEvent)
                         .order_by(ReportEvent.at.desc(), ReportEvent.id.desc())
                         .limit(max(1, min(limit, 1000)))).all()
        return [{
            "id": r.id, "kind": r.kind, "filename": r.filename,
            "report_type": r.report_type, "scope": r.scope,
            "recipients": list(r.recipients or []), "subject": r.subject,
            "source": r.source, "detail": r.detail,
            "at": r.at.isoformat(timespec="seconds") if r.at else None,
        } for r in rows]
