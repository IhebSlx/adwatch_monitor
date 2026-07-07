"""Meta Ad Library adapter — live via Apify (curious_coder/facebook-ads-library-scraper,
actor id XtaWFhbtfxyzqrFmd), with SearchAPI.io as an optional alternate backend, and an
offline mock generator for testing without burning credits.

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
import time
from difflib import SequenceMatcher
from urllib.parse import quote_plus, urlencode

import requests

from .. import config
from .base import AdSource, PageCandidate, RawAd

APIFY_BASE = "https://api.apify.com/v2"
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "TIMED_OUT", "ABORTED"}

# How confident a single-page or best-vs-runner-up match needs to be to auto-confirm.
SIM_CONFIRM_SINGLE = 0.30   # only one distinct page in results
SIM_CONFIRM = 0.50          # multiple pages: top candidate similarity floor
SIM_MARGIN = 0.15           # top candidate must lead the runner-up by this much


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


def _norm_name(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


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
        self.backend = (backend or (config.LIVE_SOURCE if config.is_live() else "mock")).strip().lower()
        if config.MODE == "mock":
            self.backend = "mock"
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
            raise RuntimeError(f"Apify run failed to start ({r.status_code}): {r.text[:400]}")
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
        start = item.get("start_date") or item.get("ad_delivery_start_time") or snapshot.get("start_date")
        end = item.get("end_date") or item.get("ad_delivery_stop_time") or snapshot.get("end_date")
        media_type = snapshot.get("display_format") or item.get("display_format") or item.get("media_type")
        reach = _num_or_bound(item.get("reach_estimate") or item.get("eu_total_reach") or item.get("impressions_with_index"))
        spend = _num_or_bound(item.get("spend"))

        return RawAd(
            external_ad_id=item.get("ad_archive_id") or item.get("id") or item.get("ad_id"),
            ad_text=ad_text,
            cta=cta,
            start_date=_parse_date(start),
            end_date=_parse_date(end),
            is_active=bool(item.get("is_active", True)),
            media_type=media_type,
            reach=int(reach) if reach is not None else None,
            real_spend=spend,
            country=item.get("country", config.DEFAULT_COUNTRY),
            source_raw=item,
        )

    # ---------------- combined search + resolve (Meta-specific) -------------
    def search_and_resolve(self, name: str, country: str = "DE", max_ads: int = 200) -> dict:
        """One Apify run: search by company name, group ads by page, pick the best
        name match. Returns dict(status, page_id, page_name, ads, candidates)."""
        if self.backend == "mock":
            from ..mockdata import generate_ads
            ads = [a for a in generate_ads(name, country) if a.is_active]
            if not ads:
                return {"status": "no_ads_found", "page_id": None, "page_name": None, "ads": [], "candidates": []}
            key = f"MOCK::{name}"
            return {"status": "confirmed", "page_id": key, "page_name": name, "ads": ads,
                   "candidates": [{"page_id": key, "name": name, "ad_count": len(ads)}]}

        url = build_ads_library_url(name=name, country=country, active_status="all")
        items = self._run_items(url, max_ads)

        pages: dict[str, dict] = {}
        for item in items:
            pid = str(item.get("page_id") or "")
            if not pid:
                continue
            pname = item.get("page_name") or (item.get("snapshot") or {}).get("page_name") or ""
            bucket = pages.setdefault(pid, {"page_id": pid, "name": pname, "ads": []})
            bucket["ads"].append(self._map_ad(item))

        if not pages:
            return {"status": "no_ads_found", "page_id": None, "page_name": None, "ads": [], "candidates": []}

        target = _norm_name(name)
        scored = sorted(
            ({**info, "sim": SequenceMatcher(None, target, _norm_name(info["name"])).ratio()}
             for info in pages.values()),
            key=lambda x: (-x["sim"], -len(x["ads"])),
        )
        candidates = [{"page_id": c["page_id"], "name": c["name"], "ad_count": len(c["ads"])} for c in scored[:5]]
        best = scored[0]

        if len(scored) == 1:
            status = "confirmed" if best["sim"] >= SIM_CONFIRM_SINGLE else "ambiguous"
        else:
            margin = best["sim"] - scored[1]["sim"]
            status = "confirmed" if (best["sim"] >= SIM_CONFIRM and margin >= SIM_MARGIN) else "ambiguous"

        active_ads = [a for a in best["ads"] if a.is_active]
        return {"status": status, "page_id": best["page_id"], "page_name": best["name"],
               "ads": active_ads, "candidates": candidates}

    def _run_items(self, url: str, max_ads: int) -> list[dict]:
        if self.backend == "apify":
            return self._run_actor({"urls": [{"url": url}], "count": max_ads})
        if self.backend == "searchapi":
            # NOTE: SearchAPI's meta_ad_library engine takes q= / page_id= directly rather
            # than a raw library URL; this path is a lower-priority alternate backend.
            data = self._searchapi_get({"engine": "meta_ad_library", "q": url, "country": "ALL"})
            return data.get("ads") or data.get("results") or []
        raise ValueError(f"Unknown backend: {self.backend}")

    # ---------------- AdSource interface ------------------------------------
    def resolve_company(self, name: str, country: str = "DE") -> list[PageCandidate]:
        if self.backend == "mock":
            return [PageCandidate(page_id=f"MOCK::{name}", name=name, has_any_ads=True)]
        result = self.search_and_resolve(name, country=country)
        return [PageCandidate(page_id=c["page_id"], name=c["name"], has_any_ads=c["ad_count"] > 0)
                for c in result["candidates"]]

    def fetch_ads(self, page_id: str, country: str = "DE", active_only: bool = True) -> list[RawAd]:
        if self.backend == "mock":
            from ..mockdata import generate_ads
            name = page_id.split("::", 1)[1] if page_id.startswith("MOCK::") else page_id
            ads = generate_ads(name, country)
            return [a for a in ads if a.is_active] if active_only else ads

        url = build_ads_library_url(page_id=page_id, country=country,
                                    active_status="active" if active_only else "all")
        items = self._run_items(url, max_ads=500)
        ads = [self._map_ad(it) for it in items]
        return [a for a in ads if a.is_active] if active_only else ads
