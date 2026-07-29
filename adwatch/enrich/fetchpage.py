"""Fetch a company's own site once, for BOTH ownership validation and fact
extraction — one crawl, two consumers, so enrichment never fetches twice.

Why this exists next to identity/website_source.py rather than in it: the
identity crawler only needs a small homepage excerpt (1,200 chars) to spot a
Facebook link. Enrichment needs much more, and specifically needs the
**Impressum/Kontakt** text, because German law puts the phone number and postal
address there — measured on this dataset, including it lifted hard phone-match
validation from 7/18 to 12/18. The low-level fetch/extract helpers are reused
from website_source so there is still only one HTTP/HTML implementation.
"""
from __future__ import annotations

from ..identity import website_source as ws
from .domains import normalize_domain

# Per-page and total text budgets. ~9k chars is plenty for the LLM stage while
# keeping the per-company token cost (and bill) predictable.
_PER_PAGE_CHARS = 4000
_TOTAL_CHARS = 9000


def page_bundle(domain: str | None, total_chars: int = _TOTAL_CHARS) -> dict | None:
    """Homepage + up to 2 of the site's own Impressum/Kontakt pages.

    Returns {domain, home_url, text, pages, chars} or None when the site can't
    be reached at all. `text` is the combined excerpt used by validate.py and
    extract.py. Never raises — an unreachable or broken site is a normal outcome
    (the caller records it as needs_review / no_website_found)."""
    dom = normalize_domain(domain)
    if not dom:
        return None
    try:
        got = ws._fetch_url(f"https://{dom}") or ws._fetch_url(f"http://{dom}")
    except Exception:  # noqa: BLE001 — network failure is an expected outcome here
        got = None
    if not got:
        return None
    html, home_url = got

    chunks = [ws._page_text(html, limit=_PER_PAGE_CHARS)]
    pages = [home_url]
    try:
        for sub_url in ws._subpage_urls(home_url, html):
            sub = ws._fetch_url(sub_url, timeout=10)
            if sub:
                chunks.append(ws._page_text(sub[0], limit=_PER_PAGE_CHARS))
                pages.append(sub_url)
    except Exception:  # noqa: BLE001 — Impressum is a bonus, not a requirement
        pass

    text = " | ".join(c for c in chunks if c)[:total_chars]
    return {"domain": dom, "home_url": home_url, "text": text,
            "pages": pages, "chars": len(text)}
