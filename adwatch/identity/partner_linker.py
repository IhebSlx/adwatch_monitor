"""PART 1b — automatic partner-page discovery via landing-URL evidence.

The problem: a company's ads may run from accounts we'd never find by
searching the company's name (e.g. "Solarlux Quality Partner Westfalen"
advertising FOR Nagelschmidt). The reliable fingerprint is the ad's landing
URL on the hub domain:

    https://solarlux.com/de-de/landing/wintergarten-nagelschmidt/
        ?utm_campaign=DE%20Nagelschmidt%20Online%20Kampagnen

Weekly, `run_sweep()` searches the Ad Library for the hub term(s), groups the
returned ads by page, extracts every landing URL, and matches distinctive
company-name tokens against the URL path + utm_campaign:

- page's evidence points to exactly ONE monitored company
    -> dedicated partner account: auto-link it (CompanyPage role="partner",
       status="auto" — visible and editable in the UI) and attribute ALL its
       ads to that company.
- page's evidence points to SEVERAL companies
    -> shared hub: attribute only the matching ads to each company, per ad;
       no exclusive page link (it re-attributes fresh every weekly sweep).
"""
from __future__ import annotations

import re
from urllib.parse import unquote_plus, urlparse

import yaml
from sqlalchemy import select

from .. import config
from ..models import CompanyPage

_CFG_PATH = config.CONFIG_DIR / "partner_discovery.yaml"


def load_config() -> dict:
    if not _CFG_PATH.exists():
        return {"enabled": False}
    with open(_CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {"enabled": False}


# ---------------------------------------------------------------------------
# Normalization + token matching
# ---------------------------------------------------------------------------

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                          "Ä": "ae", "Ö": "oe", "Ü": "ue"})


def normalize(s: str) -> str:
    """lowercase, fold umlauts, drop everything but a-z0-9 — for single tokens."""
    return re.sub(r"[^a-z0-9]", "", (s or "").translate(_UMLAUTS).lower())


