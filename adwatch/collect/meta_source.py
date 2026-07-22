"""Meta Ad Library adapter — live via Apify (curious_coder/facebook-ads-library-scraper,
actor id XtaWFhbtfxyzqrFmd), with SearchAPI.io as an optional alternate backend.

KEY IDEA (page-identity problem): this actor takes an arbitrary Facebook Ads Library
*URL* (keyword search OR page view), not a pre-known page_id. So a company only gets a
confirmed page_id once we've actually run a keyword search and seen which page its ads
come from. `search_and_resolve()` does that in ONE paid Apify run: it searches by company
name, groups the returned ads by page_id, and picks the best name match — so the same
call both resolves identity AND returns this week's ad data, at no extra cost.

Every raw item is stored (Ad.source_raw) so field mapping can be corrected later without
re-scraping, since exact nesting wasn't verifiable from outside a live account.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from urllib.parse import quote_plus, urlencode

import requests
import tldextract
from cleanco import basename as _cleanco_basename
from rapidfuzz import fuzz

from .. import config
from .base import AdSource, PageCandidate, RawAd

APIFY_BASE = "https://api.apify.com/v2"
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "TIMED_OUT", "ABORTED"}


class ApifyQuotaError(RuntimeError):
    """Apify rejected the run because the account's usage/hard limit is reached.
    Unlike a one-off fetch error this affects EVERY subsequent call, so the batch
    must stop immediately rather than retry company-by-company (which would just
    log the same 403 thousands of times and waste the whole run)."""

# How confident a single-page or best-vs-runner-up match needs to be to auto-confirm.
SIM_CONFIRM_SINGLE = 0.30   # only one distinct page in results
SIM_CONFIRM = 0.50          # multiple pages: top candidate similarity floor
SIM_MARGIN = 0.15           # top candidate must lead the runner-up by this much

# Facebook page categories that are never a legitimate business match for a
# window/facade dealer — a page in one of these can rank #1 by name similarity
# alone (political pages run huge ad volumes and often share common surname
# tokens) but must never be auto-confirmed as a company's identity.
_BLOCKED_CATEGORIES = {
    "politician", "political party", "political organization",
    "government official", "government organization", "public figure",
    "musician/band", "artist", "author", "actor", "athlete",
    "news & media website", "religious organization", "community organization",
}


def _category_is_blocked(category: str | None) -> bool:
    if not category:
        return False
    c = category.strip().lower()
    return any(term in c for term in _BLOCKED_CATEGORIES)


def _clean_name_for_similarity(name: str) -> str:
    """Strip legal-entity suffixes (GmbH, Co. KG, ...) via cleanco, then reduce
    to lowercase whitespace-separated tokens — rapidfuzz's token_set_ratio
    needs real word boundaries, unlike the old char-level normalization."""
    base = _cleanco_basename(name or "") or (name or "")
    return re.sub(r"[^\w\s]", " ", base).lower().strip()


def _registered_domain(url_or_domain: str | None) -> str | None:
    """'https://www.fenster-mueller.de/aktion?x=1' -> 'fenster-mueller.de';
    also accepts a bare domain. None if nothing extractable (e.g. a fb.com link)."""
    if not url_or_domain:
        return None
    ext = tldextract.extract(url_or_domain)
    if not ext.domain:
        return None
    return f"{ext.domain}.{ext.suffix}".lower() if ext.suffix else ext.domain.lower()


def _normalize_actor_id(value: str) -> str:
    """Accepts a bare actor id OR a full console URL and returns the bare id,
    e.g. 'https://console.apify.com/actors/XtaWFhbtfxyzqrFmd/input' -> 'XtaWFhbtfxyzqrFmd'."""
    if not value:
        return ""
    v = value.strip()
    if v.startswith("http"):
        v = v.rstrip("/")
        for marker in ("/actors/", "/acts/"):
            if marker in v:
                v = v.split(marker, 1)[1]
                if "/" in v:
                    v = v.split("/", 1)[0]
                break
    return v


# German/EU legal forms + generic descriptors that make a keyword_unordered search
# too strict (it requires EVERY word to appear in an ad). Stripped to a searchable term.
_LEGAL_TOKENS = {
    "gmbh", "mbh", "ohg", "kg", "ag", "ug", "gbr", "se", "kgaa", "ek", "e.k.",
    "co", "co.", "&", "vertriebs", "vertrieb", "haftungsbeschränkt",
}


def search_term(name: str) -> str:
    """Turn a formal company name into a keyword-search term: drop legal forms and
    hyphenated product descriptors so the Ad Library actually returns the page.
    e.g. 'Fortuna Wintergarten Vertriebs GmbH' -> 'Fortuna Wintergarten'."""
    # split on whitespace; also treat a hyphenated descriptor tail as droppable
    words = (name or "").replace("(", " ").replace(")", " ").split()
    kept = [w for w in words if w.lower().strip(".") not in _LEGAL_TOKENS]
    # drop a trailing multi-hyphen product descriptor like 'Fenster-Türen-Tore-Wintergärten'
    kept = [w for w in kept if not (w.count("-") >= 2)]
    term = " ".join(kept).strip()
    return term or (name or "").strip()


def _parse_date(value) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.utcfromtimestamp(value).date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _num_or_bound(value):
    """Handles fields that may be a plain number OR a {lower_bound, upper_bound} range
    (Meta discloses spend/reach as ranges for regulated ad categories)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        lo, hi = value.get("lower_bound"), value.get("upper_bound")
        if lo is not None and hi is not None:
            return (float(lo) + float(hi)) / 2
        if lo is not None:
            return float(lo)
        if hi is not None:
            return float(hi)
    return None


