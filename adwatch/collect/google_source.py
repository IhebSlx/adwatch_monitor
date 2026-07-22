"""Google Ads Transparency Center adapter — live via Apify (google-ads-scraper,
actor id in config.GOOGLE_ADS_ACTOR_ID, same Apify account/token as Meta).

KEY DIFFERENCE FROM META: Google's ad transparency data has no keyword/name
search. An advertiser can only be resolved from either its opaque advertiser
ID or its website DOMAIN — confirmed live against the real site: passing
`?domain=<domain>` to the actor auto-resolves the correct advertiser with no
ID needed upfront. So identity resolution here is domain-based, not
name-based, and (unlike Meta) is exact rather than fuzzy — a domain either
resolves to one advertiser or it doesn't; there's no "ambiguous, best guess"
state to handle.

Also unlike Meta, this actor exposes no landing/destination URL per ad, so
the Meta-style partner-hub sweep (attributing ads via a shared landing URL)
has no equivalent here — Google ads are only ever attributed via the
company's own advertiser identity.
"""
from __future__ import annotations

import datetime as dt
import time

import requests

from .. import config
from .base import AdSource, PageCandidate, RawAd

APIFY_BASE = "https://api.apify.com/v2"
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "TIMED_OUT", "ABORTED"}


def _parse_date(value) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _reach_for_country(region_stats, country: str) -> int | None:
    """Midpoint of the disclosed impressions range for the tracked country, if
    present — feeds the same reach-based spend estimate Meta's `reach` drives,
    instead of falling back to the count-based method."""
    if not isinstance(region_stats, list):
        return None
    for r in region_stats:
        if r.get("regionCode") == country:
            imp = r.get("impressions")
            if isinstance(imp, dict) and imp.get("lowerBound") is not None and imp.get("upperBound") is not None:
                return int((imp["lowerBound"] + imp["upperBound"]) / 2)
    return None


class GoogleAdSource(AdSource):
    name = "google"
    backend = "apify"   # always Apify (google-ads-scraper); the pipeline reads this for progress/summary

    def __init__(self):
        self.token = config.APIFY_API_TOKEN
        self.actor_id = config.GOOGLE_ADS_ACTOR_ID
        if not self.token:
            raise RuntimeError("APIFY_API_TOKEN is not set in .env")
        if not self.actor_id:
            raise RuntimeError("GOOGLE_ADS_ACTOR_ID is not set in .env")

    # ---------------- Apify low-level call ----------------------------------
    def _run_actor(self, start_url: str, skip_details: bool = False,
                   results_limit: int | None = None) -> list[dict]:
        payload = {
            "startUrls": [{"url": start_url}],
            "skipDetails": skip_details,
            "shouldDownloadAssets": False,
            "shouldDownloadPreviews": False,
        }
        if results_limit:
            payload["resultsLimit"] = results_limit
        r = requests.post(f"{APIFY_BASE}/acts/{self.actor_id}/runs",
                          params={"token": self.token}, json=payload, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"Apify run failed to start ({r.status_code}): {r.text[:400]}")
        run = r.json().get("data", {})
        run_id, status = run.get("id"), run.get("status")

        deadline = time.time() + config.GOOGLE_APIFY_RUN_TIMEOUT_SECONDS
        while status not in TERMINAL_STATES and time.time() < deadline:
            time.sleep(config.APIFY_POLL_INTERVAL_SECONDS * 2)
            rr = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": self.token}, timeout=30)
            rr.raise_for_status()
            run = rr.json().get("data", {})
            status = run.get("status")

        if status != "SUCCEEDED":
            raise RuntimeError(f"Apify run ended with status={status or 'TIMEOUT'} (run_id={run_id})")

        dataset_id = run.get("defaultDatasetId")
        items_r = requests.get(f"{APIFY_BASE}/datasets/{dataset_id}/items",
                               params={"token": self.token}, timeout=60)
        items_r.raise_for_status()
        return items_r.json()

    @staticmethod
    def _map_ad(item: dict, country: str) -> RawAd:
        variations = item.get("variations") if isinstance(item.get("variations"), list) else []
        first = variations[0] if variations else {}
        ad_text = first.get("text") or first.get("headline") or first.get("description")
        cta = first.get("cta")
        first_shown = _parse_date(item.get("firstShown"))
        last_shown = _parse_date(item.get("lastShown"))
        # Google doesn't flag active/ended explicitly like Meta does — treat an
        # ad as active if it was still being shown in the last few days of the
        # scraped window (the actor's URL defaults to "Last 30 days").
        is_active = bool(last_shown and (dt.date.today() - last_shown).days <= 3)
        return RawAd(
            external_ad_id=item.get("creativeId"),
            ad_text=ad_text,
            cta=cta,
            start_date=first_shown,
            end_date=last_shown,
            is_active=is_active,
            media_type=item.get("format"),
            reach=_reach_for_country(item.get("regionStats"), country),
            real_spend=None,  # Google Ads Transparency never discloses spend
            country=country,
            ad_library_url=item.get("adLibraryUrl"),
            landing_url=None,  # not exposed by this actor — no partner-sweep equivalent for Google
            source_raw=item,
        )

    # ---------------- identity resolution (domain-based, not name-based) ----
    def resolve_company(self, domain: str, country: str = "DE") -> list[PageCandidate]:
        """Resolve a company's Google Ads advertiser from its website domain.
        Unlike Meta's fuzzy name search, this is exact: a domain either maps to
        one advertiser or it doesn't."""
        domain = (domain or "").strip()
        if not domain:
            return []
        url = f"https://adstransparency.google.com/?region={country}&domain={domain}"
        items = self._run_actor(url, skip_details=True, results_limit=5)
        if not items:
            return []
        advertiser_id = items[0].get("advertiserId")
        advertiser_name = items[0].get("advertiserName") or domain
        if not advertiser_id:
            return []
        return [PageCandidate(page_id=advertiser_id, name=advertiser_name,
                              extra={"domain": domain, "resolved_from": "domain_lookup"})]

    def fetch_ads(self, page_id: str, country: str = "DE", active_only: bool = True) -> list[RawAd]:
        url = f"https://adstransparency.google.com/advertiser/{page_id}?region={country}"
        items = self._run_actor(url, skip_details=False)
        ads = [self._map_ad(item, country) for item in items]
        return [a for a in ads if a.is_active] if active_only else ads
