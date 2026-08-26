"""Central configuration: paths, run mode, credentials, tunable assumptions."""
from __future__ import annotations

import os
import threading as _thr
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
FIXTURES_DIR = ROOT / "fixtures"
# Overridable so a persistent volume (e.g. on Railway) can be mounted outside
# the app directory — everything under it survives redeploys/restarts.
DATA_DIR = Path(os.getenv("ADWATCH_DATA_DIR", "").strip() or (ROOT / "data"))
OUTPUT_DIR = Path(os.getenv("ADWATCH_OUTPUT_DIR", "").strip() or (ROOT / "output"))

DB_URL = os.getenv("ADWATCH_DB_URL", "").strip() or f"sqlite:///{DATA_DIR / 'adwatch.db'}"

# ---- advanced / env-only tunables (bootstrap or rarely touched) -----------
APIFY_RUN_TIMEOUT_SECONDS = int(os.getenv("APIFY_RUN_TIMEOUT_SECONDS", "180"))
APIFY_POLL_INTERVAL_SECONDS = int(os.getenv("APIFY_POLL_INTERVAL_SECONDS", "3"))
# The Google actor drives a real headless browser per ad — longer budget.
GOOGLE_APIFY_RUN_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_APIFY_RUN_TIMEOUT_SECONDS", "600"))
SEARCHAPI_KEY = os.getenv("SEARCHAPI_KEY", "").strip()
SEARCHAPI_URL = os.getenv("SEARCHAPI_URL", "https://www.searchapi.io/api/v1/search").strip()
LIVE_SOURCE = os.getenv("LIVE_SOURCE", "apify").strip().lower()   # apify | searchapi

# ---- Access control -------------------------------------------------------
# When set, the whole app requires HTTP Basic auth (username ADWATCH_USER,
# default "adwatch"). Unset => no auth, which is only safe on 127.0.0.1 (the
# default bind). MUST be set before exposing the app on a network / hosting.
ACCESS_PASSWORD = os.getenv("ADWATCH_ACCESS_PASSWORD", "").strip()
ACCESS_USER = os.getenv("ADWATCH_ACCESS_USER", "adwatch").strip() or "adwatch"

# ---- Backups --------------------------------------------------------------
BACKUP_DIR = Path(os.getenv("ADWATCH_BACKUP_DIR", "").strip() or (DATA_DIR / "backups"))
# Von 14 auf 7 gesenkt, als die E-Mail-Korrespondenz dazukam. Gerechnet, nicht
# geschätzt: die Datenbank wächst von 212 MB auf rund 1,3 GB, 14 Kopien wären
# also ~18 GB Plattenplatz für ein Werkzeug, das auf einem Arbeitsplatzrechner
# läuft.
#
# Sieben Tage sind hier vertretbar, weil der teuerste Teil des Bestands NICHT
# in den E-Mails steckt: die sind ein Spiegel des CRM und jederzeit neu
# abrufbar. Unersetzlich sind bezahlte Anreicherung, geprüfte Identitäten und
# menschliche Entscheidungen — und die ändern sich langsam genug, dass eine
# Woche Rückgriff reicht. Wer mehr will, setzt ADWATCH_BACKUP_KEEP.
BACKUP_KEEP = int(os.getenv("ADWATCH_BACKUP_KEEP", "7"))   # rotated daily copies to retain

