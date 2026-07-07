"""Ad-intent classification.

MOCK / no-key -> deterministic keyword+lexicon classifier (also used as a cheap
pre-filter and as the fallback when the LLM call fails).
LIVE + ANTHROPIC_API_KEY -> Claude Haiku returns strict JSON.

Categories: recruitment | product_sale | brand_awareness | event_promo | other
"""
from __future__ import annotations

import json

from . import config

CATEGORIES = ["recruitment", "product_sale", "brand_awareness", "event_promo", "other"]

_HIRING = ["wir suchen", "bewerben", "bewirb", "karriere", "m/w/d", "stellenangebot",
           "stelle", "verstärkung", "mitarbeiter", "join our team", "hiring", "we are hiring",
           "vollzeit", "teilzeit", "ausbildung", "jobs", "job"]
_EVENT = ["hausmesse", "messe", "webinar", "event", "veranstaltung", "live-vorführung",
          "tag der offenen tür", "anmelden"]
_SALE = ["aktionspreis", "angebot", "rabatt", "sparen", "jetzt kaufen", "bestellen",
         "shop", "sale", "%", "€", "sichern", "preis"]

# Domain lexicon — building products / glazing (also used to extract `product`)
_PRODUCT_LEXICON = [
    "Wintergärten", "Wintergarten", "Terrassendächer", "Terrassendach", "Glasfaltwände",
    "Glasfaltwand", "Schiebetüren", "Schiebetür", "Haustüren", "Haustür", "Fenster",
    "Rollläden", "Rollladen", "Markisen", "Markise", "Türen", "Tür", "Tore", "Tor",
]


def _extract_product(text: str) -> str | None:
    found: list[str] = []
    for term in _PRODUCT_LEXICON:
        if term.lower() in text.lower() and not any(term.lower() in f.lower() for f in found):
            found.append(term)
    return ", ".join(dict.fromkeys(found)) if found else None


def classify_deterministic(text: str) -> tuple[str, str | None]:
    t = (text or "").lower()
    if any(k in t for k in _HIRING):
        return "recruitment", None
    if any(k in t for k in _EVENT):
        return "event_promo", None
    product = _extract_product(text or "")
    if product or any(k in t for k in _SALE):
        return "product_sale", product
    if t.strip():
        return "brand_awareness", None
    return "other", None


def _classify_llm(text: str) -> tuple[str, str | None]:
    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = (
        "Classify this advertisement into exactly one category and, if it sells a product, "
        "name the product briefly.\n"
        f"Categories: {', '.join(CATEGORIES)}.\n"
        "Return ONLY a JSON object, no prose, no markdown fences, of the form "
        '{\"category\": \"...\", \"product\": \"...\"|null}.\n\n'
        f"Ad text:\n{text}"
    )
    msg = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    category = data.get("category")
    if category not in CATEGORIES:
        category = "other"
    return category, data.get("product")


def classify_ad(text: str) -> dict:
    """Returns {category, product, classifier}."""
    use_llm = config.is_live() and bool(config.ANTHROPIC_API_KEY)
    if use_llm:
        try:
            category, product = _classify_llm(text)
            return {"category": category, "product": product, "classifier": "llm"}
        except Exception:
            pass  # fall through to deterministic
    category, product = classify_deterministic(text)
    return {"category": category, "product": product, "classifier": "deterministic"}
