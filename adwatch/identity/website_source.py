"""ONLINE IDENTITY — read the company's OWN website for its Meta pages.

The single most authoritative identity signal there is: a facebook.com /
instagram.com link in the company's own site is the company declaring its own
page. No name search, no ambiguity, no API cost, no LLM — and it's exactly
what fixes the "matched a politician / a namesake" failure, because we never
search by name at all.

Coverage (each step free, just HTTP):
  • the ENTIRE homepage HTML is scanned — not just <a href> attributes — so
    links inside JSON-LD `sameAs` blocks and script bundles count too
  • when the homepage is inconclusive, the Impressum/Kontakt pages are fetched
    as well (German companies are legally required to have an Impressum, and
    social links very often live only there)
  • the homepage's title/description/visible text is extracted once and fed to
    the LLM stages of the serper tier (judge + keyword generator)

Runs FIRST in resolver.run_identity_check whenever a website_domain exists;
only when the site is unreachable, links nothing usable, or links SEVERAL
different pages (ambiguous) do we fall back to the serper search.

resolve_from_website() returns the same dict shape as
serper_source.resolve_identity(), plus:
  • source="website"
  • lock=True  when the match is authoritative AND fetch-ready (a numeric page
    id) — the caller then hard-locks it. A handle-only match is confirmed but
    NOT locked, so a later ad lookup can still enrich the numeric id.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin, urlsplit

import requests

from ..collect.meta_source import _registered_domain
from .serper_source import (canonicalize_fb, canonicalize_ig, _page_key,
                            _profile_url, _umlaut_ascii)

# Social links that belong to the manufacturer / a partner / a platform widget
# rather than the partner company itself — never treat these as the match.
# Includes website-builder/hoster brands: their templates embed THEIR OWN
# social links in the page source (found live: a Wix site confirming to
# facebook.com/wix), which the whole-HTML scan would otherwise pick up.
_HANDLE_BLOCKLIST = {
    "solarlux", "solarluxgmbh", "solarluxgroup",
    "facebook", "instagram", "meta", "profile.php",
    "wix", "wixcom", "jimdo", "squarespace", "wordpress", "wordpressdotcom",
    "weebly", "shopify", "webnode", "ionos", "strato", "godaddy", "duda",
    "webflow", "webador", "hostinger", "one.com", "onecom", "wixstudio",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# facebook.com / instagram.com URLs ANYWHERE in the raw HTML (href attributes,
# JSON-LD sameAs, script bundles). The netloc part deliberately excludes API
# hosts like graph.facebook.com / connect.facebook.net.
_SOCIAL_URL_RE = re.compile(
    r"(?:https?:)?//(?:www\.|m\.|[a-z]{2}(?:-[a-z]{2})?\.)?"
    r"(?:facebook\.com|instagram\.com)/[^\s\"'<>\\]+", re.I)

# hrefs that lead to the site's own Impressum/Kontakt page
_SUBPAGE_RE = re.compile(
    r'''href\s*=\s*["']([^"']*(?:impressum|kontakt|contact)[^"']*)["']''', re.I)

# ---------------------------------------------------------------------------
# Subpage selection
# ---------------------------------------------------------------------------
# Two different consumers want two different things from a company's own site:
#
#   identity   the LEGAL page — German law (and the ES/FR/IT equivalents) put the
#              phone number and postal address there, which is what proves the
#              site belongs to this company.
#   enrichment the PRODUCT and ABOUT pages — what the firm actually sells, since
#              a homepage is usually a slogan and a hero image.
#
# The old rule served only the first: three German/English keywords, then the
# first two matches in document order. On a Spanish site that reads the homepage
# and 'contacto' (which matched only by accident, being a superstring of
# 'contact') and never sees 'productos' — so the products list came from whatever
# the homepage happened to mention. `aviso legal`, the actual Spanish Impressum,
# did not match at all, even though config/markets.yaml already knows the term.
#
# Keywords are matched against the URL path, ascii-folded, so 'mentions-légales'
# and 'quiénes-somos' hit regardless of accents.
_LINK_CATEGORIES: dict[str, tuple[str, ...]] = {
    # strongest identity evidence (phone + postal address)
    "legal": (
        "impressum", "kontakt", "contact", "contacto", "contactos", "contatti",
        "aviso-legal", "avisolegal", "aviso_legal", "mentions-legales",
        "mentionslegales", "legal-notice", "legalnotice", "note-legali",
        "chi-siamo-contatti", "imprint", "colofon", "wettelijke",
    ),
    # what they sell — the highest-value pages for the ICP
    "products": (
        "produkt", "produkte", "leistungen", "sortiment", "katalog",
        "producto", "productos", "servicio", "servicios", "catalogo",
        "prodotti", "servizi", "produits", "produtos", "servicos",
        "product", "products", "services", "solutions", "soluciones",
        "sistemas", "systeme", "gama",
    ),
    # who they are — description, founding year, size, certifications
    "about": (
        "ueber-uns", "uber-uns", "ueberuns", "unternehmen", "firma", "profil",
        "about", "about-us", "aboutus", "empresa", "quienes-somos",
        "quienessomos", "sobre-nosotros", "nosotros", "conocenos",
        "chi-siamo", "chisiamo", "a-propos", "apropos", "qui-sommes-nous",
        "sobre-nos", "historia", "geschichte", "team", "wij", "over-ons",
    ),
    # proof of what they build — often the richest product evidence of all
    "references": (
        "referenzen", "projekte", "projects", "proyectos", "obras",
        "realizaciones", "progetti", "realisations", "realisaties",
        "galeria", "galerie", "gallery",
    ),
}

# How many subpages to take per category, and in which priority order. `legal`
# is capped at 1: a second contact page adds nothing, and spending a slot on it
# is exactly what starved the product pages before.
_CATEGORY_BUDGET: tuple[tuple[str, int], ...] = (
    ("legal", 1), ("products", 2), ("about", 1), ("references", 1),
)


def _ascii_lower(s: str) -> str:
    return _umlaut_ascii((s or "").lower())


def _classify_link(url: str) -> str | None:
    """Which category a URL's PATH belongs to, or None.

    Only the path is inspected — a query string like '?ref=contact' says nothing
    about the destination, and the host is already known to be this company's.
    Longest keyword wins so 'aviso-legal' is not shadowed by a shorter match.
    """
    try:
        path = _ascii_lower(urlsplit(url).path)
    except ValueError:
        return None
    if not path or path == "/":
        return None
    best: tuple[int, str] | None = None
    for cat, words in _LINK_CATEGORIES.items():
        for w in words:
            if w in path and (best is None or len(w) > best[0]):
                best = (len(w), cat)
    return best[1] if best else None

_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _fetch_url(url: str, timeout: int = 15) -> tuple[str, str] | None:
    """Best-effort GET. Returns (html, final_url_after_redirects) or None —
    a slow or dead page just falls back to the next tier."""
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout,
                         allow_redirects=True)
        if r.status_code < 400 and r.text:
            return r.text, str(r.url)
    except requests.RequestException:
        pass
    return None


# Site chrome that carries no facts about the company, only link labels.
# <footer> is deliberately NOT in here: it is where phone numbers and postal
# addresses live, which is exactly what the identity check matches on.
_CHROME_RE = re.compile(r"<(nav|header)\b[^>]*>.*?</\1>", re.S | re.I)
_MENU_RE = re.compile(
    r"""<(ul|div)\b[^>]*(?:class|id|role)\s*=\s*["'][^"']*(?:menu|nav|breadcrumb)"""
    r"""[^"']*["'][^>]*>.*?</\1>""", re.S | re.I)
