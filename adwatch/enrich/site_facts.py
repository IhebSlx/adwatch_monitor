"""Facts a website states about itself in MACHINE-READABLE form — no LLM, no cost.

Everything here is deterministic parsing of markup the site publishes for search
engines and social platforms. It runs before the LLM stage and is strictly more
trustworthy than extraction from prose, so its values win on conflict.

Why this matters more than it sounds:

  * **Phone.** Not one of the 39 Spain-list companies without a website had a
    phone number, and phone is the STRONGEST website-identity signal there is
    (`validate.validate_site` ranks it first). Sites publish it as `tel:` hrefs
    and in JSON-LD `telephone`. Harvesting it turns a future re-verification from
    "unprovable" into "proven".

  * **Facebook/Instagram.** JSON-LD `sameAs` and footer icons name the company's
    OWN social profiles. That is a far stronger Meta-page identity anchor than a
    name search — the page is linked from the verified website, so the ads found
    under it demonstrably belong to this company.

  * **foundingDate.** `sl_founding_date` is filled on 0.7% of CRM accounts, and
    the LLM may only lift a year stated in prose. Schema.org states it outright.

Parsing is defensive throughout: a site's JSON-LD is frequently invalid, nested
in @graph, a list, or several blocks at once, and a single malformed block must
never cost the whole company its enrichment.
"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit

# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------
_LD_RE = re.compile(
    r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)

# schema.org types that describe the company itself (not an article or a product)
_ORG_TYPES = {"organization", "localbusiness", "corporation", "store",
              "homeandconstructionbusiness", "generalcontractor",
              "professionalservice", "hardwarestore", "roofingcontractor",
              "electrician", "plumber", "contractor", "place"}


def _walk(node, out: list[dict]) -> None:
    """Collect every dict in an arbitrarily nested JSON-LD structure."""
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def _types_of(node: dict) -> set[str]:
    t = node.get("@type")
    vals = t if isinstance(t, list) else [t]
    return {str(v).lower() for v in vals if v}


def json_ld_blocks(html: str) -> list[dict]:
    """Every dict inside every JSON-LD block. Invalid blocks are skipped."""
    blocks: list[dict] = []
    for raw in _LD_RE.findall(html or ""):
        text = raw.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            # Trailing commas and unescaped newlines are common; one salvage try.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", text))
            except (ValueError, TypeError):
                continue
        _walk(data, blocks)
    return blocks


def _first_str(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for v in value:
            got = _first_str(v)
            if got:
                return got
    if isinstance(value, dict):
        for key in ("name", "@id", "url", "value"):
            got = _first_str(value.get(key))
            if got:
                return got
    return None


# ---------------------------------------------------------------------------
# contact details
# ---------------------------------------------------------------------------
_TEL_RE = re.compile(r'href\s*=\s*["\']tel:([^"\']+)["\']', re.I)
_MAIL_RE = re.compile(r'href\s*=\s*["\']mailto:([^"\'?]+)', re.I)
_META_RE = re.compile(
    r'<meta[^>]+(?:name|property)\s*=\s*["\']([^"\']+)["\'][^>]+content\s*=\s*["\']([^"\']*)["\']',
    re.I)

# Generic inboxes are a company address, but a personal one is personal data we
# have no reason to store — the app already avoids collecting person names.
_ROLE_MAILBOXES = ("info", "kontakt", "contact", "contacto", "office", "mail",
                   "hello", "buero", "buro", "empresa", "ventas", "verkauf",
                   "administracion", "comercial", "sales", "anfrage", "post")

_SOCIAL_HOSTS = {
    "facebook": ("facebook.com", "fb.com", "fb.me"),
    "instagram": ("instagram.com",),
    "linkedin": ("linkedin.com",),
    "youtube": ("youtube.com", "youtu.be"),
}
# Never a company's own profile — sharer widgets and platform chrome.
_SOCIAL_JUNK = ("/sharer", "/share.php", "/plugins/", "/dialog/", "intent/",
                "/tr?", "/policy", "/help", "/login", "/privacy")


def _social_from_urls(urls) -> dict[str, str]:
    out: dict[str, str] = {}
    for u in urls:
        if not isinstance(u, str):
            continue
        low = u.lower()
        if any(j in low for j in _SOCIAL_JUNK):
            continue
        for net, hosts in _SOCIAL_HOSTS.items():
            if net in out:
                continue
            if any(h in low for h in hosts):
                path = urlsplit(u).path.strip("/")
                if not path or path.count("/") > 2:
                    continue      # a bare host or a deep post is not a profile
                out[net] = u.split("?")[0].rstrip("/")
    return out


def _meta_tags(html: str) -> dict[str, str]:
    return {k.lower().strip(): (v or "").strip()
            for k, v in _META_RE.findall(html or "")}


def extract(html: str, *, base_url: str | None = None) -> dict:
    """Structured self-declared facts. Every key may be absent.

    Returns keys: phone, email, postal_code, street, city, country,
    founded_year, legal_name, social (dict), meta_description, language,
    latitude, longitude, sources (which mechanism supplied what).
    """
    out: dict = {}
    src: dict[str, str] = {}
    html = html or ""

    # ---- JSON-LD (most reliable) ----
    org = None
    for block in json_ld_blocks(html):
        if _types_of(block) & _ORG_TYPES:
            org = block
            break
    if org:
        def take(key, jkey, conv=None):
            val = _first_str(org.get(jkey))
            if val:
                out[key] = conv(val) if conv else val
                src[key] = "json-ld"

        take("phone", "telephone")
        take("email", "email")
        take("legal_name", "legalName") or take("legal_name", "name")
        founded = _first_str(org.get("foundingDate")) or _first_str(org.get("foundingdate"))
        if founded:
            m = re.search(r"(1[89]\d{2}|20\d{2})", founded)
            if m:
                out["founded_year"] = int(m.group(1))
                src["founded_year"] = "json-ld"
        addr = org.get("address")
        addr = addr[0] if isinstance(addr, list) and addr else addr
        if isinstance(addr, dict):
            for key, jkey in (("street", "streetAddress"),
                              ("postal_code", "postalCode"),
                              ("city", "addressLocality"),
                              ("country", "addressCountry")):
                val = _first_str(addr.get(jkey))
                if val:
                    out[key] = val
                    src[key] = "json-ld"
        geo = org.get("geo")
        geo = geo[0] if isinstance(geo, list) and geo else geo
        if isinstance(geo, dict):
            for key, jkey in (("latitude", "latitude"), ("longitude", "longitude")):
                try:
                    out[key] = float(str(geo.get(jkey)).replace(",", "."))
                    src[key] = "json-ld"
                except (TypeError, ValueError):
                    pass
        same = org.get("sameAs")
        social = _social_from_urls(same if isinstance(same, list) else [same])
        if social:
            out["social"] = social
            src["social"] = "json-ld"

    # ---- tel: / mailto: hrefs ----
    if "phone" not in out:
        tels = [unquote(t).strip() for t in _TEL_RE.findall(html)]
        tels = [t for t in tels if sum(ch.isdigit() for ch in t) >= 7]
        if tels:
            out["phone"] = tels[0]
            src["phone"] = "tel-href"
    if "email" not in out:
        mails = [unquote(m).strip().lower() for m in _MAIL_RE.findall(html)]
        role = [m for m in mails
                if any(m.startswith(p + "@") or m.startswith(p + ".") for p in _ROLE_MAILBOXES)]
        if role:
            out["email"] = role[0]
            src["email"] = "mailto-href"

    # ---- social links anywhere in the markup ----
    hrefs = re.findall(r'href\s*=\s*["\']([^"\']+)["\']', html)
    found = _social_from_urls(hrefs)
    if found:
        merged = dict(out.get("social") or {})
        for k, v in found.items():
            merged.setdefault(k, v)
        out["social"] = merged
        src.setdefault("social", "href")

    # ---- meta tags ----
    meta = _meta_tags(html)
    desc = meta.get("og:description") or meta.get("description")
    if desc:
        out["meta_description"] = desc[:400]
        src["meta_description"] = "meta"
    lang = meta.get("og:locale")
    if not lang:
        m = re.search(r'<html[^>]+lang\s*=\s*["\']([a-zA-Z-]{2,5})["\']', html, re.I)
        lang = m.group(1) if m else None
    if lang:
        out["language"] = lang.replace("_", "-")[:5].lower()
        src["language"] = "html-lang"

    if out:
        out["sources"] = src
    return out
