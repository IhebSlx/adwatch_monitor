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
import os

import requests

from . import config


def send_report_email(pdf_path: str, recipient: str | list[str] | None = None,
                      subject: str = "AdWatch Weekly Report") -> None:
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
    resp = requests.post(config.POWER_AUTOMATE_WEBHOOK_URL, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Power Automate webhook failed ({resp.status_code}): {resp.text[:300]}")


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
    send_report_email(path, recipient=to, subject=subject)
    return {"sent": True, "path": path, "subject": subject, "recipients": to}
