"""Fetch a company's own site once, for BOTH ownership validation and fact
extraction — one crawl, two consumers, so enrichment never fetches twice.

Why this exists next to identity/website_source.py rather than in it: the
identity crawler only needs a small homepage excerpt (1,200 chars) to spot a
Facebook link. Enrichment needs much more, and specifically needs the
**Impressum/Kontakt** text, because German law puts the phone number and postal
address there — measured on this dataset, including it lifted hard phone-match
validation from 7/18 to 12/18. The HTML/text helpers are reused from
website_source; the raw HTTP layer is NOT — enrichment sweeps thousands of
arbitrary, unvetted domains sequentially, so its fetches are hardened:

  * SSRF guard — the host must resolve to PUBLIC addresses only. Domains come
    from email addresses and search results (attacker-influenceable data), and
    must never let the crawler reach localhost/10.x/192.168.x/link-local.
  * Size + wall-clock caps — a server that serves a huge body, or drips bytes
    forever, must cost at most ~1.5 MB / ~25 s, not hang the whole job (the
    read-timeout alone does NOT bound total duration, only inter-byte gaps).
  * robots.txt respected (fail-open: an unreachable/missing robots.txt allows
    the crawl — it's a politeness measure, not a security boundary).
  * A short pause between pages of the same site.
"""
from __future__ import annotations

import ipaddress
import socket
import time
import urllib.robotparser

import requests

from ..identity import website_source as ws
from .domains import normalize_domain

# Per-page and total text budgets. ~9k chars is plenty for the LLM stage while
# keeping the per-company token cost (and bill) predictable.
_PER_PAGE_CHARS = 4000
_TOTAL_CHARS = 9000

_MAX_BYTES = 1_500_000       # hard cap per fetched page
_CONNECT_TIMEOUT = 6         # seconds to establish the connection
_READ_TIMEOUT = 12           # seconds of silence between bytes
_WALL_CLOCK_CAP = 25         # seconds total per page, even if bytes keep coming
_PAGE_PAUSE = 0.4            # politeness pause between pages of one site


def _host_is_public(host: str) -> bool:
    """True only when EVERY address the host resolves to is globally routable.
    Fails closed: unresolvable (or internal-only) hosts are not fetched."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    try:
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
    except ValueError:
        return False
    return True


def _fetch(url: str, wall_clock: float = _WALL_CLOCK_CAP) -> tuple[str, str] | None:
    """GET with size AND total-duration caps. Returns (text, final_url) or None.
    Never raises — an unreachable/slow/oversized page is a normal outcome."""
    started = time.monotonic()
    try:
        with requests.get(url, headers={"User-Agent": ws._UA}, stream=True,
                          timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                          allow_redirects=True) as r:
            if r.status_code >= 400:
                return None
            # only text-bearing responses are worth reading at all
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and not any(t in ctype for t in ("text/", "html", "xml", "json")):
                return None
            chunks: list[bytes] = []
            size = 0
            for chunk in r.iter_content(chunk_size=32_768):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size >= _MAX_BYTES or (time.monotonic() - started) > wall_clock:
                    break   # keep what we have — the page start carries the Impressum links
            enc = r.encoding or "utf-8"
            try:
                text = b"".join(chunks).decode(enc, errors="replace")
            except LookupError:
                text = b"".join(chunks).decode("utf-8", errors="replace")
            return (text, str(r.url)) if text else None
    except requests.RequestException:
        return None
    except Exception:  # noqa: BLE001 — a single weird server must never kill the job
        return None


def _robots_allows(domain: str) -> bool:
    """robots.txt check for our UA on '/'. FAIL-OPEN: no robots.txt, or one we
    can't fetch/parse, allows the crawl — this is politeness, not security."""
    got = _fetch(f"https://{domain}/robots.txt", wall_clock=8) \
        or _fetch(f"http://{domain}/robots.txt", wall_clock=8)
    if not got:
        return True
    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(got[0].splitlines())
        return rp.can_fetch(ws._UA, f"https://{domain}/")
    except Exception:  # noqa: BLE001
        return True


def page_bundle(domain: str | None, total_chars: int = _TOTAL_CHARS) -> dict | None:
    """Homepage + up to 2 of the site's own Impressum/Kontakt pages.

    Returns {domain, home_url, text, pages, chars} or None when the site can't
    (or shouldn't) be fetched: unresolvable, non-public address, robots-
    disallowed, unreachable. Never raises."""
    dom = normalize_domain(domain)
    if not dom:
        return None
    if not _host_is_public(dom):
        return None
    if not _robots_allows(dom):
        return None

    got = _fetch(f"https://{dom}") or _fetch(f"http://{dom}")
    if not got:
        return None
    html, home_url = got

    chunks = [ws._page_text(html, limit=_PER_PAGE_CHARS)]
    pages = [home_url]
    try:
        for sub_url in ws._subpage_urls(home_url, html):
            time.sleep(_PAGE_PAUSE)
            sub = _fetch(sub_url)
            if sub:
                chunks.append(ws._page_text(sub[0], limit=_PER_PAGE_CHARS))
                pages.append(sub_url)
    except Exception:  # noqa: BLE001 — Impressum is a bonus, not a requirement
        pass

    text = " | ".join(c for c in chunks if c)[:total_chars]
    return {"domain": dom, "home_url": home_url, "text": text,
            "pages": pages, "chars": len(text)}