def _is_actor_error(item: dict) -> bool:
    """The Apify Meta actor emits a sentinel record for a page/search with no
    matching ads, e.g. {"error": "Ads not found", "errorCode": "ADS_NOT_FOUND",
    "url": ...}. It carries no ad identity (no id / ad_archive_id / snapshot /
    page_id) and MUST NOT be mapped into a RawAd — otherwise a page with zero
    active ads gets stored as one phantom active ad (empty text, no id, no date),
    inflating the active-ad count and the divergence score."""
    if not isinstance(item, dict):
        return True
    if item.get("errorCode"):
        return True
    if item.get("error") and not (item.get("id") or item.get("ad_archive_id")
                                   or item.get("ad_id") or item.get("snapshot")
                                   or item.get("page_id")):
        return True
    return False


def build_ads_library_url(name: str | None = None, page_id: str | None = None,
                          country: str = "DE", active_status: str = "all") -> str:
    """Builds a real facebook.com/ads/library/ URL — either a page view (page_id known)
    or a keyword search (page_id unknown yet)."""
    params = {"active_status": active_status, "ad_type": "all", "country": country, "media_type": "all"}
    if page_id:
        params.update({"view_all_page_id": page_id, "search_type": "page"})
    else:
        params.update({"q": name or "", "search_type": "keyword_unordered"})
    return "https://www.facebook.com/ads/library/?" + urlencode(params, quote_via=quote_plus)


