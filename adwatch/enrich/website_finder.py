"""TIER 1 — find a company's website with a web search (Serper), for the rows
where no usable email domain exists. ~$0.001 per company.

Same contract as everywhere in this package: the search only proposes
CANDIDATES. Ranking is deliberately dumb (search order, minus obvious
non-company hosts) because the ACCEPT decision belongs to validate.py, which
checks the site against SAP phone/PLZ/Straße. A plausible-looking top hit is
never enough — that is precisely how the wrong Meta pages got in.
"""
from __future__ import annotations

import re

import requests

from .. import config, markets
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
    # registries/phone books that slipped through in live testing (they rank well
    # for "<Firma> <Ort> Impressum" and would clog the review queue):
    "openregister", "registercheck", "telefonbuch", "oeffnungszeiten",
    "unternehmensverzeichnis", "branchenverzeichnis", "firmenabc", "stadtbranchenbuch",
    "werliefertwas", "kompass.com", "herold.", "infobel", "yellowmap", "cybo.",
    "ortsdienst", "unternehmen24", "firmenkontor", "bizim.", "dnb.com",
    "wogibtes", "branchen-info", "meinprospekt", "kaufda",
)


def _is_directory(domain: str) -> bool:
    d = (domain or "").lower()
    return any(h in d for h in _DIRECTORY_HINTS)


_LEGAL_FORM_RE = re.compile(
    r"\b(gmbh\s*&\s*co\.?\s*kg|gmbh|ag|kg|ohg|gbr|ug|e\.?\s?k\.?|inh\.?[^,]*|nachf\.?"
    r"|s\.?\s?l\.?\s?u\.?|s\.?\s?l\.?\s?n\.?\s?e\.?|s\.?\s?l\.?l\.?|s\.?\s?l\.?"
    r"|s\.?\s?a\.?\s?u\.?|s\.?\s?a\.?|s\.?\s?c\.?\s?p\.?|lda\.?|unipessoal)\b\.?",
    re.IGNORECASE)

# The "find the Impressum" trick is language-specific: a Spanish site has no
# Impressum, it has an 'aviso legal'. Searching the German term against Spain
# actively pushes the real company site out of the results.
_LEGAL_PAGE_TERM = {
    "DE": "Impressum", "AT": "Impressum", "CH": "Impressum",
    "ES": "aviso legal", "PT": "contactos", "FR": "mentions légales",
    "IT": "contatti", "NL": "contact", "PL": "kontakt",
}
_UI_LANG = {"ES": "es", "PT": "pt", "FR": "fr", "IT": "it", "NL": "nl", "PL": "pl",
            "GB": "en", "IE": "en", "US": "en"}


def _core_name(name: str) -> str:
    """The searchable core of a legal company name: 'Metallbau Thomas Saß GmbH'
    -> 'Metallbau Thomas Saß'. Small firms' websites rarely carry the full
    legal form, so the loose second query searches without it."""
    core = _LEGAL_FORM_RE.sub(" ", name or "")
    return re.sub(r"\s{2,}", " ", core).strip(" -–·,")


def _run_query(query: str, country: str, limit: int, seen: set[str]) -> list[dict]:
    cc = (country or "DE").upper()
    r = requests.post(
        config.SERPER_SEARCH_URL,
        headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": query, "gl": cc.lower(), "hl": markets.search_lang(cc), "num": 10},
        timeout=25,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Serper search failed ({r.status_code}): {r.text[:200]}")
    out: list[dict] = []
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


def search_candidates(name: str, city: str | None = None, country: str = "DE",
                      limit: int = 4) -> list[dict]:
    """Serper organic results for this company, reduced to plausible own-site
    domains (deduped, directories/freemail/social removed), best-guess first.

    Two-step: an exact quoted query first; if that yields nothing usable, a
    looser one without quotes/legal form ('Metallbau Thomas Saß Neu Karin
    Website') — small firms' sites rarely carry the exact legal name. The loose
    step can surface wrong-but-plausible domains, which is fine: the validation
    gate (validate.py) decides acceptance, not the search ranking.

    Raises RuntimeError if no Serper key is configured; returns [] when the
    search simply finds nothing usable."""
    if not config.SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY is not set — needed to search for websites")
    name = (name or "").strip()
    city = (city or "").strip()
    seen: set[str] = set()
    legal_term = markets.legal_page_term(country)

    strict = f'"{name}"' + (f" {city}" if city else "") + f" {legal_term}"
    out = _run_query(strict, country, limit, seen)
    if out:
        return out

    core = _core_name(name)
    if core and (core.lower() != name.lower() or city):
        loose = f"{core}" + (f" {city}" if city else "") + " Website"
        out = _run_query(loose, country, limit, seen)
    return out
