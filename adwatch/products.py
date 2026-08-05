"""One product vocabulary for the whole app.

Why this module exists: ads are written in the local market's language, but the
report is German. The first Spanish run produced these three "products" for two
companies — "cerramiento", "Porcheschließung/Porche-Verschluss" and
"windows and doors (wood, wood-aluminium, aluminium, PVC)". The first two are
the same product in two languages, the third is English plus a list of
*materials*. Free text like that cannot be counted, compared across companies,
or filtered on, and it reads as broken in a German PDF.

So every product name in the app — whether it came from ad text (any language)
or from a company website — is mapped onto `PRODUCT_VOCAB`. Anything that can't
be mapped is dropped rather than shown: a wrong-language product name is worse
than no product name, because it silently splits one family into several.

Both producers import from here, so they can never drift apart:
  - insights/classify.py  (products advertised, from ad creative)
  - enrich/extract.py     (products offered, from the company website)
"""
from __future__ import annotations

import re

# Canonical families. Order is cosmetic (it seeds the LLM prompts) but stable —
# stored values reference these strings, so rename nothing without a migration.
PRODUCT_VOCAB = (
    "Fenster", "Türen", "Haustüren", "Schiebetüren",
    "Glasfaltwand", "Terrassenverglasung",
    "Wintergarten", "Terrassendach", "Glasdach", "Fassade",
    "Sonnenschutz", "Markisen", "Rollladen", "Jalousien", "Insektenschutz",
    "Tore", "Glas/Glaserei", "Metallbau", "Holzbau", "Zimmerei",
    "Tischlerei/Schreinerei", "Innenausbau", "Bausanierung", "Smart Home",
    "Balkon/Geländer", "Carport", "Pergola", "Gartenbau",
)

# Free text -> canonical. Keys are lowercase; German inflections, English,
# Spanish and Portuguese all land on the same German family. Deliberately
# focused on what the live data actually contains plus obvious neighbours —
# an unmapped term is dropped, not guessed at.
_SYNONYMS: dict[str, str] = {
    # Terrassen-/Balkonverglasung — Solarlux's core product, and the one the
    # Spanish market names in four different ways.
    "cerramiento": "Terrassenverglasung",
    "cerramiento de terraza": "Terrassenverglasung",
    "cortina de cristal": "Terrassenverglasung",
    "cortinas de cristal": "Terrassenverglasung",
    "glascortina": "Terrassenverglasung",
    "porche": "Terrassenverglasung",
    "porch closure": "Terrassenverglasung",
    "porch enclosure": "Terrassenverglasung",
    "porcheverglasung": "Terrassenverglasung",
    "porcheschließung": "Terrassenverglasung",
    "porcheschliessung": "Terrassenverglasung",
    "porche-verschluss": "Terrassenverglasung",
    "terrassenverglasung": "Terrassenverglasung",
    "balkonverglasung": "Terrassenverglasung",
    "envidraçamento": "Terrassenverglasung",
    "envidracamento": "Terrassenverglasung",
    "varanda de vidro": "Terrassenverglasung",
    "terrace glazing": "Terrassenverglasung",
    # Glas-Faltwand
    "glasfaltwand": "Glasfaltwand",
    "glasfaltwände": "Glasfaltwand",
    "glas-faltwand": "Glasfaltwand",
    "faltwand": "Glasfaltwand",
    "faltverglasung": "Glasfaltwand",
    "glass folding wall": "Glasfaltwand",
    "folding glass door": "Glasfaltwand",
    "pared plegable": "Glasfaltwand",
    "cristal plegable": "Glasfaltwand",
    # Fenster / Türen
    "fenster": "Fenster",
    "window": "Fenster", "windows": "Fenster",
    "ventana": "Fenster", "ventanas": "Fenster", "janela": "Fenster",
    "tür": "Türen", "türen": "Türen",
    "door": "Türen", "doors": "Türen",
    "puerta": "Türen", "puertas": "Türen", "porta": "Türen",
    "haustür": "Haustüren", "haustüren": "Haustüren",
    "front door": "Haustüren", "entrance door": "Haustüren",
    "puerta de entrada": "Haustüren",
    "schiebetür": "Schiebetüren", "schiebetüren": "Schiebetüren",
    "schiebefenster": "Schiebetüren", "schiebesystem": "Schiebetüren",
    "schiebeverglasung": "Schiebetüren",
    "sliding door": "Schiebetüren", "sliding system": "Schiebetüren",
    "puerta corredera": "Schiebetüren", "corredera": "Schiebetüren",
    # Wintergarten / Dächer
    "wintergarten": "Wintergarten", "wintergärten": "Wintergarten",
    "sommergarten": "Wintergarten",
    "conservatory": "Wintergarten", "jardín de invierno": "Wintergarten",
    "terrassendach": "Terrassendach", "terrassendächer": "Terrassendach",
    "terrassenüberdachung": "Terrassendach",
    "lamellendach": "Terrassendach",
    "techo de terraza": "Terrassendach", "pergola bioclimática": "Terrassendach",
    "glasdach": "Glasdach",
    "pergola": "Pergola", "pérgola": "Pergola",
    # Sonnenschutz
    "markise": "Markisen", "markisen": "Markisen",
    "toldo": "Markisen", "toldos": "Markisen", "awning": "Markisen",
    "rollladen": "Rollladen", "rollläden": "Rollladen",
    "persiana": "Rollladen", "persianas": "Rollladen",
    "jalousie": "Jalousien", "jalousien": "Jalousien",
    "insektenschutz": "Insektenschutz", "fliegengitter": "Insektenschutz",
    "mosquitera": "Insektenschutz",
    "sonnenschutz": "Sonnenschutz",
    # Übriges Bauhandwerk
    "fassade": "Fassade", "facade": "Fassade", "fachada": "Fassade",
    "tor": "Tore", "tore": "Tore", "garagentor": "Tore",
    # NB: no bare "verglasung" — it lives inside Terrassen-/Balkon-/Schiebe-
    # verglasung, which are all more specific, and would tag every one of them
    # as a glazier's workshop on top of the real family.
    "glaserei": "Glas/Glaserei",
    "vidrio": "Glas/Glaserei", "vidrios": "Glas/Glaserei",
    "cristaleria": "Glas/Glaserei", "cristalería": "Glas/Glaserei",
    "metallbau": "Metallbau", "carpinteria metalica": "Metallbau",
    "carpintería metálica": "Metallbau", "cerrajeria": "Metallbau",
    "holzbau": "Holzbau", "zimmerei": "Zimmerei",
    "tischlerei": "Tischlerei/Schreinerei", "schreinerei": "Tischlerei/Schreinerei",
    "carpinteria": "Tischlerei/Schreinerei", "carpintería": "Tischlerei/Schreinerei",
    "carpintaria": "Tischlerei/Schreinerei",
    "innenausbau": "Innenausbau", "bausanierung": "Bausanierung",
    "sanierung": "Bausanierung", "renovation": "Bausanierung",
    "smart home": "Smart Home",
    "balkon": "Balkon/Geländer", "geländer": "Balkon/Geländer",
    "barandilla": "Balkon/Geländer", "balcón": "Balkon/Geländer",
    "carport": "Carport", "gartenbau": "Gartenbau",
}

