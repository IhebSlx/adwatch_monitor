"""TIER 0 — derive a company's website from its SAP contact email. Free: no
API call, no crawl, no LLM.

`info@sf-mitschele.de` -> `sf-mitschele.de`. Roughly 1,000 of the companies
with no website have a usable (non-freemail) email domain, so this is by far
the cheapest coverage available.

It is NOT trusted blindly: a contact email can live on a supplier's or a
portal's domain rather than the company's own (real examples from this dataset:
`warema.de` — a Solarlux COMPETITOR — and `orosimoitsa.it`). So a derived
domain is only a CANDIDATE; validate.py has to confirm ownership before
service.py accepts it.
"""
from __future__ import annotations

import re

# Consumer mailbox providers — an address here says nothing about a website.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "t-online.de", "web.de", "gmx.de", "gmx.net",
    "gmx.at", "gmx.ch", "yahoo.de", "yahoo.com", "hotmail.de", "hotmail.com",
    "outlook.de", "outlook.com", "online.de", "freenet.de", "arcor.de", "aol.com",
    "icloud.com", "me.com", "mac.com", "mail.de", "posteo.de", "live.de", "live.com",
    "msn.com", "vodafone.de", "kabelmail.de", "kabelbw.de", "unitybox.de",
    "1und1.de", "einsundeins.de", "telekom.de", "t-mobile.de", "o2online.de",
    "ewetel.net", "htp-tel.de", "netcologne.de", "versanet.de", "gmail.de",
    "protonmail.com", "proton.me", "yandex.com", "zoho.com",
    # Country variants of the big freemailers. `gmail.co.uk` reached the Spanish
    # review queue as a website candidate for "D. Miguel Romero" because only
    # `gmail.com` and `gmail.de` were listed.
    "gmail.co.uk", "gmail.es", "gmail.fr", "gmail.it", "gmail.nl",
    "hotmail.es", "hotmail.fr", "hotmail.it", "hotmail.co.uk", "hotmail.nl",
    "outlook.es", "outlook.fr", "outlook.it", "yahoo.es", "yahoo.fr",
    "yahoo.it", "yahoo.co.uk", "live.nl", "telefonica.net", "terra.es",
    "wanadoo.es", "movistar.es", "orange.es", "ya.com",
}

# Domains that are never a single company's own site, even though partners
# sometimes use an address there. Portals/marketplaces/social/registries.
NON_COMPANY_DOMAINS = {
    "gelbeseiten.de", "dasoertliche.de", "11880.com", "wlw.de", "europages.de",
    "northdata.de", "firmenwissen.de", "handelsregister.de", "bundesanzeiger.de",
    "facebook.com", "instagram.com", "linkedin.com", "xing.com", "youtube.com",
    "twitter.com", "x.com", "tiktok.com", "pinterest.de", "pinterest.com",
    "wikipedia.org", "google.com", "google.de", "maps.google.com", "amazon.de",
    "ebay.de", "etsy.com", "houzz.de", "myhammer.de", "check24.de", "wer-zu-wem.de",
    "kununu.com", "indeed.com", "stepstone.de", "meinestadt.de", "yelp.de",
    "provenexpert.com", "golocal.de", "cylex.de", "branchenbuch.de", "marktplatz.de",
    # trade portals found in live testing — exact registrable-domain matches, so
    # 'mueller-metallbau.com' is NOT caught, only the portal 'metallbau.com' is
    "dashandwerk.de", "firminform.de", "metallbau.com", "openregister.de",
    "registercheck.de", "dastelefonbuch.de",
}

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def normalize_domain(value: str | None) -> str | None:
    """'https://www.Foo.de/kontakt?x=1' -> 'foo.de'. None if it isn't a domain."""
    if not value:
        return None
    s = str(value).strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)     # strip scheme
    s = s.split("/")[0].split("?")[0].split("#")[0]  # strip path/query/fragment
    s = s.split("@")[-1]                             # tolerate a full email
    s = s.rstrip(".").strip()
    if s.startswith("www."):
        s = s[4:]
    if not s or "." not in s or not _DOMAIN_RE.match(s):
        return None
    # The SAP typo pattern 'http.x.de' (someone typed 'http.' instead of
    # 'http://') LOOKS like a valid hostname but never is one — found live as an
    # unreachable-crawl error in the 112-company pilot. Deliberately rejected
    # here (not silently fixed): a malformed value must go through salvage_domain
    # + validation, so the repair is PROVEN before anything trusts it.
    if s.split(".", 1)[0] in ("http", "https"):
        return None
    return s


