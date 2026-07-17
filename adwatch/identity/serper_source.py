"""ONLINE IDENTITY CHECK — resolve a company's Meta presence (Facebook page OR
Instagram profile) via Google (serper.dev), WITHOUT touching ads or the Apify quota.

This is the cheap, ads-free counterpart to the Apify resolver in
collect/meta_source.py. It Googles the company, reads the facebook.com /
instagram.com results, and decides which profile is the company's — using
corroborating signals so name similarity alone can never mislead us (the
failure that once matched a company to a politician / to "Alfred Grob"):

  • website-domain match  — the company's own domain appears on the result
  • city match            — the company's city appears in title/snippet
  • name similarity        — rapidfuzz token-set ratio, legal suffixes stripped
  • LLM relevance judge    — with ANTHROPIC_API_KEY set, Claude Haiku reads the
    top candidates (name/platform/snippet) TOGETHER with everything we know
    about the company (master data + its own website text) and picks the one
    that actually IS the company — or rejects them all, in which case NO best
    guess is stored (better an honest "no page found" than an unrelated page).
  • fallback query ladder  — when the standard queries find nothing viable,
    cheap deterministic variants run one by one (drop the city, umlaut-
    normalized ü→ue/ß→ss, brand-core token only, Instagram without city),
    stopping at the first viable candidate.
  • LLM keyword generator  — the stubborn residue gets ONE Haiku call that
    reads the full company profile + website text and proposes the page names
    the business would ACTUALLY use (brand ≠ legal name, owner's name, shop
    name); those phrases are then searched too.

Instagram matters because both platforms are Meta: ads are generic across
FB+IG under the same page identity, and some partners only maintain an
Instagram profile. So when Facebook yields nothing corroborated, one extra
Instagram-scoped query runs and an IG profile can become the main page.

CONSERVATIVE confirm rule: auto-confirm only when the domain matches, or the
LLM picks a candidate that also has city/strong-name corroboration, or
(no LLM) city + strong name. Everything else stays "ambiguous" for a human.

Returns the SAME dict shape as MetaAdSource.search_and_resolve() so the
identity persistence code is backend-agnostic — except `ads` is always empty
(this never looks at ads) and candidates carry platform/city_match/snippet.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import requests
from rapidfuzz import fuzz

from .. import config
from ..collect.meta_source import _clean_name_for_similarity, _registered_domain, search_term

# name confidence needed to confirm on city-match alone (no domain corroboration)
_STRONG_NAME = 0.72
# a candidate worth JUDGING at all — below this (and without site/city
# corroboration) the pool counts as "nothing found" and the fallback ladder runs
_VIABLE_NAME = 0.60


def _umlaut_ascii(s: str) -> str:
    """German sites/pages often spell the name ASCII-style ('Mueller' for
    'Müller') — a query variant in that spelling finds them."""
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"),
                 ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue")):
        s = s.replace(a, b)
    return s


# trade words that don't identify a company — never useful as a brand-core query
_GENERIC_TOKENS = {
    "fenster", "fensterbau", "türen", "tueren", "tore", "bau", "bauelemente",
    "glas", "glaserei", "glasbau", "holz", "holzbau", "metall", "metallbau",
    "tischlerei", "schreinerei", "zimmerei", "wintergarten", "wintergärten",
    "wintergaerten", "rolladen", "rollladen", "rollläden", "sonnenschutz",
    "montage", "technik", "team", "haus", "service", "systeme", "elemente",
}


def _brand_core(term: str) -> str | None:
    """The most distinctive token of the search term — a last-resort query for
    pages named after just the brand ('Fortuna' for 'Fortuna Wintergarten').
    None when the term is a single token already (nothing to reduce)."""
    tokens = [t for t in term.split() if len(t) >= 4]
    distinctive = [t for t in tokens if t.lower() not in _GENERIC_TOKENS]
    pick = distinctive[0] if distinctive else (tokens[0] if tokens else None)
    return pick if pick and pick.lower() != term.lower() else None


def _pool_viable(pages: dict[str, dict]) -> bool:
    """True when at least one candidate is worth judging — corroborated by
    site or city, or a reasonably close name."""
    return any(c["site_match"] or c["city_match"] or c["similarity"] >= _VIABLE_NAME
               for c in pages.values())

# facebook.com path prefixes that are NOT a company page (groups, people search,
# platform pages, a personal timeline, …) — excluded from candidates entirely.
_FB_NON_PAGE = {
    "groups", "public", "people", "watch", "marketplace", "events", "business",
    "help", "policies", "login", "sharer", "hashtag", "media", "story.php",
    "photo.php", "photo", "reel", "reels", "gaming", "jobs",
    "plugins", "dialog", "tr", "connect",   # social widgets / tracking pixels
    "community",                             # generic FB landing page, not a company
}
# instagram.com path prefixes that are NOT a profile (posts, reels, explore, …)
_IG_NON_PROFILE = {
    "p", "reel", "reels", "explore", "stories", "tv", "accounts", "direct",
    "about", "developer", "legal", "web",
}


def canonicalize_fb(url: str) -> dict | None:
    """Reduce any facebook.com URL to the page it belongs to.
    Returns {platform, page_id, handle} or None if it isn't a usable company
    page. `page_id` is Facebook's NUMERIC id when the URL exposes it — that's
    the id the Ad Library / Apify fetch needs later."""
    try:
        p = urlparse(url if "//" in url else "https://" + url)
    except ValueError:
        return None
    if "facebook.com" not in (p.netloc or "").lower():
        return None
    parts = [seg for seg in (p.path or "").split("/") if seg]
    if not parts:
        return None
    first = parts[0].lower()

    if first == "profile.php":   # personal profile
        return None
    if first in _FB_NON_PAGE:
        return None
    if first == "p" and len(parts) >= 2:          # /p/Some-Name-123456789
        m = re.search(r"-(\d{5,})$", parts[1])
        return {"platform": "facebook", "page_id": m.group(1) if m else None, "handle": parts[1]}
    if re.fullmatch(r"\d{5,}", parts[0]):          # /123456789/...
        return {"platform": "facebook", "page_id": parts[0], "handle": None}
    if first == "pages" and len(parts) >= 3 and re.fullmatch(r"\d{5,}", parts[-1]):
        return {"platform": "facebook", "page_id": parts[-1], "handle": parts[1]}
    # bare vanity handle — but the old "Name-<numeric id>" format embeds the
    # numeric page id the ad fetch needs (e.g. /Maderos-246334416039116)
    m = re.search(r"-(\d{7,})$", parts[0])
    return {"platform": "facebook", "page_id": m.group(1) if m else None, "handle": parts[0]}


def canonicalize_ig(url: str) -> dict | None:
    """Reduce any instagram.com URL to the profile it belongs to.
    Instagram exposes no numeric page id in URLs, so page_id is always None —
    an IG-resolved identity is 'known but not yet fetch-ready' (same as a
    handle-only Facebook confirm)."""
    try:
        p = urlparse(url if "//" in url else "https://" + url)
    except ValueError:
        return None
    if "instagram.com" not in (p.netloc or "").lower():
        return None
    parts = [seg for seg in (p.path or "").split("/") if seg]
    if not parts or parts[0].lower() in _IG_NON_PROFILE:
        return None
    return {"platform": "instagram", "page_id": None, "handle": parts[0]}


def _canonicalize(url: str) -> dict | None:
    return canonicalize_fb(url) or canonicalize_ig(url)


def _page_key(canon: dict) -> str:
    if canon["page_id"]:
        return canon["page_id"]
    handle = (canon["handle"] or "").lower()
    return f'{canon["platform"][:2]}:{handle}' if handle else ""


def _clean_title(title: str) -> str:
    """Result titles are often a post/video caption or carry '| City',
    '(@handle)', '- Mentions/About/…'. Strip that noise down to the page name."""
    t = re.sub(r"\s*\(@[^)]*\)", "", title or "")
    t = t.split("|")[0].split("·")[0]
    t = re.sub(r"\s*[-–]\s*(Mentions|About|Posts|Photos|Videos|Reels|Home|Beiträge|Info|Instagram).*$",
               "", t, flags=re.I)
    t = re.sub(r"\s+added a new photo.*$", "", t, flags=re.I)
    return t.strip(" -–|·") or (title or "").strip()


def _is_bare_page(url: str, platform: str) -> bool:
    """True when the URL points at the page/profile itself, not a sub-page
    (bare results carry the real page NAME as their title)."""
    try:
        parts = [seg for seg in (urlparse(url).path or "").split("/") if seg]
    except ValueError:
        return False
    if not parts:
        return False
    if platform == "facebook" and parts[0].lower() in ("p", "pages"):
        return len(parts) <= 2
    return len(parts) == 1


def _serper(q: str, country: str) -> dict:
    r = requests.post(
        config.SERPER_SEARCH_URL,
        headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": q, "gl": (country or "de").lower(), "hl": "de", "num": 10},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"serper search failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def _collect(resp: dict, target: str, dom: str | None, city_lc: str | None,
             from_domain_query: bool = False) -> dict[str, dict]:
    """Turn a serper response into {page_key: candidate}, keeping the best
    (most-corroborated) row per page/profile."""
    out: dict[str, dict] = {}

    def consider(title, url, snippet):
        canon = _canonicalize(url)
        if not canon:
            return
        key = _page_key(canon)
        if not key:
            return
        blob = f"{url} {title} {snippet}".lower()
        bare = _is_bare_page(url, canon["platform"])
        clean = _clean_title(title)
        # score name against BOTH the cleaned title and the raw snippet — a page
        # surfaced via a post caption has its name in the snippet, not the title
        name_sim = max(fuzz.token_set_ratio(target, _clean_name_for_similarity(clean)),
                       fuzz.token_set_ratio(target, _clean_name_for_similarity(f"{title} {snippet}"))) / 100
        site_match = from_domain_query or bool(dom and dom in blob)
        city_match = bool(city_lc and city_lc in blob)
        prev = out.get(key)
        if prev is None:
            out[key] = {
                "platform": canon["platform"],
                "page_id": canon["page_id"], "handle": canon["handle"],
                "name": clean if bare else (canon["handle"] or clean),
                "profile_uri": url, "similarity": round(name_sim, 2),
                "site_match": site_match, "city_match": city_match,
                "snippet": (snippet or "")[:220],
                "category": None, "blocked": False, "ad_count": 0, "active_ad_count": 0,
                "_bare": bare,
            }
        else:
            prev["site_match"] = prev["site_match"] or site_match
            prev["city_match"] = prev["city_match"] or city_match
            prev["similarity"] = max(prev["similarity"], round(name_sim, 2))
            if not prev["page_id"] and canon["page_id"]:
                prev["page_id"] = canon["page_id"]
            if len(snippet or "") > len(prev.get("snippet") or ""):
                prev["snippet"] = snippet[:220]
            # a bare-page result gives the real page name — prefer it for display
            if bare and not prev["_bare"]:
                prev["name"], prev["profile_uri"], prev["_bare"] = clean, url, True

    for o in resp.get("organic", []) or []:
        consider(o.get("title", ""), o.get("link", ""), o.get("snippet", ""))
    kg = resp.get("knowledgeGraph") or {}
    for key in ("facebook", "Facebook", "instagram", "Instagram"):
        if kg.get(key):
            consider(kg.get("title", ""), kg[key], kg.get("description", ""))
    return out


def _merge(pages: dict[str, dict], extra: dict[str, dict], mark_site_match: bool = False) -> None:
    for key, cand in extra.items():
        if key in pages:
            prev = pages[key]
            prev["site_match"] = prev["site_match"] or cand["site_match"] or mark_site_match
            prev["city_match"] = prev["city_match"] or cand["city_match"]
            prev["similarity"] = max(prev["similarity"], cand["similarity"])
            if not prev["page_id"] and cand["page_id"]:
                prev["page_id"] = cand["page_id"]
            if cand.get("_bare") and not prev.get("_bare"):
                prev["name"], prev["_bare"] = cand["name"], True
        else:
            pages[key] = cand


def _company_context(company: dict | None, site_text: str | None) -> str:
    """Everything we know about the company, phrased for the LLM prompts:
    master-data fields (street, segment, email, ...) plus an excerpt of the
    company's own website. Empty string when we know nothing extra."""
    parts = []
    if company:
        bits = [f"{k.replace('_', ' ')}: {v}" for k, v in company.items() if v]
        if bits:
            parts.append("Known master data — " + "; ".join(bits) + ".")
    if site_text:
        parts.append(f"Excerpt from the company's own website: {site_text[:500]!r}.")
    return (" ".join(parts) + " ") if parts else ""


