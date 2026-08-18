"""Power Automate flows as configurable integration points, not hardcoded URLs.

Before this, one module (emailer) read one constant (POWER_AUTOMATE_WEBHOOK_URL)
and knew how to POST to it. Adding a second flow — a Dataverse query proxy — would
have meant a second constant, a second timeout, a second error convention and a
second place to forget logging.

A flow is addressed by its ROLE ("what the app wants done"), never by URL. The URL
lives in the normal settings store, so it inherits everything the API keys already
have: masked display, reveal-on-demand, DB override of .env, and a test button.

Roles are deliberately code-level, not user-invented: the app has to know what
shape of payload to send and what to do with the answer, so a flow nobody calls
would be dead config. Adding an integration point = one FLOW_ROLES entry + one
SETTINGS_SPEC line, and it appears in Settings automatically.
"""
from __future__ import annotations

import logging
import time

import requests

from . import config

log = logging.getLogger("adwatch.flows")

# role -> (settings key, human label, what the app sends)
FLOW_ROLES: dict[str, tuple[str, str, str]] = {
    "report_email": (
        "FLOW_URL_REPORT_EMAIL", "Bericht per E-Mail senden",
        "{filename, content (base64 PDF), recipient, subject, week}"),
    "crm_query": (
        "FLOW_URL_CRM_QUERY", "Dataverse abfragen (Accounts, Opportunities)",
        "{entity, select, filter, top} -> {value: [rows]}"),
}

# The email flow predates this registry and is configured in many installs as
# POWER_AUTOMATE_WEBHOOK_URL. Fall back to it so upgrading breaks nobody.
_LEGACY_KEYS = {"report_email": "POWER_AUTOMATE_WEBHOOK_URL"}

DEFAULT_TIMEOUT = 30


def url_for(role: str) -> str:
    """Configured URL for a role, or "" when unset. Never raises."""
    key, _, _ = FLOW_ROLES.get(role, (None, None, None))
    if not key:
        raise ValueError(f"Unknown flow role: {role!r}. Known: {sorted(FLOW_ROLES)}")
    url = (config.resolve_setting(key) or "").strip()
    if not url and role in _LEGACY_KEYS:
        url = (config.resolve_setting(_LEGACY_KEYS[role]) or "").strip()
    return url


def is_configured(role: str) -> bool:
    return bool(url_for(role))


def missing_message(role: str) -> str:
    """One consistent, actionable error instead of each caller inventing its own."""
    key, label, _ = FLOW_ROLES[role]
    return (f"Kein Power-Automate-Flow für „{label}“ konfiguriert. "
            f"URL unter Einstellungen → {key} eintragen.")


# Ein Dataverse-Aufruf ohne Filter ist kein "alle Zeilen", sondern ein Fehler:
# der Konnektor antwortet mit
#   BadRequest — The value for OData query '$filter' cannot be empty.
# und weil die Aktion dann scheitert, erreicht der Lauf die Response nie; der
# Aufrufer sieht bloß HTTP 502 NoResponse und sucht den Fehler an der falschen
# Stelle. Gemessen 2026-08-18: drei Testabfragen, drei fehlgeschlagene Läufe,
# eine halbe Stunde Fehlersuche im Flow — der Fehler saß hier.
#
# Aufgefallen ist es nie, weil jeder echte Aufruf bisher einen Filter trug
# (`modifiedon gt ...` beim Delta-Sync). Erst die erste ungefilterte Abfrage
# stolperte darüber.
#
# `statecode eq 0` = aktive Datensätze. Als Voreinstellung fachlich richtig:
# inaktive Firmen, abgeschlossene Leads und stornierte Aktivitäten gehören in
# keine Auswertung, in der sie nicht ausdrücklich verlangt wurden.
_DEFAULT_DATAVERSE_FILTER = "statecode eq 0"


def _guard_payload(role: str, payload: dict) -> dict:
    if role != "crm_query" or not isinstance(payload, dict):
        return payload
    if str(payload.get("filter") or "").strip():
        return payload
    log.info("flow[crm_query] leerer Filter -> Vorgabe '%s'", _DEFAULT_DATAVERSE_FILTER)
    return {**payload, "filter": _DEFAULT_DATAVERSE_FILTER}


def post(role: str, payload: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """POST to the flow behind `role` and return its parsed JSON body ({} if the
    flow answers with no body, which a fire-and-forget flow legitimately does).

    Logs the attempt BEFORE the call, so a crash mid-request still leaves a record
    — the case that made a failed report send unexplainable. Raises RuntimeError
    for every failure mode so callers handle exactly one exception type.
    """
    url = url_for(role)
    if not url:
        raise RuntimeError(missing_message(role))

    payload = _guard_payload(role, payload)

    # never log the URL itself: it is a bearer secret, anyone holding it can
    # trigger the flow
    log.info("flow[%s] POST (%d keys)", role, len(payload or {}))
    started = time.monotonic()
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except Exception as exc:                                   # noqa: BLE001
        log.error("flow[%s] FAILED (no response) after %.1fs: %s: %s",
                  role, time.monotonic() - started, type(exc).__name__, exc)
        raise RuntimeError(f"Flow „{FLOW_ROLES[role][1]}“ nicht erreichbar: {exc}") from exc

    took = time.monotonic() - started
    if resp.status_code >= 300:
        log.error("flow[%s] FAILED (HTTP %s) after %.1fs: %s",
                  role, resp.status_code, took, resp.text[:300])
        raise RuntimeError(f"Flow „{FLOW_ROLES[role][1]}“ antwortete "
                           f"HTTP {resp.status_code}: {resp.text[:300]}")
    log.info("flow[%s] OK (HTTP %s) in %.1fs", role, resp.status_code, took)
    try:
        return resp.json() if (resp.text or "").strip() else {}
    except ValueError:
        return {"raw": resp.text[:2000]}


def status() -> list[dict]:
    """Which integration points exist and which are wired up — for Settings and
    for an honest 'what can this install actually do' answer."""
    out = []
    for role, (key, label, payload) in FLOW_ROLES.items():
        out.append({"role": role, "key": key, "label": label,
                    "payload": payload, "configured": is_configured(role),
                    "legacy_key": _LEGACY_KEYS.get(role)})
    return out