# Below this, stripping has clearly eaten the content rather than the chrome
# (single-page sites whose whole body sits inside <header>), so we keep the
# original text instead — a flooded extraction still beats an empty one.
_CHROME_KEEP_RATIO = 0.10


def _page_text(html: str, limit: int = 1200, drop_chrome: bool = False) -> str:
    """Title + meta description + the first visible text of the page — a
    compact self-description of the company for the LLM stages.

    `drop_chrome` removes navigation menus first. Big WordPress/Elementor sites
    open with a mega-menu, and since this keeps text in document order, those
    link labels ate the whole character budget before any prose was reached:
    TYPSA handed the extractor 9.000 characters of "Carreteras · Ferrocarriles ·
    Aeropuertos …" and nothing else, so every field came back null and the
    company scored no relevance at all. 10 of 72 Spanish architects failed this
    way — none of them for want of text.

    Off by default because the identity check matches on phone and postcode,
    which sit in the footer and header of exactly the small sites that have no
    menu problem. Only the enrichment path asks for it.
    """
    if not html:
        return ""
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)',
                     html, re.I)
    source = html
    if drop_chrome:
        stripped = _MENU_RE.sub(" ", _CHROME_RE.sub(" ", html))
        if len(_squash(stripped)) >= _CHROME_KEEP_RATIO * max(len(_squash(html)), 1):
            source = stripped
    body = _squash(source)
    head = " · ".join(x.group(1).strip() for x in (title, desc) if x and x.group(1).strip())
    return unescape(f"{head} — {body}" if head else body)[:limit]