def registrable(domain: str | None) -> str | None:
    """Best-effort registrable part, so `mail.foo.de` and `foo.de` compare equal.
    Handles the common German multi-part suffixes (co.uk, com.de, ...) without
    pulling in a public-suffix dependency."""
    d = normalize_domain(domain)
    if not d:
        return None
    parts = d.split(".")
    two_level = {"co.uk", "org.uk", "ac.uk", "com.de", "co.at", "or.at", "ac.at",
                 "co.ch", "com.tr", "com.pl", "co.nl"}
    if len(parts) >= 3 and ".".join(parts[-2:]) in two_level:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


_DOMAINISH_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


def salvage_domain(value: str | None) -> str | None:
    """Rescue a domain from a MALFORMED master-data value. Six of the 1,000
    imported websites are typo'd beyond what normalize_domain accepts, e.g.
    'https://http: //www.tischlerei-tieste.de', 'http;//www.x.de',
    'www.bauelemente-thoms .de', 'http./ www.alubau.org'.

    Deliberately separate from normalize_domain: a salvaged value is only ever a
    CANDIDATE and must still pass validate.validate_site before it is stored —
    the same value could be an unrelated note ('Quelle: aktionstage2019.de'),
    which validation will reject."""
    if not value:
        return None
    s = re.sub(r"\s+", "", str(value).strip().lower()).replace(";", ":")
    if "://" in s:
        s = s.rsplit("://", 1)[-1]
    # NOTE: do NOT pre-split on '/' — in these typo'd values the slash is a stray
    # separator, not a path ('http./www.alubau.org'). The regex below stops at
    # '/' by itself, and taking the FIRST candidate keeps 'x.de' from
    # 'www.x.de/foo.html'.
    for cand in _DOMAINISH_RE.findall(s) or []:
        cand = cand.strip(".")
        # strip junk leading labels — 'www.' and the 'http.'/'https.' typo label
        # ('http.terrassen-freye.de' -> 'terrassen-freye.de')
        changed = True
        while changed:
            changed = False
            for junk in ("www.", "http.", "https."):
                if cand.startswith(junk):
                    cand = cand[len(junk):]
                    changed = True
        tld = cand.rsplit(".", 1)[-1]
        if "." not in cand or not (2 <= len(tld) <= 24) or not tld.isalpha():
            continue
        if tld in ("html", "htm", "php", "asp", "aspx", "jsp", "pdf", "jpg", "jpeg",
                   "png", "gif", "svg", "css", "js", "xml", "json"):
            continue   # a file name, not a host
        got = normalize_domain(cand)
        if got:
            return got
    return None


def is_usable_company_domain(domain: str | None) -> bool:
    """False for freemail, portals/registries/social, and anything malformed."""
    d = normalize_domain(domain)
    if not d:
        return False
    reg = registrable(d)
    return not (d in FREEMAIL_DOMAINS or reg in FREEMAIL_DOMAINS
                or d in NON_COMPANY_DOMAINS or reg in NON_COMPANY_DOMAINS)


def domain_from_email(email: str | None) -> str | None:
    """The company-website candidate hiding in a SAP contact email, or None when
    there isn't a usable one (no address, freemail, portal, malformed)."""
    if not email or "@" not in str(email):
        return None
    dom = normalize_domain(str(email).rsplit("@", 1)[-1])
    if not dom or not is_usable_company_domain(dom):
        return None
    return dom
