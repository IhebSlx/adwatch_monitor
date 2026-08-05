"""Email delivery via a Power Automate webhook.

The flow: this app POSTs {filename, content (base64), recipient, subject, week}
to an HTTP-triggered Power Automate flow, which sends the email via Office 365
Outlook with the PDF as an attachment. `week` ('KW 29') is included so the
flow's email body can reference the week number as dynamic content without
needing an expression to parse it out of the filename. See README for how the
flow is built (config.POWER_AUTOMATE_WEBHOOK_URL — treat it as a secret, it's
a bearer of its own: whoever has it can trigger the flow)."""
from __future__ import annotations

import base64
import logging
import os
import time

import requests

from . import config

# Every send attempt and its outcome go here (data/logs/adwatch.log). An ad-hoc
# send isn't a FetchJob, so without this there is NO record of it anywhere — and
# after a browser "failed to fetch" you cannot tell whether the mail went out or
# not, which risks sending a colleague the same report twice.
log = logging.getLogger("adwatch.emailer")


def send_report_email(pdf_path: str, recipient: str | list[str] | None = None,
                      subject: str = "AdWatch Weekly Report",
                      source: str | None = None) -> None:
    """POST a PDF to the Power Automate webhook so it gets emailed.
    `recipient` may be a single address, a list of addresses (sent to all of
    them at once — Outlook's To field accepts semicolon-separated addresses),
    or omitted to fall back to REPORT_EMAIL_DEFAULT_RECIPIENT.
    Raises RuntimeError if the webhook isn't configured, no recipient is
    known, or the call fails."""
    if not config.POWER_AUTOMATE_WEBHOOK_URL:
        raise RuntimeError("POWER_AUTOMATE_WEBHOOK_URL is not set in .env")
    if isinstance(recipient, list):
        to = "; ".join(r.strip() for r in recipient if r and r.strip())
    else:
        to = (recipient or config.REPORT_EMAIL_DEFAULT_RECIPIENT or "").strip()
    if not to:
        raise RuntimeError("No recipient given and REPORT_EMAIL_DEFAULT_RECIPIENT is not set")

    with open(pdf_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("ascii")

    from .report import week_str_for_filename
    filename = os.path.basename(pdf_path)
    payload = {
        "filename": filename,
        "content": content_b64,
        "recipient": to,
        "subject": subject,
        "week": week_str_for_filename(filename),
    }
    from . import report_log

    addresses = [a.strip() for a in to.split(";") if a.strip()]
    kb = len(content_b64) // 1024
    # logged BEFORE the call, so a crash mid-request still leaves the attempt on
    # record — that is the case you most need to reconstruct afterwards
    log.info("send attempt: %s (%d KB b64) -> %s | subject=%r", filename, kb, to, subject)
    started = time.monotonic()
    try:
        resp = requests.post(config.POWER_AUTOMATE_WEBHOOK_URL, json=payload, timeout=30)
    except Exception as exc:
        log.error("send FAILED (no response) after %.1fs: %s -> %s | %s: %s",
                  time.monotonic() - started, filename, to, type(exc).__name__, exc)
        report_log.record("send_failed", filename, recipients=addresses, subject=subject,
                          source=source, detail=f"{type(exc).__name__}: {exc}")
        raise RuntimeError(f"Power Automate webhook unreachable: {exc}") from exc
    if resp.status_code >= 300:
        log.error("send FAILED (HTTP %s) after %.1fs: %s -> %s | %s",
                  resp.status_code, time.monotonic() - started, filename, to, resp.text[:300])
        report_log.record("send_failed", filename, recipients=addresses, subject=subject,
                          source=source, detail=f"HTTP {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError(f"Power Automate webhook failed ({resp.status_code}): {resp.text[:300]}")
    log.info("send OK (HTTP %s) in %.1fs: %s -> %s",
             resp.status_code, time.monotonic() - started, filename, to)
    report_log.record("sent", filename, recipients=addresses, subject=subject,
                      source=source, detail=f"HTTP {resp.status_code} in "
                                            f"{time.monotonic() - started:.1f}s")


def send_weekly_report(full: bool = False) -> dict:
    """Build the top5 (or full) report from currently stored data and email it
    to every active recipient. Shared by `run.py send-weekly` and the in-app
    scheduler so both paths behave identically."""
    from . import services
    from .report import build_report, build_top5_report, subject_for_filename

    path = build_report() if full else build_top5_report()
    to = [r["email"] for r in services.list_recipients() if r["active"]]
    if not to:
        return {"sent": False, "path": path, "reason": "no active recipients configured"}
    subject = subject_for_filename(os.path.basename(path))
    send_report_email(path, recipient=to, subject=subject, source="schedule")
    return {"sent": True, "path": path, "subject": subject, "recipients": to}