def normalize_haystack(s: str) -> str:
    """Like normalize() but keeps separators as single spaces, so token matching
    can respect word boundaries ('schmidt' must NOT match inside
    'wintergarten-nagelschmidt' — but 'nagelschmidt' must)."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").translate(_UMLAUTS).lower()).strip()


def excluded_page_patterns(cfg: dict | None = None) -> list[str]:
    """Normalized name fragments of pages whose ads must always be excluded
    (see `excluded_pages` in partner_discovery.yaml)."""
    cfg = load_config() if cfg is None else cfg
    return [p for p in (normalize(x) for x in cfg.get("excluded_pages", [])) if p]


def is_excluded_page(page_name: str | None, cfg: dict | None = None) -> bool:
    """True if this advertiser page is on the exclusion list. Matched as a
    normalized substring, so regional variants ("… Westfalen") are covered."""
    hay = normalize(page_name or "")
    return bool(hay) and any(pat in hay for pat in excluded_page_patterns(cfg))


def company_tokens(name: str, cfg: dict) -> list[str]:
    """Distinctive tokens of a company name usable as URL evidence.
    'Nagelschmidt Fenster und Rollladen GmbH' -> ['nagelschmidt']"""
    from ..collect.meta_source import search_term
    stop = {normalize(t) for t in cfg.get("stop_tokens", [])}
    min_len = int(cfg.get("min_token_len", 5))
    out = []
    for word in search_term(name).split():
        tok = normalize(word)
        if len(tok) >= min_len and tok not in stop:
            out.append(tok)
    return out


def extract_landing_evidence(item: dict, hub_domain: str) -> list[dict]:
    """All hub-domain landing URLs in a raw ad item (top level + carousel cards),
    each returned as {url, haystack} where haystack is the normalized
    path + utm_campaign text to match tokens against."""
    snap = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    urls = [snap.get("link_url")]
    for card in snap.get("cards") or []:
        if isinstance(card, dict):
            urls.append(card.get("link_url"))

    out = []
    for url in filter(None, urls):
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if hub_domain not in (parsed.netloc or "").lower():
            continue
        utm = ""
        m = re.search(r"utm_campaign=([^&]+)", parsed.query or "")
        if m:
            utm = unquote_plus(m.group(1))
        out.append({"url": url.split("?")[0], "utm": utm,
                    "haystack": normalize_haystack(parsed.path) + " | " + normalize_haystack(utm)})
    return out


def match_company(evidence: list[dict], token_map: dict[int, list[str]]) -> dict[int, dict]:
    """Which monitored companies does this ad's evidence point to?
    Boundary-aware: 'schmidt' must not match inside 'nagelschmidt'.
    Returns {company_id: winning_evidence}."""
    hits: dict[int, dict] = {}
    for ev in evidence:
        for cid, tokens in token_map.items():
            for tok in tokens:
                if re.search(rf"(?<!\w){re.escape(tok)}(?!\w)", ev["haystack"]):
                    hits.setdefault(cid, {**ev, "token": tok})
    return hits


# ---------------------------------------------------------------------------
# The weekly sweep
# ---------------------------------------------------------------------------

def run_sweep(source, session, companies) -> list[dict]:
    """Discover + attribute partner ads. Returns attribution groups:
    [{company_id, page_id, page_name, role, evidence, ads: [RawAd]}]
    Creates CompanyPage links (status='auto') for dedicated partner pages.
    Uses the caller's session; caller commits."""
    from ..collect.meta_source import MetaAdSource

    cfg = load_config()
    if not cfg.get("enabled"):
        return []
    hub_domain = (cfg.get("hub_domain") or "").lower()
    if not hub_domain:
        return []

    token_map = {c.id: company_tokens(c.name, cfg) for c in companies}
    token_map = {cid: toks for cid, toks in token_map.items() if toks}
    if not token_map:
        return []

    already_linked = {p.page_id for p in session.scalars(
        select(CompanyPage).where(CompanyPage.source == "meta", CompanyPage.active))}

    # -- gather raw items from every sweep term ------------------------------
    items: list[dict] = []
    seen_ad_ids: set[str] = set()
    for term in cfg.get("search_terms", []):
        for it in source.sweep_hub(term, country=config.DEFAULT_COUNTRY,
                                   max_ads=int(cfg.get("max_ads", 300))):
            ad_id = str(it.get("ad_archive_id") or it.get("id") or "")
            if ad_id and ad_id in seen_ad_ids:
                continue
            seen_ad_ids.add(ad_id)
            items.append(it)

    # -- group by page, match evidence per ad --------------------------------
    pages: dict[str, dict] = {}
    for it in items:
        pid = str(it.get("page_id") or "")
        if not pid or pid in already_linked:
            continue  # linked pages are fetched directly in the main loop
        snap = it.get("snapshot") if isinstance(it.get("snapshot"), dict) else {}
        page_name = it.get("page_name") or snap.get("page_name") or ""
        if is_excluded_page(page_name, cfg):
            continue  # excluded page — never link or attribute its ads
        page = pages.setdefault(pid, {
            "page_id": pid,
            "page_name": page_name,
            "matches": {},   # company_id -> {"ads": [RawAd], "evidence": {...}}
            "all_ads": [],
        })
        raw_ad = MetaAdSource._map_ad(it)
        page["all_ads"].append(raw_ad)
        for cid, ev in match_company(extract_landing_evidence(it, hub_domain), token_map).items():
            bucket = page["matches"].setdefault(cid, {"ads": [], "evidence": ev})
            bucket["ads"].append(raw_ad)

    # -- decide: dedicated partner page vs shared hub -------------------------
    groups: list[dict] = []
    for page in pages.values():
        matched_cids = list(page["matches"].keys())
        if not matched_cids:
            continue
        if len(matched_cids) == 1:
            cid = matched_cids[0]
            ev = page["matches"][cid]["evidence"]
            session.add(CompanyPage(
                company_id=cid, source="meta", page_id=page["page_id"],
                page_name=page["page_name"], role="partner", status="auto",
                evidence={"method": "landing_url", "url": ev["url"],
                          "utm_campaign": ev["utm"], "token": ev["token"]},
            ))
            groups.append({"company_id": cid, "page_id": page["page_id"],
                           "page_name": page["page_name"], "role": "partner",
                           "evidence": ev, "ads": page["all_ads"]})
        else:
            for cid, bucket in page["matches"].items():
                groups.append({"company_id": cid, "page_id": page["page_id"],
                               "page_name": page["page_name"], "role": "hub",
                               "evidence": bucket["evidence"], "ads": bucket["ads"]})
    return groups