def _squash(html: str) -> str:
    """Visible text of an HTML fragment, whitespace-collapsed."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", html))).strip()


def _own_links(base_url: str, html: str) -> list[str]:
    """Every on-site absolute URL in the page, deduped, in document order."""
    out, seen = [], set()
    base_dom = _registered_domain(base_url)
    for href in re.findall(r'''href\s*=\s*["']([^"']+)["']''', html or ""):
        href = href.strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:")) \
                or href.startswith("#"):
            continue
        full = urljoin(base_url, href).split("#")[0].rstrip("/")
        if not full.lower().startswith(("http://", "https://")):
            continue
        if _registered_domain(full) != base_dom:
            continue  # off-site link, not this company's page
        # Nav bars repeat the same target (desktop + mobile menu); a duplicate
        # used to consume one of only two slots.
        key = _ascii_lower(urlsplit(full).path).rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(full)
    return out


def _subpage_urls(base_url: str, html: str, max_pages: int = 2,
                  budget: tuple[tuple[str, int], ...] | None = None) -> list[str]:
    """The most informative subpages of the site, best first.

    Chosen by CATEGORY rather than document order, so one legal page (identity
    evidence) is fetched alongside the product/about pages that say what the firm
    actually sells. Within a category the shortest path wins: '/productos' is the
    section index, '/productos/ventanas/pvc/serie-70' is one item.

    `max_pages` defaults to 2 to preserve the previous cost for existing callers;
    enrichment asks for more.
    """
    ranked: dict[str, list[str]] = {}
    for url in _own_links(base_url, html):
        cat = _classify_link(url)
        if cat:
            ranked.setdefault(cat, []).append(url)
    for urls in ranked.values():
        urls.sort(key=lambda u: (len(urlsplit(u).path.rstrip("/")), u))

    out: list[str] = []
    for cat, take in (budget or _CATEGORY_BUDGET):
        for url in ranked.get(cat, [])[:take]:
            if url not in out:
                out.append(url)
            if len(out) >= max_pages:
                return out
    return out[:max_pages]


def crawl_site(website_domain: str) -> dict | None:
    """Fetch the homepage (https, then http). Returns
    {home_html, home_url, sub_htmls (lazy), text} or None when unreachable.
    `text` is the excerpt that feeds the LLM stages of the serper tier."""
    raw = (website_domain or "").strip()
    if not raw:
        return None
    urls = [raw] if raw.startswith(("http://", "https://")) \
        else [f"https://{raw}", f"http://{raw}"]
    for url in urls:
        got = _fetch_url(url)
        if got:
            html, final_url = got
            return {"home_html": html, "home_url": final_url,
                    "sub_htmls": None, "text": _page_text(html)}
    return None


def _ensure_subpages(crawl: dict) -> list[str]:
    """Fetch the Impressum/Kontakt pages once, lazily, caching on the crawl."""
    if crawl.get("sub_htmls") is None:
        crawl["sub_htmls"] = []
        for sub_url in _subpage_urls(crawl["home_url"], crawl["home_html"]):
            got = _fetch_url(sub_url, timeout=10)
            if got:
                crawl["sub_htmls"].append(got[0])
    return crawl["sub_htmls"]


def _collect_links(html: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Extract the distinct Facebook and Instagram pages referenced anywhere in
    the page source, dropping widgets/manufacturer links. Returns (facebook,
    instagram) dicts keyed by page — a record carrying a numeric page id wins."""
    fb: dict[str, dict] = {}
    ig: dict[str, dict] = {}
    # JSON blocks escape slashes (https:\/\/...) — normalize before scanning
    text = (html or "").replace("\\/", "/").replace("&amp;", "&")
    for url in _SOCIAL_URL_RE.findall(text):
        url = url.rstrip(".,;:)'\"")
        canon = canonicalize_fb(url) or canonicalize_ig(url)
        if not canon:
            continue
        handle = (canon.get("handle") or "").lower().strip(".")
        if handle in _HANDLE_BLOCKLIST:
            continue
        if handle.isdigit():
            continue  # xmlns/api artifacts like facebook.com/2008/fbml
        key = _page_key(canon)
        if not key:
            continue
        bucket = fb if canon["platform"] == "facebook" else ig
        prev = bucket.get(key)
        if prev is None or (not prev.get("page_id") and canon.get("page_id")):
            bucket[key] = canon
    return fb, ig