def _llm_pick(name: str, city: str | None, dom: str | None, cands: list[dict],
              company: dict | None = None, site_text: str | None = None) -> int | None:
    """Ask Claude Haiku which candidate (if any) actually IS the company —
    judged against the FULL profile we hold (master data + website text), not
    just the name. Returns the candidate index, -1 for 'none of them', or None
    when the LLM is unavailable/failed (callers then fall back to deterministic
    rules). Best-effort by design — an LLM hiccup must never break the check."""
    if not config.ANTHROPIC_API_KEY or not cands:
        return None
    try:
        import anthropic
        listing = "\n".join(
            f'{i}. [{c["platform"]}] name={c["name"]!r} url={c["profile_uri"]} '
            f'signals: name-match {int(c.get("similarity", 0) * 100)}%'
            f'{", same city" if c.get("city_match") else ""}'
            f'{", links to company website" if c.get("site_match") else ""} '
            f'snippet={(c.get("snippet") or "")!r}'
            for i, c in enumerate(cands))
        prompt = (
            f"Company: {name!r}, city: {city or 'unknown'}, website: {dom or 'unknown'}. "
            + _company_context(company, site_text) +
            "A German building-sector business (windows/glazing/carpentry/metalwork — a Solarlux trade partner). "
            "Below are social-media profiles found via Google. Which ONE is this company's official "
            "Facebook page or Instagram profile? Note: the page often uses a BRAND name that differs "
            "from the legal company name (e.g. 'Holzhandel Vogt' for 'Alfred Vogt GmbH'), and snippets "
            "are often post captions, not page descriptions — weigh the listed signals too. "
            "A personal profile of the owner counts only if the snippet clearly shows it represents "
            "this business. Politicians, associations, namesakes, unrelated businesses and generic "
            "pages must be rejected.\n"
            f"{listing}\n"
            'Reply with ONLY JSON: {"pick": <index>} or {"pick": -1} if none clearly belongs to this company.'
        )
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=50,
                                     messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text
        m = re.search(r'"pick"\s*:\s*(-?\d+)', text) or re.search(r'(-?\d+)', text)
        if not m:
            return None
        pick = int(m.group(1))
        return pick if -1 <= pick < len(cands) else None
    except Exception:
        return None