# ---------------------------------------------------------------------------
# In-app–customisable settings (the Settings tab). Each is resolved live at
# access time via __getattr__ below: an override saved in the DB wins, else
# the .env variable, else the default. Editing them in the app takes effect
# immediately — no restart — because every call site reads `config.X`.
# `secret` values are masked in the API; `test` names a connection check.
# ---------------------------------------------------------------------------
SETTINGS_SPEC = [
    {"key": "APIFY_API_TOKEN", "env": "APIFY_API_TOKEN", "default": "", "secret": True,
     "test": "apify", "group": "Scraping — Meta & Google ads", "label": "Apify API token",
     "help": "Powers the Ad lookup: scrapes the Meta Ad Library (and Google Ads via the actors below)."},
    {"key": "APIFY_ACTOR_ID", "env": "APIFY_ACTOR_ID", "default": "", "secret": False,
     "test": None, "group": "Scraping — Meta & Google ads", "label": "Meta Ad Library actor ID",
     "help": "The Apify actor that scrapes Meta ads."},
    {"key": "GOOGLE_ADS_ACTOR_ID", "env": "GOOGLE_ADS_ACTOR_ID", "default": "", "secret": False,
     "test": None, "group": "Scraping — Meta & Google ads", "label": "Google Ads actor ID",
     "help": "Optional — the Apify actor for Google Ads Transparency (uses the same token)."},

    {"key": "SERPER_API_KEY", "env": "SERPER_API_KEY", "default": "", "secret": True,
     "test": "serper", "group": "Identity resolution", "label": "Serper.dev API key",
     "help": "Finds each company's Facebook / Instagram page via Google — powers the Identity check."},
    {"key": "SERPER_SEARCH_URL", "env": "SERPER_SEARCH_URL",
     "default": "https://google.serper.dev/search", "secret": False, "test": None,
     "group": "Identity resolution", "label": "Serper search URL", "help": "Rarely changed."},

    {"key": "ANTHROPIC_API_KEY", "env": "ANTHROPIC_API_KEY", "default": "", "secret": True,
     "test": "anthropic", "group": "AI — Claude", "label": "Anthropic API key",
     "help": "Claude judges identity candidates, generates search keywords, and classifies ad copy. Optional."},
    {"key": "ANTHROPIC_MODEL", "env": "ANTHROPIC_MODEL", "default": "claude-haiku-4-5-20251001",
     "secret": False, "test": None, "group": "AI — Claude", "label": "Claude model"},

    # Power Automate flows are addressed by ROLE, not by a single hardcoded URL —
    # see adwatch/flows.py. Adding an integration point = one FLOW_ROLES entry
    # plus one line here, and it shows up in Settings with masking and a test
    # button for free.
    {"key": "FLOW_URL_REPORT_EMAIL", "env": "FLOW_URL_REPORT_EMAIL", "default": "",
     "secret": True, "test": "flow_report_email", "group": "Power Automate flows",
     "label": "Flow: Bericht per E-Mail senden",
     "help": "POST {filename, content (base64 PDF), recipient, subject, week}. "
             "Whoever has this URL can trigger the flow — treat it as a password."},
    {"key": "FLOW_URL_CRM_QUERY", "env": "FLOW_URL_CRM_QUERY", "default": "",
     "secret": True, "test": "flow_crm_query", "group": "Power Automate flows",
     "label": "Flow: Dataverse abfragen",
     "help": "POST {entity, select, filter, top} -> {value:[rows]}. Read-only. "
             "Used for the CRM sync and scoped loads."},
    {"key": "FLOW_URL_GRAPH_USERS", "env": "FLOW_URL_GRAPH_USERS", "default": "",
     "secret": True, "test": "flow_graph_users", "group": "Power Automate flows",
     "label": "Flow: Personen suchen (Office 365)",
     "help": "POST {suche, top} -> {value:[{displayName, mail, jobTitle, department}]}. "
             "Nur lesend. Füllt die Empfängerauswahl, damit Adressen gewählt statt "
             "getippt werden. Optional — ohne diesen Flow bleibt das Eingabefeld."},
    # Legacy key: installs configured before the flow registry existed. flows.py
    # falls back to it for the report_email role, so nothing breaks on upgrade.
    {"key": "POWER_AUTOMATE_WEBHOOK_URL", "env": "POWER_AUTOMATE_WEBHOOK_URL", "default": "",
     "secret": True, "test": None, "group": "Power Automate flows",
     "label": "Webhook URL (alt — bitte oben eintragen)",
     "help": "Vorgänger von „Flow: Bericht per E-Mail senden“. Wird weiter genutzt, "
             "wenn das neue Feld leer ist."},
    {"key": "REPORT_EMAIL_DEFAULT_RECIPIENT", "env": "REPORT_EMAIL_DEFAULT_RECIPIENT", "default": "",
     "secret": False, "test": None, "group": "Email delivery", "label": "Default recipient e-mail"},

    {"key": "DEFAULT_COUNTRY", "env": "ADWATCH_COUNTRY", "default": "DE", "secret": False,
     "test": None, "group": "General", "label": "Default country code",
     "help": "Two-letter code (e.g. DE) used as the region for ad and identity searches."},
]
_SPEC_BY_KEY = {s["key"]: s for s in SETTINGS_SPEC}

# cache of DB overrides {key: value}; None = not loaded yet
_db_overrides: dict | None = None
_lock = _thr.Lock()


def _load_overrides() -> dict:
    """Read the app_settings table. Empty dict if the table doesn't exist yet
    (fresh DB, pre-migration) or on any DB error — callers then use .env."""
    try:
        from .db import SessionLocal
        from .models import Setting
        from sqlalchemy import select
        with SessionLocal() as s:
            return {r.key: r.value for r in s.scalars(select(Setting)) if r.value not in (None, "")}
    except Exception:
        return {}


def _overrides() -> dict:
    global _db_overrides
    if _db_overrides is None:
        with _lock:
            if _db_overrides is None:
                _db_overrides = _load_overrides()
    return _db_overrides


def invalidate_settings_cache() -> None:
    """Call after saving settings so the next `config.X` read reflects them."""
    global _db_overrides
    with _lock:
        _db_overrides = None


def resolve_setting(key: str) -> str:
    """DB override → .env → default, for one SETTINGS_SPEC key."""
    spec = _SPEC_BY_KEY[key]
    db = _overrides().get(key)
    if db not in (None, ""):
        return db.strip() if isinstance(db, str) else db
    return os.getenv(spec["env"], "").strip() or spec["default"]


def setting_source(key: str) -> str:
    """Where the effective value comes from: 'custom' (DB), 'env', or 'default'."""
    spec = _SPEC_BY_KEY[key]
    if _overrides().get(key) not in (None, ""):
        return "custom"
    if os.getenv(spec["env"], "").strip():
        return "env"
    return "default"


def __getattr__(name):
    # module-level dynamic attribute: `config.APIFY_API_TOKEN` etc. resolve here
    if name in _SPEC_BY_KEY:
        return resolve_setting(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def load_companies() -> list[dict]:
    with open(CONFIG_DIR / "companies.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f).get("companies", [])


def load_spend_assumptions() -> dict:
    with open(CONFIG_DIR / "spend_assumptions.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