def _merge_bucket(dst: dict, src: dict) -> None:
    for key, canon in src.items():
        prev = dst.get(key)
        if prev is None or (not prev.get("page_id") and canon.get("page_id")):
            dst[key] = canon


def resolve_from_website(website_domain: str, country: str = "DE",
                         crawl: dict | None = None) -> dict | None:
    """Read the company's own site and return an identity result IF a single,
    unambiguous Facebook (preferred) or Instagram page is linked. Returns None
    when the site is unreachable, links nothing usable, or is ambiguous (several
    distinct pages on the chosen platform) — the caller falls back to serper.

    Pass a `crawl` from crawl_site() to reuse an already-fetched homepage."""
    crawl = crawl or crawl_site(website_domain)
    if not crawl:
        return None

    fb, ig = _collect_links(crawl["home_html"])
    # Homepage inconclusive → Impressum/Kontakt often carry the links (and only
    # then do we spend the extra fetches; a clear homepage answer skips them).
    if len(fb) != 1:
        for html in _ensure_subpages(crawl):
            f2, g2 = _collect_links(html)
            _merge_bucket(fb, f2)
            _merge_bucket(ig, g2)

    if len(fb) == 1:
        chosen, platform = next(iter(fb.values())), "facebook"
    elif not fb and len(ig) == 1:
        chosen, platform = next(iter(ig.values())), "instagram"
    else:
        return None   # 0 usable links, or >1 distinct pages → let serper/human decide

    page_id = chosen.get("page_id")
    handle = chosen.get("handle")
    if not page_id and platform == "facebook" and handle:
        # the URL didn't expose the numeric id — the embed endpoint usually
        # does, turning this into a fetch-ready (lockable) identity for free
        from .page_id_lookup import fb_page_id_from_handle
        page_id = fb_page_id_from_handle(handle)
    profile = _profile_url(chosen)
    cand = {
        "platform": platform, "page_id": page_id, "handle": handle,
        "name": handle, "profile_uri": profile,
        "similarity": 1.0, "site_match": True, "city_match": False,
        "snippet": "Linked from the company's own website.",
        "category": None, "blocked": False, "ad_count": 0, "active_ad_count": 0,
    }
    return {
        "status": "confirmed",
        "page_id": page_id,
        "page_name": handle,
        "page_url": profile,
        "platform": platform,
        "ads": [],
        "candidates": [cand],
        "search_term": website_domain,
        "source": "website",
        # Only a numeric page id is fetch-ready and safe to hard-lock. A
        # handle-only match is trusted+confirmed but left re-checkable so a
        # later ad lookup can enrich the numeric id.
        "lock": bool(page_id),
    }
