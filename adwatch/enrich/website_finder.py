"""TIER 1 — find a company's website with a web search (Serper), for the rows
where no usable email domain exists. ~$0.001 per company.

Same contract as everywhere in this package: the search only proposes
CANDIDATES. Ranking is deliberately dumb (search order, minus obvious
non-company hosts) because the ACCEPT decision belongs to validate.py, which
checks the site against SAP phone/PLZ/Straße. A plausible-looking top hit is
never enough — that is precisely how the wrong Meta pages got in.
"""
from __future__ import annotations

import requests

from .. import config
from .domains import (
    NON_COMPANY_DOMAINS,
    FREEMAIL_DOMAINS,
    is_usable_company_domain,
    normalize_domain,
    registrable,
)

# Directories/portals that rank well for "<Firma> <Ort>" but are never the
# company's own site. Substring-matched, so regional variants are covered too.
_DIRECTORY_HINTS = (
    "gelbeseiten", "dasoertliche", "das-oertliche", "11880", "wlw.", "europages",
    "northdata", "firmenwissen", "handelsregister", "bundesanzeiger", "unternehmensregister",
    "facebook.", "instagram.", "linkedin.", "xing.", "youtube.", "twitter.", "tiktok.",
    "wikipedia.", "google.", "amazon.", "ebay.", "etsy.", "houzz.", "myhammer",
    "check24", "wer-zu-wem", "kununu", "indeed.", "stepstone", "meinestadt",
    "yelp.", "provenexpert", "golocal", "cylex", "branchenbuch", "marktplatz",
    "gewerbeauskunft", "firmeneintrag", "companyhouse", "moneyhouse", "creditreform",
    "werkenntdenbesten", "kleinanzeigen", "immobilienscout", "jobs.", "stellenanzeige",
    "trustpilot", "bewertet.de", "openstreetmap", "tripadvisor", "pinterest",
)


def _is_directory(domain: str) -> bool:
    d = (domain or "").lower()
    return any(h in d for h in _DIRECTORY_HINTS)


def search_candidates(name: str, city: str | None = None, country: str = "DE",
                      limit: int = 6) -> list[dict]:
    """Serper organic results for this company, reduced to plausible own-site
    domains (deduped, directories/freemail/social removed), best-guess first.

    Raises RuntimeError if no Serper key is configured; returns [] when the
    search simply finds nothing usable."""
    if not config.SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY is not set — needed to search for websites")
    query = f'"{(name or "").strip()}"'
    if city:
        query += f" {city.strip()}"
    query += " Impressum"      # biases hard toward a real company site over a directory

    r = requests.post(
        config.SERPER_SEARCH_URL,
        headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": query, "gl": (country or "de").lower(), "hl": "de", "num": 10},
        timeout=25,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Serper search failed ({r.status_code}): {r.text[:200]}")

    out: list[dict] = []
    seen: set[str] = set()
    for item in (r.json().get("organic") or []):
        dom = normalize_domain(item.get("link"))
        if not dom:
            continue
        reg = registrable(dom) or dom
        if reg in seen:
            continue
        if _is_directory(dom) or not is_usable_company_domain(dom) or reg in FREEMAIL_DOMAINS \
                or reg in NON_COMPANY_DOMAINS:
            continue
        seen.add(reg)
        out.append({
            "domain": reg,
            "title": (item.get("title") or "")[:200],
            "snippet": (item.get("snippet") or "")[:300],
            "position": item.get("position"),
        })
        if len(out) >= limit:
            break
    return out
