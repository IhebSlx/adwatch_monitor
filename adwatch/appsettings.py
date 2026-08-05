"""Settings service — reads/writes the in-app overrides for config.SETTINGS_SPEC
and runs lightweight connection tests. Secrets are never shipped to the client
in full: the API returns only whether one is set plus a masked hint.

Precedence lives in config (DB override → .env → default); this module is the
CRUD + validation layer the Settings tab talks to.
"""
from __future__ import annotations

import datetime as dt

import requests
from sqlalchemy import select

from . import config
from .db import SessionLocal
from .models import Setting


def _mask(value: str) -> str:
    """'sk-ant-…-14Dw' → '••••14Dw' — enough to recognise, not to reuse."""
    if not value:
        return ""
    tail = value[-4:] if len(value) >= 8 else ""
    return f"••••{tail}" if tail else "••••"


def get_settings() -> dict:
    """The Settings tab's data: groups of fields with effective values.
    Secret values are masked (hint + configured flag only); non-secret values
    are returned in full so they can be edited in place."""
    groups: dict[str, list] = {}
    for spec in config.SETTINGS_SPEC:
        value = config.resolve_setting(spec["key"])
        field = {
            "key": spec["key"], "label": spec["label"], "help": spec.get("help", ""),
            "secret": spec["secret"], "test": spec.get("test"),
            "source": config.setting_source(spec["key"]),
        }
        if spec["secret"]:
            field["configured"] = bool(value)
            field["hint"] = _mask(value)
            field["value"] = ""            # never send the raw secret
        else:
            field["value"] = value
        groups.setdefault(spec["group"], []).append(field)
    return {"groups": [{"name": name, "fields": fields} for name, fields in groups.items()]}


def save_settings(changes: dict) -> dict:
    """Upsert overrides. A value of "" clears the override (falls back to .env).
    Unknown keys are ignored. Returns {saved: [keys]}."""
    spec_keys = {s["key"] for s in config.SETTINGS_SPEC}
    valid = {k: ("" if v is None else str(v)) for k, v in (changes or {}).items() if k in spec_keys}
    now = dt.datetime.utcnow()
    with SessionLocal() as s:
        for key, value in valid.items():
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value, updated_at=now))
            else:
                row.value, row.updated_at = value, now
        s.commit()
    config.invalidate_settings_cache()     # next config.X read reflects this immediately
    return {"saved": sorted(valid.keys())}


def reveal(key: str) -> dict:
    """Return the actual effective value of one setting (for the Settings
    page's show/hide eye). On-demand only — secrets are still masked in the
    normal GET /api/settings; this ships the real value solely when the user
    explicitly clicks reveal. Behind auth when ACCESS_PASSWORD is set."""
    keys = {s["key"] for s in config.SETTINGS_SPEC}
    if key not in keys:
        return {"value": None}
    return {"value": config.resolve_setting(key)}


def test_connection(which: str, value: str | None = None) -> dict:
    """Validate a credential against its provider. If `value` is given (the key
    the user just typed but hasn't saved), test THAT — so Test reflects what
    you're about to save, not the old stored key. Otherwise test the effective
    saved value. Never echoes the secret back. Returns {ok, detail}."""
    value = (value or "").strip() or None
    try:
        # Power Automate flows: reachability only. A real POST would SEND a report
        # or hit Dataverse, so the test does a HEAD-like probe instead of firing
        # the flow — a "test" button must never have side effects.
        if which and which.startswith("flow_"):
            from . import flows
            role = which[len("flow_"):]
            if role not in flows.FLOW_ROLES:
                return {"ok": False, "detail": f"Unbekannte Flow-Rolle: {role}"}
            url = value or flows.url_for(role)
            if not url:
                return {"ok": False, "detail": flows.missing_message(role)}
            if not url.lower().startswith("https://"):
                return {"ok": False, "detail": "URL muss mit https:// beginnen."}
            import urllib.parse as _up
            host = _up.urlparse(url).hostname or ""
            if "powerplatform.com" not in host and "logic.azure.com" not in host:
                return {"ok": False, "detail": f"Unerwarteter Host „{host}“ — "
                                               "Power-Automate-URLs enden auf "
                                               "powerplatform.com bzw. logic.azure.com."}
            import socket
            try:
                socket.create_connection((host, 443), timeout=10).close()
            except Exception as exc:            # noqa: BLE001
                return {"ok": False, "detail": f"Host {host} nicht erreichbar: {exc}"}
            return {"ok": True, "detail": f"URL plausibel, {host} erreichbar. "
                                          "Der Flow selbst wird erst beim echten "
                                          "Aufruf getestet (ein Test würde sonst "
                                          "wirklich senden)."}

        if which == "apify":
            token = value or config.APIFY_API_TOKEN
            if not token:
                return {"ok": False, "detail": "No Apify token set."}
            r = requests.get("https://api.apify.com/v2/users/me",
                             params={"token": token}, timeout=15)
            if r.status_code == 200:
                name = (r.json().get("data") or {}).get("username", "ok")
                return {"ok": True, "detail": f"Connected as “{name}”."}
            return {"ok": False, "detail": f"Apify rejected the token (HTTP {r.status_code})."}

        if which == "serper":
            key = value or config.SERPER_API_KEY
            if not key:
                return {"ok": False, "detail": "No Serper key set."}
            r = requests.post(config.SERPER_SEARCH_URL,
                              headers={"X-API-KEY": key, "Content-Type": "application/json"},
                              json={"q": "test", "num": 1}, timeout=15)
            if r.status_code < 400:
                return {"ok": True, "detail": "Serper key is valid."}
            return {"ok": False, "detail": f"Serper rejected the key (HTTP {r.status_code})."}

        if which == "anthropic":
            key = value or config.ANTHROPIC_API_KEY
            if not key:
                return {"ok": False, "detail": "No Anthropic key set."}
            r = requests.get("https://api.anthropic.com/v1/models",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             timeout=15)
            if r.status_code == 200:
                return {"ok": True, "detail": "Anthropic key is valid."}
            return {"ok": False, "detail": f"Anthropic rejected the key (HTTP {r.status_code})."}

        return {"ok": False, "detail": f"No test available for '{which}'."}
    except requests.RequestException as e:
        return {"ok": False, "detail": f"Network error: {str(e)[:120]}"}