def _llm_keywords(name: str, city: str | None, dom: str | None,
                  company: dict | None = None, site_text: str | None = None) -> list[str]:
    """Last resort when every deterministic query found nothing viable: ONE
    Haiku call reads EVERYTHING we hold about the company (master data + its
    own website text) and proposes the page names the business would actually
    use on Facebook/Instagram (brand name, shop name, owner's name — anything
    but the legal name we already searched). Best-effort: [] on any failure."""
    if not config.ANTHROPIC_API_KEY:
        return []
    try:
        import anthropic
        prompt = (
            f"Company: {name!r}, city: {city or 'unknown'} (Germany), website: {dom or 'unknown'}. "
            + _company_context(company, site_text) +
            "This German building-sector business (windows/glazing/carpentry/metalwork) has a Facebook or "
            "Instagram page that Google searches for the legal name did NOT find. Such businesses usually "
            "name their page differently: a brand or shop name (often visible in the website text or the "
            "email/website domain), the owner's personal name, a shortened form, or an ascii spelling. "
            "From the data above, suggest up to 3 search phrases (2–4 words each, most likely first) that "
            "would find their page. No legal forms (GmbH, KG). Only phrases meaningfully different from "
            f"{name!r} itself.\n"
            'Reply ONLY JSON: {"queries": ["...", "..."]}'
        )
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=150,
                                     messages=[{"role": "user", "content": prompt}])
        m = re.search(r"\{.*\}", msg.content[0].text, re.S)
        arr = json.loads(m.group(0)).get("queries", []) if m else []
        return [str(q).strip() for q in arr if str(q).strip()][:3]
    except Exception:
        return []