# Materials and finishes are not products. They arrived inside strings like
# "windows and doors (wood, wood-aluminium, aluminium, PVC)" and would otherwise
# be listed as four separate products the company supposedly advertises.
_MATERIAL_ONLY = frozenset({
    "aluminium", "aluminum", "alu", "aluminio", "alumínio",
    "pvc", "kunststoff", "holz", "wood", "madera", "madeira",
    "wood-aluminium", "holz-aluminium", "holz-alu",
    "stahl", "steel", "acero", "glass",
})

# Longer keys match as substrings so German compounds and plurals are caught
# ("porcheverglasung" inside "Porcheverglasungen"). Short keys need word
# boundaries, or "tor" would fire inside "Motor" and "pvc" inside a URL.
_LONG_KEY_MIN = 6


def _matchers() -> list[tuple[re.Pattern, str]]:
    out: list[tuple[re.Pattern, str]] = []
    # Canonical spellings first, then synonyms; longest key first so
    # "schiebetür" wins over "tür" on the same text.
    pairs = [(v.lower(), v) for v in PRODUCT_VOCAB] + list(_SYNONYMS.items())
    for key, canon in sorted(pairs, key=lambda kv: -len(kv[0])):
        if len(key) >= _LONG_KEY_MIN:
            out.append((re.compile(re.escape(key), re.IGNORECASE), canon))
        else:
            out.append((re.compile(r"(?<!\w)" + re.escape(key) + r"(?!\w)",
                                   re.IGNORECASE), canon))
    return out


_MATCHERS = _matchers()


def canonical_products(values, limit: int = 6) -> list[str]:
    """Map free-text product names in any language onto `PRODUCT_VOCAB`.

    Accepts a list, or a single string that may itself pack several names
    ("Porcheschließung/Porche-Verschluss"). Unmappable text and bare materials
    are dropped. Result order follows `PRODUCT_VOCAB` so two companies with the
    same families always render identically.
    """
    if isinstance(values, str):
        values = [values]
    if not values:
        return []

    found: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if text.lower() in _MATERIAL_ONLY:
            continue
        for pattern, canon in _MATCHERS:
            if canon in found:
                continue
            if pattern.search(text):
                found.add(canon)

    ordered = [v for v in PRODUCT_VOCAB if v in found]
    return ordered[:limit] if limit else ordered
