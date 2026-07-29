"""Does this website actually belong to THIS company? — deterministic checks.

This module is the safety gate for the whole enrichment package. A website is
only auto-accepted when hard SAP master data (phone / PLZ+Straße / a distinctive
name token) is found on the site itself. Everything weaker becomes
`needs_review` for a human.

Why so strict: the Meta identity work taught this the hard way — an
automatically-linked WRONG page silently attached another company's ads (and
poisoned the win-back ranking) for weeks. A wrong website is worse, because
every extracted fact after it inherits the error. So no LLM, no fuzzy "looks
about right" — a machine may only accept what it can PROVE from data that came
out of SAP.

Real trap from this dataset: several contact emails live on `warema.de` (a
Solarlux competitor). Deriving that domain is fine; accepting it is not — the
WAREMA site carries neither the dealer's phone nor its PLZ, so it fails here.
"""
from __future__ import annotations

import re

from .domains import normalize_domain, registrable

# Reuse the identity resolver's German compound-word logic so "which parts of a
# company name are actually distinctive" is defined in exactly one place
# (Metallbau/Fenster/GmbH are generic; 'Mitschele' is not).
try:
    from ..identity.serper_source import _distinctive_tokens as _id_distinctive_tokens
except Exception:  # noqa: BLE001 — keep enrichment working if that module moves
    _id_distinctive_tokens = None

_GENERIC_FALLBACK = {
    "gmbh", "kg", "ag", "co", "ohg", "ek", "mbh", "und", "der", "die", "das", "inh",
    "fenster", "tueren", "turen", "tore", "glas", "glaserei", "metallbau", "holzbau",
    "tischlerei", "schreinerei", "bauelemente", "bau", "sonnenschutz", "wintergarten",
    "fassade", "fassaden", "rollladen", "markisen", "terrassendach", "elemente",
    "technik", "service", "handel", "montage", "zentrum", "profi", "meister",
}


def _ascii_fold(s: str) -> str:
    return (s.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
            .replace("ß", "ss").replace("é", "e").replace("è", "e"))


def distinctive_tokens(name: str) -> set[str]:
    """The parts of a company name that actually identify it (no legal forms, no
    generic trade words). Delegates to the identity resolver when available."""
    if _id_distinctive_tokens is not None:
        try:
            toks = _id_distinctive_tokens(name)
            if toks:
                return {_ascii_fold(t) for t in toks if len(t) >= 4}
        except Exception:  # noqa: BLE001
            pass
    words = re.findall(r"[a-zäöüßA-ZÄÖÜ]{3,}", name or "")
    return {_ascii_fold(w) for w in words if _ascii_fold(w) not in _GENERIC_FALLBACK and len(w) >= 4}


# --------------------------------------------------------------------------- phone
def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", str(s or ""))


def national_phone_digits(phone: str | None) -> str:
    """German number -> national significant digits, so '+49 5405 1234-0',
    '0049 5405/12340' and '05405 1234 0' all compare equal."""
    d = _digits(phone)
    if not d:
        return ""
    if d.startswith("0049"):
        d = d[4:]
    elif d.startswith("49") and len(d) >= 11:
        d = d[2:]
    if d.startswith("0"):
        d = d[1:]
    return d


def phone_matches(company_phone: str | None, page_text: str | None) -> bool:
    """True if the company's phone appears on the page.

    Compares an 8-digit prefix of the national number (area code + base), which
    is what makes a different Durchwahl match: SAP may hold '05405 1234-0' while
    the site prints '05405 1234-20'. A 9-digit prefix would include the '-0'
    itself and miss it. 8 shared digits including the area code is far too
    specific to hit by chance, and we still require >=7 digits overall."""
    want = national_phone_digits(company_phone)
    if len(want) < 7:
        return False
    prefix = want[:8] if len(want) >= 8 else want
    haystack = _digits(page_text)
    return bool(haystack) and prefix in haystack


# --------------------------------------------------------------------------- address
def plz_matches(postal_code: str | None, page_text: str | None) -> bool:
    plz = _digits(postal_code)
    if len(plz) != 5 or not page_text:
        return False
    return re.search(rf"(?<!\d){re.escape(plz)}(?!\d)", page_text) is not None


def street_matches(street: str | None, page_text: str | None) -> bool:
    """The street NAME (house number ignored — sites format it differently)."""
    if not street or not page_text:
        return False
    name = re.sub(r"\d+.*$", "", str(street)).strip()   # drop house number onward
    name = re.sub(r"(stra(ss|ß)e|str\.?)$", "", _ascii_fold(name)).strip(" .-")
    if len(name) < 4:
        return False
    return name in _ascii_fold(page_text)


def name_token_matches(name: str | None, page_text: str | None) -> bool:
    toks = distinctive_tokens(name or "")
    if not toks or not page_text:
        return False
    hay = _ascii_fold(page_text)
    return any(t in hay for t in toks)


def domain_name_matches(name: str | None, domain: str | None) -> bool:
    """A distinctive name token appears in the domain itself
    ('sf-mitschele.de' for 'Fensterbau Mitschele') — usable even when the site
    can't be crawled."""
    d = registrable(normalize_domain(domain))
    if not d:
        return False
    stem = _ascii_fold(d.rsplit(".", 1)[0]).replace("-", "")
    toks = distinctive_tokens(name or "")
    return any(t in stem for t in toks if len(t) >= 4)


# --------------------------------------------------------------------------- gate
# Only these outcomes may be auto-accepted. Ordered strongest first; the label is
# stored on CompanyEnrichment.website_validated_by so any acceptance is auditable.
def validate_site(company: dict, domain: str | None, page_text: str | None) -> dict:
    """Decide whether `domain` provably belongs to `company`.

    Returns {ok, matched_by, signals}. `ok=True` only for a hard match:
      phone            — the SAP phone number is on the site
      plz_street       — same PLZ *and* same street
      plz_name         — same PLZ *and* a distinctive name token
      domain_plus_name — the domain carries the name AND the site confirms a name
                         token (used when a site has no address/phone in its text)
    A lone name token, or nothing at all, is NOT enough -> needs_review.
    """
    signals = {
        "phone": phone_matches(company.get("phone"), page_text),
        "plz": plz_matches(company.get("postal_code"), page_text),
        "street": street_matches(company.get("street"), page_text),
        "name_in_text": name_token_matches(company.get("name"), page_text),
        "name_in_domain": domain_name_matches(company.get("name"), domain),
    }
    if signals["phone"]:
        matched_by = "phone"
    elif signals["plz"] and signals["street"]:
        matched_by = "plz_street"
    elif signals["plz"] and signals["name_in_text"]:
        matched_by = "plz_name"
    elif signals["name_in_domain"] and signals["name_in_text"]:
        matched_by = "domain_plus_name"
    else:
        matched_by = None
    return {"ok": matched_by is not None, "matched_by": matched_by, "signals": signals}
