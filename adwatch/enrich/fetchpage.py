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
from . import extract, site_facts
from .domains import normalize_domain

# Per-page and total text budgets. The TOTAL is what sets the extraction bill, so
# it stays fixed; only how it is SPENT changed. Reading 4 shorter pages beats 2
# long ones here: a homepage tail is navigation and footer boilerplate, while the
# first 2,200 chars of '/productos' is the product list itself.
_PER_PAGE_CHARS = 2200
_HOME_CHARS = 2400           # the homepage still gets the largest single share
_TOTAL_CHARS = 9000          # unchanged -> per-company cost unchanged
_MAX_SUBPAGES = 3            # legal (identity) + products + about/references

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
    """Homepage + the site's most informative subpages (legal, products, about).

    Page choice is by CATEGORY, not document order — see
    identity.website_source._subpage_urls. This matters most outside Germany: the
    old keyword set was impressum/kontakt/contact, so a Spanish site yielded the
    homepage plus 'contacto' and the product pages were never read, leaving the
    products list to whatever the homepage happened to mention.

    Returns {domain, home_url, text, pages, chars, categories} or None when the
    site can't (or shouldn't) be fetched: unresolvable, non-public address,
    robots-disallowed, unreachable. Never raises."""
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

    # drop_chrome: the character budget is the scarce resource here, and on
    # menu-heavy sites the navigation would consume all of it before any prose.
    chunks = [ws._page_text(html, limit=_HOME_CHARS, drop_chrome=True)]
    # Kept UNSTRIPPED and UNTRIMMED, for the deterministic brand scan only. The
    # two edits that make the prose extract good — dropping navigation and
    # capping characters — are exactly the two that hide brand names, because a
    # "Marcas" menu and a partner logo strip are chrome by every structural test.
    # Scanning a closed vocabulary costs nothing, so it reads the whole page.
    full = [ws._page_text(html, limit=10 ** 7)]
    pages = [home_url]
    categories = {home_url: "home"}
    # Machine-readable facts, harvested from the SAME html we already hold — no
    # extra request, no LLM. The homepage is parsed first so its values win, but
    # the legal/contact page is where a phone and postal address usually live, so
    # later pages fill whatever is still missing.
    facts = site_facts.extract(html, base_url=home_url)
    try:
        for sub_url in ws._subpage_urls(home_url, html, max_pages=_MAX_SUBPAGES):
            time.sleep(_PAGE_PAUSE)
            sub = _fetch(sub_url)
            if sub:
                chunks.append(ws._page_text(sub[0], limit=_PER_PAGE_CHARS,
                                            drop_chrome=True))
                full.append(ws._page_text(sub[0], limit=10 ** 7))
                pages.append(sub_url)
                categories[sub_url] = ws._classify_link(sub_url) or "other"
                try:
                    more = site_facts.extract(sub[0], base_url=sub_url)
                except Exception:  # noqa: BLE001
                    more = {}
                for k, v in more.items():
                    if k == "social":
                        merged_social = dict(facts.get("social") or {})
                        for net, url in (v or {}).items():
                            merged_social.setdefault(net, url)
                        facts["social"] = merged_social
                    elif k == "sources":
                        facts.setdefault("sources", {}).update(
                            {sk: sv for sk, sv in v.items() if sk not in facts})
                    elif k not in facts:
                        facts[k] = v
    except Exception:  # noqa: BLE001 — a subpage is a bonus, not a requirement
        pass

    text = " | ".join(c for c in chunks if c)[:total_chars]
    brands = extract.scan_brands(" ".join(full))
    return {"domain": dom, "home_url": home_url, "text": text,
            "pages": pages, "chars": len(text), "categories": categories,
            "facts": facts, "brands": brands}
