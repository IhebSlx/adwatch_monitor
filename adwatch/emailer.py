"""Email delivery via a Power Automate webhook.

The flow: this app POSTs {filename, content (base64), recipient, subject} to
an HTTP-triggered Power Automate flow, which sends the email via Office 365
Outlook with the PDF as an attachment. See README for how the flow is built
(config.POWER_AUTOMATE_WEBHOOK_URL — treat it as a secret, it's a bearer of
its own: whoever has it can trigger the flow)."""
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

    payload = {
        "filename": os.path.basename(pdf_path),
        "content": content_b64,
        "recipient": to,
        "subject": subject,
    }
    resp = requests.post(config.POWER_AUTOMATE_WEBHOOK_URL, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Power Automate webhook failed ({resp.status_code}): {resp.text[:300]}")
