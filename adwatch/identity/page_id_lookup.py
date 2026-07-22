"""Resolve a Facebook vanity handle to the NUMERIC page id — free, no login.

The numeric id is what the Meta Ad Library (and the Apify fetch) needs; a
vanity handle alone is "identity known but not fetch-ready". Facebook blocks
anonymous scraping of page HTML, but the official PAGE-PLUGIN embed endpoint
(plugins/page.php — what websites use to embed a page box) renders the public
page header without a login and links the page as

    https://www.facebook.com/<numeric_id>?ref=embed_page

so one HTTPS GET + one regex yields the id. Verified against pages with known
ids (exact match) and nonexistent pages (clean None).

Best-effort by design: any failure returns None and callers fall back to their
existing paths (Ad Library exact-name search / manual entry).
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import quote

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

_EMBED_ID_RE = re.compile(r"facebook\.com/(\d{8,})\?ref=embed_page")


def fb_page_id_from_handle(handle: str) -> str | None:
    """Numeric page id for a facebook vanity handle, or None. A handle that is
    already numeric is returned as-is (it IS the id)."""
    handle = (handle or "").strip().strip("/")
    if not handle:
        return None
    if handle.isdigit():
        return handle
    url = ("https://www.facebook.com/plugins/page.php?href="
           + quote(f"https://www.facebook.com/{handle}/", safe="") + "&width=340")
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=15)
        if r.status_code >= 400 or not r.text:
            return None
        ids = _EMBED_ID_RE.findall(r.text)
        # the page's own id appears several times (header link, like button,
        # sharer); take the most frequent to be robust against stray ids
        return Counter(ids).most_common(1)[0][0] if ids else None
    except requests.RequestException:
        return None
