"""PART 3a — Ad-intent classification.

Two backends:
- deterministic: weighted keyword scoring with WORD-BOUNDARY matching (fixes
  the old substring bugs where "natürlich" matched "Tür" and "100% Qualität"
  counted as a sale signal). Always available; also the fallback.
- llm: Claude Haiku with a German-aware prompt. Enabled automatically when
  ANTHROPIC_API_KEY is set in .env (live mode only). Costs well under
  €0.01/week at typical volume.

Every result carries its evidence (per-category scores + matched keywords) so
misclassifications can be audited later via Ad.classifier_raw.

Categories: recruitment | product_sale | brand_awareness | event_promo | other
"""
from __future__ import annotations

import json
import re

from .. import config

CATEGORIES = ["recruitment", "product_sale", "brand_awareness", "event_promo", "other"]

# ---------------------------------------------------------------------------
# Deterministic backend
# ---------------------------------------------------------------------------

# (phrase, weight). Strong = unambiguous intent markers; weight 2.
_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "recruitment": [
        ("m/w/d", 2), ("jetzt bewerben", 2), ("wir suchen", 2), ("wir stellen ein", 2),
        ("bewirb dich", 2), ("stellenangebot", 2), ("we are hiring", 2), ("join our team", 2),
        ("verstärkung gesucht", 2), ("dein neuer job", 2),
        ("karriere", 1), ("bewerben", 1), ("bewerbung", 1), ("stellenanzeige", 1),
        ("verstärkung", 1), ("mitarbeiter", 1), ("vollzeit", 1), ("teilzeit", 1),
        ("ausbildung", 1), ("quereinsteiger", 1), ("festanstellung", 1), ("gesucht", 1),
        ("job", 1), ("jobs", 1), ("hiring", 1), ("aufgepasst", 1),
    ],
    "event_promo": [
        ("tag der offenen tür", 2), ("hausmesse", 2), ("jetzt anmelden", 2),
        ("messe", 1), ("webinar", 1), ("event", 1), ("veranstaltung", 1),
        ("live-vorführung", 1), ("einladung", 1), ("ausstellung", 1), ("anmelden", 1),
    ],
    "product_sale": [
        ("jetzt kaufen", 2), ("angebot sichern", 2), ("aktionspreis", 2), ("rabatt", 2),
        ("sale", 2), ("sonderaktion", 2), ("aktionswochen", 2),
        ("angebot", 1), ("aktion", 1), ("sparen", 1), ("bestellen", 1), ("shop", 1),
        ("sichern", 1), ("preis", 1), ("kaufen", 1), ("konfigurieren", 1),
        ("beratungstermin", 1), ("jetzt anfragen", 1), ("kostenlose beratung", 1),
        ("beratung anfragen", 1), ("shop now", 1),
    ],
}

# Numeric sale signals. Discount: 1-2 digit percentage (5 %, 20% — NOT "100% Qualität").
# Price: number followed by €.
_NUMERIC_SALE = [
    (re.compile(r"(?<!\d)\d{1,2}\s*%"), 2, "<NN %>"),
    (re.compile(r"\d[\d.,]*\s*€"), 2, "<price €>"),
]

# Domain lexicon — building products / glazing (also used to extract `product`).
_PRODUCT_LEXICON = [
    "Wintergärten", "Wintergarten", "Terrassendächer", "Terrassendach",
    "Terrassenüberdachung", "Glasfaltwände", "Glasfaltwand", "Glas-Faltwände",
    "Schiebetüren", "Schiebetür", "Schiebefenster", "Haustüren", "Haustür",
    "Fenster", "Rollläden", "Rollladen", "Markisen", "Markise", "Türen", "Tür",
    "Tore", "Tor", "Sommergarten", "Lamellendach", "Pergola",
]


def _boundary_pattern(phrase: str) -> re.Pattern:
    """Whole-word/phrase match: 'tür' must NOT match inside 'natürlich'."""
    return re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.IGNORECASE)


_COMPILED: dict[str, list[tuple[re.Pattern, str, int]]] = {
    cat: [(_boundary_pattern(kw), kw, w) for kw, w in kws]
    for cat, kws in _KEYWORDS.items()
}
_PRODUCT_PATTERNS = [(_boundary_pattern(term), term) for term in _PRODUCT_LEXICON]


def _extract_products(text: str) -> list[str]:
    found: list[str] = []
    for pattern, term in _PRODUCT_PATTERNS:
        if pattern.search(text):
            # skip if a longer variant already matched (Wintergärten ⊃ Wintergarten)
            norm = term.lower().rstrip("en").rstrip("e")
            if not any(norm in f.lower() or f.lower().rstrip("en").rstrip("e") in term.lower()
                       for f in found):
                found.append(term)
    return found


def classify_deterministic(text: str) -> dict:
    """Weighted scoring across all categories; the strongest wins.
    Returns {category, product, scores, matched}."""
    t = text or ""
    scores = {c: 0 for c in ("recruitment", "event_promo", "product_sale")}
    matched: dict[str, list[str]] = {c: [] for c in scores}

    for cat, patterns in _COMPILED.items():
        for pattern, kw, weight in patterns:
            if pattern.search(t):
                scores[cat] += weight
                matched[cat].append(kw)

    for pattern, weight, label in _NUMERIC_SALE:
        if pattern.search(t):
            scores["product_sale"] += weight
            matched["product_sale"].append(label)

    products = _extract_products(t)
    if products:
        scores["product_sale"] += 1
        matched["product_sale"].append(f"<product: {', '.join(products)}>")

    best_cat, best_score = max(scores.items(), key=lambda kv: (kv[1],
                               -["recruitment", "event_promo", "product_sale"].index(kv[0])))
    if best_score == 0:
        best_cat = "brand_awareness" if t.strip() else "other"

    product = ", ".join(products) if (products and best_cat == "product_sale") else None
    return {"category": best_cat, "product": product,
            "scores": scores, "matched": {k: v for k, v in matched.items() if v}}


# ---------------------------------------------------------------------------
# LLM backend (Claude Haiku)
# ---------------------------------------------------------------------------

_LLM_PROMPT = """You classify German (sometimes English) Facebook/Instagram ads \
from building-product companies (windows, glass walls, winter gardens, doors).

Category definitions:
- recruitment: hiring people (Stellenanzeigen, "m/w/d", "jetzt bewerben", team growth)
- product_sale: selling or promoting a concrete product/service, incl. lead-gen \
("Beratung anfragen", "Angebot sichern", price/discount mentions)
- event_promo: inviting to an event (Messe, Hausmesse, Tag der offenen Tür, Webinar)
- brand_awareness: general image/values/quality content with no concrete product push
- other: none of the above / no meaningful text

Return ONLY a JSON object (no prose, no markdown):
{"category": "<one of: recruitment|product_sale|brand_awareness|event_promo|other>",
 "product": "<German product name(s) if product_sale, else null>"}

Ad text:
"""


def _classify_llm(text: str) -> dict:
    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": _LLM_PROMPT + (text or "")[:2000]}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    category = data.get("category")
    if category not in CATEGORIES:
        category = "other"
    product = data.get("product") or None
    return {"category": category, "product": product}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def classify_ad(text: str) -> dict:
    """Returns {category, product, classifier, scores?, matched?}."""
    if config.ANTHROPIC_API_KEY:
        try:
            result = _classify_llm(text)
            return {**result, "classifier": "llm"}
        except Exception:  # noqa: BLE001 — LLM problems must never block collection
            pass
    det = classify_deterministic(text)
    return {**det, "classifier": "deterministic"}
