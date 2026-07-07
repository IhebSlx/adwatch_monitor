"""Central configuration: paths, run mode, credentials, tunable assumptions."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# mock = offline on generated sample data | live = real Apify calls. Default LIVE.
MODE = os.getenv("ADWATCH_MODE", "live").strip().lower()

DB_URL = os.getenv("ADWATCH_DB_URL", "").strip() or f"sqlite:///{ROOT / 'data' / 'adwatch.db'}"

# ---- Apify (primary live source) ------------------------------------------
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "").strip()
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "").strip()
APIFY_RUN_TIMEOUT_SECONDS = int(os.getenv("APIFY_RUN_TIMEOUT_SECONDS", "180"))
APIFY_POLL_INTERVAL_SECONDS = int(os.getenv("APIFY_POLL_INTERVAL_SECONDS", "3"))

# ---- SearchAPI.io (optional alternate live source) -------------------------
SEARCHAPI_KEY = os.getenv("SEARCHAPI_KEY", "").strip()
SEARCHAPI_URL = os.getenv("SEARCHAPI_URL", "https://www.searchapi.io/api/v1/search").strip()

# Which live backend to use: apify | searchapi
LIVE_SOURCE = os.getenv("LIVE_SOURCE", "apify").strip().lower()

# ---- Anthropic (optional ad-copy classification upgrade) -------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()

CONFIG_DIR = ROOT / "config"
FIXTURES_DIR = ROOT / "fixtures"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

DEFAULT_COUNTRY = os.getenv("ADWATCH_COUNTRY", "DE").strip() or "DE"


def is_live() -> bool:
    return MODE == "live"


def load_companies() -> list[dict]:
    with open(CONFIG_DIR / "companies.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f).get("companies", [])


def load_spend_assumptions() -> dict:
    with open(CONFIG_DIR / "spend_assumptions.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