def _profile_url(c: dict) -> str:
    if c["platform"] == "instagram":
        return f"https://www.instagram.com/{c['handle']}/"
    return (f"https://www.facebook.com/{c['page_id']}/" if c["page_id"]
            else f"https://www.facebook.com/{c['handle']}/")


def resolve_identity(name: str, country: str = "DE", website_domain: str | None = None,
                     city: str | None = None, company: dict | None = None,
                     site_text: str | None = None) -> dict:
    """Online identity check for ONE company. Returns
    {status, page_id, page_name, page_url, platform, ads:[], candidates, search_term}.

    `company` (master-data fields) and `site_text` (an excerpt of the company's
    own website) feed the two LLM stages: the keyword generator that rephrases
    the search when every deterministic query finds nothing, and the judge that
    compares candidates against the full company profile."""
    if not config.SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY is not set in .env")

    term = search_term(name)
    dom = _registered_domain(website_domain)
    city_lc = (city or "").strip().lower() or None
    target = _clean_name_for_similarity(name)

    pages: dict[str, dict] = {}
    tried: set[str] = set()

    def run(q: str, from_domain_query: bool = False) -> None:
        """One serper query merged into the pool; dedupes repeated phrasings."""
        q = " ".join(q.split())
        if not q or q in tried:
            return
        tried.add(q)
        extra = _collect(_serper(q, country), target, dom, city_lc,
                         from_domain_query=from_domain_query)
        _merge(pages, extra, mark_site_match=from_domain_query)

    run(f"{term} {city or ''} facebook")

    # The name+city query is high-variance (Google may not surface the page in a
    # given call). A domain-anchored query is stable and precise — a unique
    # domain resolves to one page — so ALWAYS run it when we have a domain.
    # This is also what nails brand≠legal-name cases (e.g. "Alfred Vogt GmbH"
    # whose page is "Holzhandel Vogt").
    if dom:
        run(f'site:facebook.com "{dom}"', from_domain_query=True)

    # Instagram fallback — both platforms are Meta and share one ad identity,
    # and some partners only maintain an Instagram profile. When Facebook gave
    # us nothing domain-proven, one IG-scoped query joins the candidate pool.
    if not any(c["site_match"] for c in pages.values()):
        run(f"{term} {city or ''} instagram")

    # ---- fallback ladder — only when nothing viable was found, so the easy
    # majority never pays for it. Each rung stops the ladder on first success.
    if not _pool_viable(pages):
        ladder = [f"{term} facebook"]                          # 1. drop the city (it can suppress the page)
        ascii_term = _umlaut_ascii(term).replace("-", " ")
        if ascii_term.lower() != term.lower():
            ladder.append(f"{ascii_term} {city or ''} facebook")   # 2. ascii spelling
        core = _brand_core(term)
        if core:
            ladder.append(f"{core} {city or ''} facebook")     # 3. brand core only
        ladder.append(f"{term} instagram")                     # 4. IG without city
        for vq in ladder:
            run(vq)
            if _pool_viable(pages):
                break

    # ---- LLM keyword stage — the stubborn residue: Haiku reads the full
    # company profile + website text and proposes the names the page would
    # actually use; each suggestion is searched until one yields a candidate.
    if not _pool_viable(pages):
        for kq in _llm_keywords(name, city, dom, company, site_text):
            run(f"{kq} facebook")
            if _pool_viable(pages):
                break

    ranked = sorted(pages.values(),
                    key=lambda c: (c["site_match"], c["city_match"], c["similarity"],
                                   c["platform"] == "facebook"),
                    reverse=True)

    candidates = [{**{k: v for k, v in c.items() if k != "_bare"},
                   "profile_uri": c.get("profile_uri") or _profile_url(c)} for c in ranked[:8]]

    if not ranked:
        return {"status": "no_ads_found", "page_id": None, "page_name": None, "page_url": None,
                "platform": None, "ads": [], "candidates": [], "search_term": term}

    best = ranked[0]
    llm = None
    if best["site_match"]:
        status = "confirmed"   # domain-proven — no LLM needed
    else:
        pick = _llm_pick(name, city, dom, ranked[:5], company=company, site_text=site_text)
        if pick == -1:
            if best["city_match"] and best["similarity"] >= _STRONG_NAME:
                # The judge and the deterministic signals disagree (strong
                # city+name evidence) — don't discard, let a human decide.
                status = "ambiguous"
                llm = "rejected_all"
                return {"status": status, "page_id": best["page_id"], "page_name": best["name"],
                        "page_url": _profile_url(best), "platform": best["platform"],
                        "ads": [], "candidates": candidates, "search_term": term, "llm": llm}
            # Weak candidates AND the judge rejected them all — store NO best
            # guess. An honest "no page found" beats an unrelated page.
            return {"status": "no_ads_found", "page_id": None, "page_name": None,
                    "page_url": None, "platform": None, "ads": [],
                    "candidates": candidates, "search_term": term, "llm": "rejected_all"}
        if pick is not None:
            best = ranked[pick]
            llm = "picked"
            status = "confirmed" if (best["city_match"] or best["similarity"] >= _STRONG_NAME) \
                else "ambiguous"
        else:
            # no LLM available — the deterministic conservative rule
            status = "confirmed" if (best["city_match"] and best["similarity"] >= _STRONG_NAME) \
                else "ambiguous"

    return {"status": status, "page_id": best["page_id"], "page_name": best["name"],
            "page_url": _profile_url(best), "platform": best["platform"],
            "ads": [], "candidates": candidates, "search_term": term, "llm": llm}