class MetaAdSource(AdSource):
    name = "meta"

    def __init__(self, backend: str | None = None):
        self.backend = (backend or config.LIVE_SOURCE).strip().lower()
        if self.backend == "apify":
            self.token = config.APIFY_API_TOKEN
            self.actor_id = _normalize_actor_id(config.APIFY_ACTOR_ID)
            if not self.token:
                raise RuntimeError("APIFY_API_TOKEN is not set in .env")
            if not self.actor_id:
                raise RuntimeError("APIFY_ACTOR_ID is not set in .env")

    # ---------------- Apify low-level calls ---------------------------------
    def _run_actor(self, payload: dict) -> list[dict]:
        r = requests.post(f"{APIFY_BASE}/acts/{self.actor_id}/runs",
                          params={"token": self.token}, json=payload, timeout=60)
        if r.status_code >= 400:
            body = r.text[:400]
            # Monthly usage / hard limit (402 payment-required or 403 with a
            # limit message) -> a batch-fatal condition, not a per-company error.
            if r.status_code in (402, 403) and re.search(
                    r"usage|hard limit|monthly limit|quota|exceeded", body, re.I):
                raise ApifyQuotaError(body)
            raise RuntimeError(f"Apify run failed to start ({r.status_code}): {body}")
        run = r.json().get("data", {})
        run_id = run.get("id")
        status = run.get("status")

        deadline = time.time() + config.APIFY_RUN_TIMEOUT_SECONDS
        while status not in TERMINAL_STATES and time.time() < deadline:
            time.sleep(config.APIFY_POLL_INTERVAL_SECONDS)
            rr = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": self.token}, timeout=30)
            rr.raise_for_status()
            run = rr.json().get("data", {})
            status = run.get("status")

        if status != "SUCCEEDED":
            raise RuntimeError(f"Apify run ended with status={status or 'TIMEOUT'} (run_id={run_id})")

        dataset_id = run.get("defaultDatasetId")
        items_r = requests.get(f"{APIFY_BASE}/datasets/{dataset_id}/items",
                               params={"token": self.token, "clean": "true"}, timeout=60)
        items_r.raise_for_status()
        return items_r.json()

    def _searchapi_get(self, params: dict) -> dict:
        headers = {"Authorization": f"Bearer {config.SEARCHAPI_KEY}"}
        resp = requests.get(config.SEARCHAPI_URL, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()

    # ---------------- field mapping (see Ad.source_raw for the untouched item) --
    @staticmethod
    def _map_ad(item: dict) -> RawAd:
        snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        body = snapshot.get("body") if isinstance(snapshot.get("body"), dict) else {}
        ad_text = (
            item.get("ad_text") or item.get("body")
            or (body.get("text") if isinstance(body, dict) else None)
            or snapshot.get("title") or item.get("title")
        )
        cta = item.get("cta_text") or snapshot.get("cta_text") or item.get("cta")

        # Dynamic catalog ads (media_type "DCO") store an unresolved merge-tag
        # template at the top level, e.g. "{{product.brand}}" — Meta fills that in
        # per-viewer from a product feed, so the archive never resolves it. The
        # actual human-written copy shown to viewers lives in the first carousel
        # card instead.
        cards = snapshot.get("cards")
        first_card = cards[0] if isinstance(cards, list) and cards and isinstance(cards[0], dict) else None
        landing_url = snapshot.get("link_url")
        if first_card:
            if not ad_text or "{{" in str(ad_text):
                ad_text = first_card.get("body") or first_card.get("title") or ad_text
            if not cta or "{{" in str(cta):
                cta = first_card.get("cta_text") or cta
            if not landing_url:
                landing_url = first_card.get("link_url")

        start = item.get("start_date") or item.get("ad_delivery_start_time") or snapshot.get("start_date")
        end = item.get("end_date") or item.get("ad_delivery_stop_time") or snapshot.get("end_date")
        media_type = snapshot.get("display_format") or item.get("display_format") or item.get("media_type")
        reach = _num_or_bound(item.get("reach_estimate") or item.get("eu_total_reach") or item.get("impressions_with_index"))
        spend = _num_or_bound(item.get("spend"))
        ad_archive_id = item.get("ad_archive_id") or item.get("id") or item.get("ad_id")
        ad_library_url = (item.get("ad_library_url")
                          or (f"https://www.facebook.com/ads/library/?id={ad_archive_id}"
                              if ad_archive_id else None))

        return RawAd(
            external_ad_id=ad_archive_id,
            ad_text=ad_text,
            cta=cta,
            start_date=_parse_date(start),
            end_date=_parse_date(end),
            is_active=bool(item.get("is_active", True)),
            media_type=media_type,
            reach=int(reach) if reach is not None else None,
            real_spend=spend,
            country=item.get("country", config.DEFAULT_COUNTRY),
            ad_library_url=ad_library_url,
            landing_url=landing_url,
            source_raw=item,
        )

    # ---------------- combined search + resolve (Meta-specific) -------------
    def search_and_resolve(self, name: str, country: str = "DE", max_ads: int = 200,
                           website_domain: str | None = None) -> dict:
        """One Apify run: search by company name, group ads by page, pick the best
        match. Returns dict(status, page_id, page_name, ads, candidates).

        Name similarity alone is unreliable — political pages run huge ad
        volumes and often share a common surname token with a company name, so
        a politician can easily out-score the real page on string similarity.
        Two corroborating signals fix that: `website_domain`, if given, is
        matched against each candidate's ad landing URLs (a page whose ads
        link to the company's own site is near-certain — the same technique
        `partner_linker.py` uses for partner-account discovery), and a page
        category blocklist (`_BLOCKED_CATEGORIES`) vetoes auto-confirming
        obviously-non-business pages regardless of how well the name matches."""
        # Resolve by keyword search over ALL statuses (an inactive-only page still
        # tells us the name maps to a real page). Formal names are too strict for the
        # actor's keyword_unordered search, so search the cleaned term; if that finds
        # nothing, fall back once to the distinctive leading token(s).
        term = search_term(name)
        pages = self._search_pages(term, country, max_ads)
        if not pages:
            head = " ".join(term.split()[:2]) or term
            if head and head != term:
                pages = self._search_pages(head, country, max_ads)

        if not pages:
            return {"status": "no_ads_found", "page_id": None, "page_name": None,
                    "ads": [], "candidates": [], "search_term": term}

        target = _clean_name_for_similarity(name)
        company_domain = _registered_domain(website_domain)

        def score(info: dict) -> dict:
            sim = fuzz.token_set_ratio(target, _clean_name_for_similarity(info["name"])) / 100
            site_match = company_domain is not None and any(
                _registered_domain(ad.landing_url) == company_domain
                for ad in info["ads"] if ad.landing_url
            )
            return {**info, "sim": sim, "site_match": site_match,
                    "blocked": _category_is_blocked(info.get("category"))}

        scored = sorted(
            (score(info) for info in pages.values()),
            key=lambda x: (x["site_match"], not x["blocked"], x["sim"], len(x["ads"])),
            reverse=True,
        )
        candidates = [{
            "page_id": c["page_id"], "name": c["name"], "ad_count": len(c["ads"]),
            "active_ad_count": sum(1 for a in c["ads"] if a.is_active),
            "category": c.get("category"), "profile_uri": c.get("profile_uri"),
            "similarity": round(c["sim"], 2), "site_match": c["site_match"], "blocked": c["blocked"],
        } for c in scored[:8]]
        best = scored[0]

        if best["blocked"]:
            # A politician/public-figure/etc. page — never auto-confirm, even if
            # it's the only or best-scoring result. Flag for human review instead.
            status = "ambiguous"
        elif best["site_match"]:
            # Its ads link to the company's own website — stronger evidence than
            # name similarity can ever provide, so this alone is enough to confirm.
            status = "confirmed"
        elif len(scored) == 1:
            status = "confirmed" if best["sim"] >= SIM_CONFIRM_SINGLE else "ambiguous"
        else:
            margin = best["sim"] - scored[1]["sim"]
            status = "confirmed" if (best["sim"] >= SIM_CONFIRM and margin >= SIM_MARGIN) else "ambiguous"

        active_ads = [a for a in best["ads"] if a.is_active]
        return {"status": status, "page_id": best["page_id"], "page_name": best["name"],
               "ads": active_ads, "candidates": candidates, "search_term": term}

    def _search_pages(self, term: str, country: str, max_ads: int) -> dict[str, dict]:
        """Run one keyword search and group returned ads by their Facebook page."""
        url = build_ads_library_url(name=term, country=country, active_status="all")
        items = self._run_items(url, max_ads, active_status="all", country=country)
        pages: dict[str, dict] = {}
        for item in items:
            pid = str(item.get("page_id") or "")
            if not pid:
                continue
            snap = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
            pname = item.get("page_name") or snap.get("page_name") or ""
            bucket = pages.get(pid)
            if bucket is None:
                cats = snap.get("page_categories") or []
                bucket = pages[pid] = {
                    "page_id": pid, "name": pname, "ads": [],
                    "category": cats[0] if cats else None,
                    "profile_uri": snap.get("page_profile_uri"),
                }
            bucket["ads"].append(self._map_ad(item))
        return pages

    def _run_items(self, url: str, max_ads: int, active_status: str = "all",
                   country: str = "DE") -> list[dict]:
        if self.backend == "apify":
            # The actor parses filters from the library URL, but the documented
            # top-level fields are passed too so behaviour is explicit and stable.
            payload = {
                "urls": [{"url": url}],
                "count": max_ads,
                "scrapePageAds.activeStatus": active_status,
                "scrapePageAds.countryCode": country or "ALL",
            }
            items = self._run_actor(payload)
        elif self.backend == "searchapi":
            # NOTE: SearchAPI's meta_ad_library engine takes q= / page_id= directly rather
            # than a raw library URL; this path is a lower-priority alternate backend.
            data = self._searchapi_get({"engine": "meta_ad_library", "q": url, "country": "ALL"})
            items = data.get("ads") or data.get("results") or []
        else:
            raise ValueError(f"Unknown backend: {self.backend}")
        # Drop the actor's "no ads" sentinel record so it never becomes a phantom
        # ad. A genuinely empty page then yields [] → run status "no_active_ads".
        return [it for it in items if not _is_actor_error(it)]

    # ---------------- AdSource interface ------------------------------------
    def resolve_company(self, name: str, country: str = "DE") -> list[PageCandidate]:
        result = self.search_and_resolve(name, country=country)
        return [PageCandidate(page_id=c["page_id"], name=c["name"], has_any_ads=c["ad_count"] > 0)
                for c in result["candidates"]]

    def fetch_ads(self, page_id: str, country: str = "DE", active_only: bool = True) -> list[RawAd]:
        active_status = "active" if active_only else "all"
        url = build_ads_library_url(page_id=page_id, country=country, active_status=active_status)
        items = self._run_items(url, max_ads=500, active_status=active_status, country=country)
        ads = [self._map_ad(it) for it in items]
        return [a for a in ads if a.is_active] if active_only else ads

    def sweep_hub(self, term: str, country: str = "DE", max_ads: int = 300) -> list[dict]:
        """One keyword search over ACTIVE ads for the partner-hub term (e.g. 'Solarlux').

        Returns RAW items (dicts) — the partner linker needs the untouched
        link_url / utm evidence, and groups by page itself."""
        url = build_ads_library_url(name=term, country=country, active_status="active")
        return self._run_items(url, max_ads, active_status="active", country=country)
