"""TIER 2 — turn a company's own website text into structured facts, with ONE
LLM call per company (Haiku by default: ~2k in / ~250 out tokens, so the whole
3,600-company book costs roughly $12 once).

Design rules that make the output trustworthy enough for BD decisions:

  * EXTRACT, never infer. Every field must be stated on the page. Unknown -> null.
    The model is explicitly told not to estimate — an invented "50-100
    Mitarbeiter" is worse than an empty field, because nobody can tell it was a
    guess.
  * `employee_hint` and `founded_year` are VERBATIM lifts, not analysis.
  * `evidence` carries the short quote each fact came from, so any value can be
    checked without re-crawling (the same auditability as CompanyPage.evidence).
  * NO personal data. Many rows here are sole traders (their company name IS a
    person's name), so Geschäftsführer/owner/employee names are deliberately not
    collected — company-level facts only (GDPR).

The genuinely high-value field for this tool is `brands`: whether the site still
promotes Solarlux, and which competitor brands it features. That maps straight
onto the win-back/Divergenz thesis and costs nothing extra.
"""
from __future__ import annotations

import json

from .. import config

# Solarlux's own brand plus the competitors worth knowing about. Given to the
# model as a closed vocabulary so results stay comparable across companies
# instead of free-text noise.
SOLARLUX_BRANDS = ("Solarlux",)
COMPETITOR_BRANDS = (
    "WAREMA", "Sunflex", "Weinor", "Schüco", "Kömmerling", "Griesser", "Markilux",
    "Lewens", "Klaiber", "Erhardt", "Guhr", "Alukon", "Roma", "Velux", "Velfac",
    "Internorm", "Josko", "Finstral", "Rehau", "Veka", "Gealan", "Aluprof",
    "Reynaers", "Heroal", "Wicona", "Hueck", "Sky-Frame", "Vitrocsa", "Air-Lux",
    "Nanawall", "Corradi", "Pratic", "Gibus", "Renson", "Brustor", "Kettler",
)

PRODUCT_VOCAB = (
    "Fenster", "Türen", "Haustüren", "Schiebetüren", "Wintergarten", "Terrassendach",
    "Glasdach", "Fassade", "Sonnenschutz", "Markisen", "Rollladen", "Jalousien",
    "Insektenschutz", "Tore", "Glas/Glaserei", "Metallbau", "Holzbau", "Zimmerei",
    "Tischlerei/Schreinerei", "Innenausbau", "Bausanierung", "Smart Home",
    "Balkon/Geländer", "Carport", "Pergola", "Gartenbau",
)

_PROMPT = """Du extrahierst Firmeninformationen aus dem Website-Text eines deutschen Bauelemente-/Handwerksbetriebs.

STRIKTE REGELN:
- Gib NUR zurück, was WÖRTLICH im Text steht. Nichts schätzen, nichts annehmen, nichts aus Weltwissen ergänzen.
- Wenn eine Information nicht im Text steht: null (bzw. leere Liste). Ein leeres Feld ist besser als eine Vermutung.
- KEINE Personennamen (keine Geschäftsführer, Inhaber, Mitarbeiter) — nur firmenbezogene Angaben.
- "employee_hint" und "founded_year" nur, wenn der Text sie ausdrücklich nennt ("15 Mitarbeiter", "seit 1952", "gegründet 1978").
- "evidence": kurzes wörtliches Zitat (max. 120 Zeichen) als Belegstelle je gefülltem Feld.

Antworte AUSSCHLIESSLICH mit diesem JSON (keine Erklärung, kein Markdown):
{
  "description_de": "<1-2 knappe Sätze: was macht die Firma? oder null>",
  "products": [<0-6 Werte aus: PRODUCT_VOCAB>],
  "founded_year": <Jahr als Zahl oder null>,
  "employee_hint": "<wörtliche Angabe zur Betriebsgröße oder null>",
  "legal_form": "<GmbH | GmbH & Co. KG | KG | OHG | AG | e.K. | Einzelunternehmen | GbR | null>",
  "service_area": "<Region/Umkreis, falls genannt, sonst null>",
  "mentions_solarlux": <true|false>,
  "competitor_brands": [<genannte Marken aus: COMPETITOR_LIST>],
  "evidence": {"<feldname>": "<Zitat>", ...}
}

PRODUCT_VOCAB: __PRODUCTS__
COMPETITOR_LIST: __COMPETITORS__

Website-Text:
"""


def _prompt() -> str:
    return (_PROMPT.replace("__PRODUCTS__", ", ".join(PRODUCT_VOCAB))
                   .replace("__COMPETITORS__", ", ".join(COMPETITOR_BRANDS)))


def _coerce_year(value) -> int | None:
    """Only a plausible founding year survives (a stray '2024' copyright line or
    a phone fragment must not become founded_year)."""
    try:
        y = int(str(value)[:4])
    except (TypeError, ValueError):
        return None
    return y if 1700 <= y <= 2100 else None


def _clean_list(value, vocab: tuple[str, ...], limit: int) -> list[str]:
    """Keep only values from the closed vocabulary (case-insensitive), deduped."""
    if not isinstance(value, list):
        return []
    lookup = {v.lower(): v for v in vocab}
    out: list[str] = []
    for item in value:
        canon = lookup.get(str(item).strip().lower())
        if canon and canon not in out:
            out.append(canon)
        if len(out) >= limit:
            break
    return out


def extract_facts(site_text: str, model: str | None = None) -> dict:
    """One LLM call -> validated fact dict. Raises on a missing key/bad response
    so the caller can record an error rather than store junk."""
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set — needed to extract company facts")
    text = (site_text or "").strip()
    if len(text) < 80:
        raise ValueError("site text too short to extract anything from")

    from anthropic import Anthropic

    use_model = model or config.ANTHROPIC_MODEL
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=use_model,
        max_tokens=700,
        messages=[{"role": "user", "content": _prompt() + text[:9000]}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)

    desc = (data.get("description_de") or "").strip() or None
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    return {
        "description_de": desc[:600] if desc else None,
        "products": _clean_list(data.get("products"), PRODUCT_VOCAB, 6),
        "founded_year": _coerce_year(data.get("founded_year")),
        "employee_hint": ((data.get("employee_hint") or "").strip() or None),
        "legal_form": ((data.get("legal_form") or "").strip() or None),
        "service_area": ((data.get("service_area") or "").strip() or None),
        "mentions_solarlux": bool(data.get("mentions_solarlux")),
        "competitor_brands": _clean_list(data.get("competitor_brands"), COMPETITOR_BRANDS, 12),
        "evidence": {str(k)[:40]: str(v)[:200] for k, v in list(evidence.items())[:12]},
        "llm_model": use_model,
    }
