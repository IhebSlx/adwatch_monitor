"""One registry for per-country knowledge, loaded from config/markets.yaml.

Adding a market used to require three separate code edits — the country-name
aliases in customers._COUNTRY_CODES, the legal-page term in
website_finder._LEGAL_PAGE_TERM and the search language in ._UI_LANG. Miss one and
the failure is silent: forgetting an alias is how 982 Spanish companies were first
imported as "DE", and searching for "Impressum" in Spain simply finds nothing.

Now it is a data file. A successor adds a market by copying a block in
config/markets.yaml — no Python, no deploy.

Fails SAFE rather than loudly: if the YAML is missing or malformed the built-in
DACH + ES/PT defaults below still work, because an unparseable config file must
not take down an app that is mid-import.
"""
from __future__ import annotations

import logging
import threading

import yaml

from . import config

log = logging.getLogger("adwatch.markets")

# Minimum viable set if config/markets.yaml is missing or broken. Deliberately
# only the markets actually in use — a silent fallback should not pretend to
# support countries nobody checked.
_FALLBACK: dict[str, dict] = {
    "DE": {"aliases": ["deutschland", "germany"], "search_lang": "de", "legal_page": "Impressum"},
    "AT": {"aliases": ["österreich", "austria"], "search_lang": "de", "legal_page": "Impressum"},
    "CH": {"aliases": ["schweiz", "switzerland"], "search_lang": "de", "legal_page": "Impressum"},
    "ES": {"aliases": ["spanien", "españa", "spain"], "search_lang": "es", "legal_page": "aviso legal"},
    "PT": {"aliases": ["portugal"], "search_lang": "pt", "legal_page": "contactos"},
}

_cache: dict | None = None
_alias_cache: dict[str, str] | None = None
_lock = threading.Lock()


def _load() -> dict:
    path = config.CONFIG_DIR / "markets.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        markets = data.get("markets") or {}
        if not isinstance(markets, dict) or not markets:
            raise ValueError("no 'markets' mapping")
        out = {}
        for code, spec in markets.items():
            code = str(code).strip().upper()
            if len(code) != 2 or not isinstance(spec, dict):
                log.warning("markets.yaml: skipping invalid entry %r", code)
                continue
            out[code] = {
                "aliases": [str(a).strip().lower() for a in (spec.get("aliases") or [])],
                "search_lang": str(spec.get("search_lang") or "de").strip(),
                "legal_page": str(spec.get("legal_page") or "contact").strip(),
                "ad_region": str(spec.get("ad_region") or code).strip().upper(),
            }
        return out
    except FileNotFoundError:
        log.warning("config/markets.yaml not found — using built-in defaults")
    except Exception as exc:                       # noqa: BLE001
        log.error("config/markets.yaml unreadable (%s) — using built-in defaults", exc)
    return {k: {**v, "ad_region": k} for k, v in _FALLBACK.items()}


def all_markets() -> dict:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = _load()
    return _cache


def reload() -> int:
    """Drop the cache so an edited markets.yaml takes effect. Returns the count."""
    global _cache, _alias_cache
    with _lock:
        _cache = None
        _alias_cache = None
    return len(all_markets())


def _aliases() -> dict[str, str]:
    """alias (lowercase) -> ISO code. Includes umlaut-stripped variants, because
    exports are inconsistent about them."""
    global _alias_cache
    if _alias_cache is None:
        table: dict[str, str] = {}
        for code, spec in all_markets().items():
            table[code.lower()] = code
            for alias in spec["aliases"]:
                table[alias] = code
                stripped = (alias.replace("ä", "a").replace("ö", "o").replace("ü", "u")
                                 .replace("ß", "ss").replace("é", "e").replace("í", "i")
                                 .replace("ñ", "n").replace("è", "e"))
                if stripped != alias:
                    table[stripped] = code
        with _lock:
            _alias_cache = table
    return _alias_cache


def code_for(value) -> str | None:
    """ISO-2 code for whatever a source calls a country. None if unrecognised —
    the caller decides whether to default, so a typo never silently becomes DE."""
    s = str(value or "").strip().lower()
    if not s:
        return None
    hit = _aliases().get(s)
    if hit:
        return hit
    # an unknown but plausible 2-letter code is passed through: better to keep
    # "SI" than to drop the row or mislabel it
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return None


def search_lang(country: str | None) -> str:
    m = all_markets().get((country or "").upper())
    return m["search_lang"] if m else "de"


def legal_page_term(country: str | None) -> str:
    m = all_markets().get((country or "").upper())
    return m["legal_page"] if m else "contact"


def ad_region(country: str | None) -> str:
    m = all_markets().get((country or "").upper())
    return m["ad_region"] if m else (country or config.DEFAULT_COUNTRY or "DE").upper()


def known_codes() -> list[str]:
    return sorted(all_markets())
