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
import re

from .. import config

# Solarlux's own brand plus the competitors worth knowing about. Given to the
# model as a closed vocabulary so results stay comparable across companies
# instead of free-text noise.
#
# Grouped by WHAT THE BRAND MEANS FOR US, because a flat list answers the wrong
# question. "Builds Sunflex" and "buys Cortizo profiles" are both foreign brands,
# but the first is a company already selling our product category to our customer
# and the second is an ordinary window shop. Only the first is a conquest target.
#
# The old list was 36 brands, every one of them German or pan-European, with no
# Iberian system house at all. Probing 22 Spanish installers recorded as naming
# NO brand: none named a brand the list knew, but 5 named one it could not hear —
# Cortizo four times, plus Technal, Strugal, Sapa, Guardian. Cortizo is the
# largest aluminium system house in Spain. We were deaf to the most common answer
# in the market we are actually working.
BRANDS_DIRECT = (          # our product category: glass folding / sliding / winter garden
    "Sunflex", "Vitrocsa", "Sky-Frame", "Air-Lux", "Nanawall", "Panoramah",
    "Keller", "Lumon", "Seeglass", "Glassspace", "Cortizo Cor Vision",
)
BRANDS_TERRACE = (         # terrace roofs, pergolas, awnings — overlaps our outdoor range
    "Renson", "Brustor", "Corradi", "Pratic", "Gibus", "Weinor", "Markilux",
    "WAREMA", "Griesser", "Lewens", "Klaiber", "Erhardt", "Guhr", "Alukon",
    "Ke Outdoor", "Gaviota", "Llambi", "Persax", "Saxun", "Kettler", "Roma",
)
BRANDS_SYSTEM = (          # profile system houses — tells us their supply chain, not a rival
    "Schüco", "Cortizo", "Technal", "Strugal", "Exlabesa", "Alumafel",
    "Extrugasa", "Hydro", "Sapa", "Indalsu", "Alugom", "Giménez Ganga",
    "Reynaers", "Heroal", "Wicona", "Hueck", "Aluk", "Aliplast", "Ponzio",
    "Kömmerling", "Rehau", "Veka", "Gealan", "Aluprof", "Deceuninck",
    "Internorm", "Josko", "Finstral", "Velfac", "Velux", "Guardian",
)
COMPETITOR_BRANDS = BRANDS_DIRECT + BRANDS_TERRACE + BRANDS_SYSTEM
SOLARLUX_BRANDS = ("Solarlux",)

# What a named brand tells us commercially. Used by the report and the filters so
# "conquest target" is derived from data instead of retyped in every query.
BRAND_TIER = ({b: "direkt" for b in BRANDS_DIRECT}
              | {b: "terrasse" for b in BRANDS_TERRACE}
              | {b: "system" for b in BRANDS_SYSTEM})


def brand_tiers(brands) -> list[str]:
    """The distinct tiers a company's brand list touches, strongest first."""
    seen = {BRAND_TIER.get(b) for b in (brands or [])} - {None}
    return [t for t in ("direkt", "terrasse", "system") if t in seen]


# Brand names that are also ordinary words, place names or surnames. A regex
# would match "Roma" in a Mallorca address and "Keller" in any German basement,
# so these stay LLM-only, where context decides.
_AMBIGUOUS_BRANDS = {"Roma", "Sapa", "Keller", "Hydro", "Guardian", "Metra",
                     "Kettler", "Saxun", "Persax", "Gaviota", "Llambi"}
SCAN_BRANDS = tuple(b for b in COMPETITOR_BRANDS if b not in _AMBIGUOUS_BRANDS)
# Word boundaries, NOT hyphen boundaries. Treating "-" as part of the word looks
# safer and silently loses the most common way both languages write a brand:
# "Sunflex-Anlagen", "Schüco-Fenster", "sistemas Cortizo-Cor". A trailing word
# character still blocks the real false positive ("cortizona" is not Cortizo).
_SCAN_RE = {b: re.compile(rf"(?<!\w){re.escape(b)}(?!\w)", re.I) for b in SCAN_BRANDS}


def fit_floor(brands) -> str | None:
    """The lowest solarlux_fit a company's brands allow, or None for no constraint.

    A company that carries Sunflex sells glass folding walls. That is a fact about
    the company, and it outranks a grade the model inferred from a partial page —
    which is exactly the conflict this resolves. Proymetal trades as "SUNFLEX
    Top-Partner", the scan found Sunflex in its logo strip, and the model still
    said "gering", because the extract it was given stopped before the brand and
    read like a general metalwork shop. Storing both would have put a proven
    conquest target at the bottom of the ranking.

    "terrasse" only lifts to "mittel": it spans genuine overlap (Renson, Brustor
    terrace roofs) and mere adjacency (Markilux awnings, which we do not make), so
    it is not proof they sell our category.
    """
    tiers = brand_tiers(brands)
    if "direkt" in tiers:
        return "hoch"
    return "mittel" if "terrasse" in tiers else None


_FIT_RANK = {"gering": 1, "mittel": 2, "hoch": 3}


def apply_fit_floor(fit: str | None, brands) -> str | None:
    """Raise a graded fit to what the brand evidence proves. Never lowers it, and
    never invents one where the brands say nothing."""
    floor = fit_floor(brands)
    if not floor:
        return fit
    return floor if _FIT_RANK[floor] > _FIT_RANK.get(fit or "", 0) else fit


def scan_brands(text: str) -> list[str]:
    """Brand names present in the page text, found deterministically.

    Brands are a CLOSED vocabulary, so searching for them is a regex problem,
    not a language problem — and the regex is strictly better here. It reads the
    whole page instead of the trimmed extract the model gets, so it still sees
    the two places brands actually live on an installer's site: the "Marcas"
    navigation menu and the partner logo strip. Both were invisible to the model:
    the menu because enrichment strips navigation, the rest because it falls past
    the character budget. Dekovent lost Vitrocsa, Renson and Griesser that way,
    and Proymetal lost Sunflex — the direct competitors, the ones worth most.

    The model still runs; it decides ROLE (partner_of vs passing mention), which
    a regex genuinely cannot. This only guarantees nothing is missed.
    """
    low = text or ""
    return [b for b in SCAN_BRANDS if _SCAN_RE[b].search(low)]

# Shared with ad classification (see adwatch/products.py) so "products
# advertised" and "products offered" are always the same vocabulary and can be
# compared per company.
from ..products import PRODUCT_VOCAB  # noqa: E402  (re-exported on purpose)

_PROMPT = """Du analysierst den Website-Text eines Bauelemente-/Handwerksbetriebs.
Die Antwort hat ZWEI streng getrennte Teile: belegte FAKTEN und eine als solche
gekennzeichnete EINSCHÄTZUNG. Vermische die beiden niemals.

SPRACHE — wichtig:
- Der Website-Text kann in JEDER Sprache sein (Deutsch, Spanisch, Portugiesisch,
  Englisch, Französisch, Italienisch, Niederländisch). Der Bericht ist Deutsch.
- Alle FREITEXT-Felder ("description_de", "employee_hint", "service_area",
  "assessment_de") MÜSSEN auf Deutsch sein — auch wenn die Quelle es nicht ist.
  Übersetze sinngemäß, lasse keine fremdsprachigen Wörter stehen.
  Beispiele: "cerramiento de porche" -> "Terrassenverglasung";
  "carpintería de aluminio" -> "Aluminium-Metallbau" (NICHT "Karpenterie");
  "barandillas" -> "Geländer"; "Un gran equipo" -> "großes Team".
- AUSNAHMEN, die NICHT übersetzt werden: Eigennamen, Marken, Orts- und
  Regionsnamen (Mallorca, Alicante) und die Rechtsform.
- "evidence" bleibt das WÖRTLICHE Originalzitat in der Sprache der Website.

TEIL 1 — FAKTEN (alle Felder bis "evidence"):
- Gib inhaltlich NUR zurück, was im Text steht. Nichts schätzen, nichts annehmen,
  nichts aus Weltwissen ergänzen. (Übersetzen ist erlaubt, Erfinden nicht.)
- Fehlt eine Information: null (bzw. leere Liste). Ein leeres Feld ist besser als eine Vermutung.
- "employee_hint" und "founded_year" nur, wenn der Text sie ausdrücklich nennt
  ("15 Mitarbeiter", "seit 1952", "gegründet 1978"). Sinngemäß auf Deutsch.
- "legal_form": WÖRTLICH so, wie sie im Text steht — NIE übersetzen und NIE durch
  eine deutsche Form ersetzen. Eine spanische "S.L." bleibt "S.L.", eine
  portugiesische "Lda." bleibt "Lda.". Steht keine Rechtsform im Text: null.
  Eine deutsche Rechtsform (GmbH, e.K., KG …) NUR bei einer deutschen Firma.
- "evidence": kurzes wörtliches Zitat (max. 120 Zeichen) als Belegstelle je gefülltem Faktenfeld.

WORAUF ES UNS ANKOMMT — Solarlux baut GLAS-FALTWÄNDE, große Schiebeanlagen,
Wintergärten, Terrassendächer, Glasdächer und Balkonverglasungen. Wir suchen
Betriebe, die so etwas heute schon verkaufen und montieren. Zwei Felder
entscheiden darüber, und sie sind wichtiger als alles andere:

- "competitor_brands": JEDE Systemmarke aus COMPETITOR_LIST, die der Text nennt —
  egal in welcher Rolle: verbaut, vertreibt, ist Partner, zeigt sie in Referenzen
  oder im Showroom. Spanische und portugiesische Betriebe nennen ihr System sehr
  oft ("trabajamos con", "distribuidor oficial de", "sistemas de") — diese
  Nennungen zählen alle. Nur Marken aus der Liste, nichts dazuerfinden.
- "partner_of": Marken, zu denen der Text eine AUSDRÜCKLICHE Partnerschaft
  behauptet — "distribuidor oficial", "concesionario", "Vertragspartner",
  "Top-Partner", "autorizado", "official dealer", "Premium-Partner". Das ist
  mehr als eine Erwähnung: es ist eine vertragliche Bindung. Sonst leere Liste.

TEIL 2 — EINSCHÄTZUNG. Hier DARFST und SOLLST du begründet schlussfolgern.
- "solarlux_fit": Wie gut passt der Betrieb als Verkäufer/Monteur unserer Systeme?
    "hoch"   = baut heute schon große Verglasungen: Glas-Faltwände, Schiebe-
               anlagen, Wintergärten, Terrassen-/Balkonverglasung, Glasdächer —
               oder arbeitet im gehobenen Villen-/Hotelbau mit viel Glas.
    "mittel" = Metallbau, Fenster, Fassade, Alu-/PVC-Bau ohne die obigen
               Produkte: könnte die Kategorie aufnehmen, macht sie aber noch nicht.
    "gering" = anderes Gewerk: nur Rollladen/Markisen, Tore, Zäune, Geländer,
               Innenausbau, Glaserei ohne Bauelemente, reiner Baustoffhandel.
  null NUR wenn der Text nicht erkennen lässt, was der Betrieb macht. "gering"
  ist KEIN Auffangwert für fehlende Information — dafür ist null da.
- "assessment_de", 2-3 kurze Sätze: Größenklasse, Zielkundschaft (Privat /
  Objekt / Handel), Preis- und Qualitätspositionierung, regionale Reichweite und
  die BEGRÜNDUNG des solarlux_fit. Nur was aus dem Text plausibel folgt.
  ERFINDE KEINE ZAHLEN. Unsicherheit kennzeichnen ("wirkt", "dürfte", "eher").
  Keine Personennamen. Zu dünner Text: null.

Antworte AUSSCHLIESSLICH mit diesem JSON (keine Erklärung, kein Markdown):
{
  "description_de": "<1-2 knappe Sätze: was macht die Firma? oder null>",
  "products": [<0-6 Werte aus: PRODUCT_VOCAB>],
  "competitor_brands": [<genannte Marken aus: COMPETITOR_LIST>],
  "partner_of": [<Marken aus COMPETITOR_LIST mit ausdrücklicher Partnerschaft>],
  "own_fabrication": <true wenn eigene Fertigung/Produktion/Werkstatt belegt | false wenn ausdrücklich nur Handel/Vertrieb | null wenn unklar>,
  "has_showroom": <true wenn Ausstellung/Showroom/Musterhaus genannt | null wenn unklar>,
  "installs": <true wenn der Betrieb selbst montiert ("montaje", "instalación", "Montage") | null wenn unklar>,
  "project_focus": [<0-4 aus: "Wohnbau", "Objektbau", "Hotel/Gastro", "Sanierung", "Gewerbe", "Öffentlich">],
  "positioning": "<premium | mittel | budget — nur bei klaren Hinweisen (Luxus/Exklusiv vs. preiswert), sonst null>",
  "certifications": [<Zertifikate/Normen WÖRTLICH, z.B. "ISO 9001", "CE", "Passivhaus", "RAL" — max 6, sonst []>],
  "founded_year": <Jahr als Zahl oder null>,
  "employee_hint": "<Angabe zur Betriebsgröße auf Deutsch oder null>",
  "legal_form": "<Rechtsform WÖRTLICH aus dem Text (GmbH, GmbH & Co. KG, KG, AG, e.K., GbR, S.L., S.L.U., S.A., Lda., Unipessoal Lda., SARL, SRL, BV, Ltd …) oder null>",
  "service_area": "<Region/Umkreis auf Deutsch, falls genannt, sonst null>",
  "mentions_solarlux": <true|false>,
  "evidence": {"<feldname>": "<Zitat>", ...},
  "solarlux_fit": "<hoch | mittel | gering | null>",
  "assessment_de": "<2-3 Sätze Einschätzung wie oben, oder null>"
}

PRODUCT_VOCAB: __PRODUCTS__
COMPETITOR_LIST: __COMPETITORS__

Website-Text:
"""


# ---------------------------------------------------------------------------
# Architekten brauchen einen eigenen Prompt
# ---------------------------------------------------------------------------
# The dealer prompt opens with "Website-Text eines Bauelemente-/Handwerksbetriebs"
# and asks for `products` (from a SELLER's vocabulary), `own_fabrication` and
# `has_showroom`. For an Architekturbüro every one of those is wrong: an architect
# SPECIFIES systems, never sells or fabricates them, so those fields come back
# empty at best and falsely claim a trade business at worst.
#
# What actually decides an architect's value to Solarlux is different in kind:
#   * WHICH systems they specify  -> the conquest signal (stored in
#     competitor_brands, same column, same meaning: whose profiles they use today)
#   * WHETHER their projects even involve large glazing -> solarlux_relevance;
#     an office doing interiors or roads is irrelevant no matter how prestigious
#   * WHAT they build             -> project_focus (Wohnbau/Objektbau/Hotel …),
#     which matters MORE here than for dealers because it is the whole thesis
#   * Generalplaner vs specialist, references, awards/memberships
#
# Market note (Iheb): in SPAIN architects hold real decision power and can
# effectively award the Auftrag, so they are treated as targets; in GERMANY they
# consult. The prompt therefore asks about decision role explicitly.
#
# solarlux_relevance lives in the EINSCHÄTZUNG half, not in the facts half. The
# first version asked for it under "Nur was im Text steht, nichts schätzen" and
# defined "hoch" as "the text mentions large glazing / facades / folding walls".
# No architect writes that about their own work, so the grade was unreachable:
# the first 72 Spanish offices came back 0x hoch, and Costa-del-Sol villa studios
# — the exact target — landed on "gering", a bucket whose own definition
# (interiors, infrastructure, urban planning) did not even cover them. Relevance
# is a JUDGEMENT, so it belongs where judging is allowed, the rubric keys off the
# PROJECT TYPE (observable) rather than glazing vocabulary (never written), and
# it is emitted AFTER project_focus so the grade is conditioned on the facts.
_PROMPT_ARCHITEKT = """Du analysierst den Website-Text eines ARCHITEKTUR- oder PLANUNGSBÜROS.
Die Antwort hat ZWEI streng getrennte Teile: belegte FAKTEN und eine als solche
gekennzeichnete EINSCHÄTZUNG. Vermische die beiden niemals.

WICHTIG — ein Architekturbüro VERKAUFT keine Bauelemente, es PLANT und
SCHREIBT AUS. Behaupte niemals, das Büro verkaufe oder fertige Produkte.

SPRACHE:
- Der Text kann in JEDER Sprache sein. Alle FREITEXT-Felder ("description_de",
  "employee_hint", "service_area", "assessment_de") MÜSSEN Deutsch sein —
  sinngemäß übersetzen, keine fremdsprachigen Wörter stehen lassen.
  Beispiele: "estudio de arquitectura" -> "Architekturbüro";
  "obra nueva" -> "Neubau"; "reforma integral" -> "Komplettsanierung".
- NICHT übersetzt werden: Eigennamen, Marken, Orts-/Regionsnamen, Rechtsform.
- "evidence" bleibt WÖRTLICHES Originalzitat in der Sprache der Website.

TEIL 1 — FAKTEN:
- Nur was im Text steht. Nichts schätzen, nichts aus Weltwissen ergänzen.
- Fehlt eine Information: null bzw. leere Liste.
- "specified_systems": Hersteller/Systemmarken, die als eingesetzt oder geplant
  GENANNT werden (aus: __COMPETITORS__). Nur wenn wirklich genannt.
- "elements": welche Bauelement-Typen in den Projekten vorkommen
  (aus: __PRODUCTS__) — also was GEPLANT wird, nicht was verkauft wird.
- "office_type": "Generalplaner" | "Architekturbüro" | "Fachplaner" |
  "Innenarchitektur" | "Landschaftsplanung" | null — wörtlich am Text belegt.
- "decision_role": "vergibt Aufträge" wenn das Büro die Ausführung steuert —
  Bauleitung, Ausschreibung, Vergabe, Projektsteuerung, schlüsselfertige
  Abwicklung ("dirección de obra", "dirección facultativa", "project
  management", "llave en mano", "obra completa", "chiavi in mano",
  "clé en main"); "empfiehlt" wenn das Büro ausschließlich entwirft und plant;
  null wenn der Text die Leistungsphasen nicht erkennen lässt.
- "reference_scale": kurzer deutscher Hinweis auf Umfang/Größe der Referenzen,
  falls genannt ("über 200 Projekte", "Hotels ab 100 Zimmern") — sonst null.
- "memberships": Kammern, Auszeichnungen, Verbände, Publikationen (max 6).
- "employee_hint": Bürogröße NUR wenn ausdrücklich genannt ("12 Architekten").
- "legal_form": WÖRTLICH wie im Text (S.L.P., S.L., GmbH, Partnerschaft …), sonst null.
- "evidence": kurzes wörtliches Zitat je gefülltem Faktenfeld (max. 120 Zeichen).

TEIL 2 — EINSCHÄTZUNG. Hier DARFST und SOLLST du aus den Projekttypen schließen.

- "solarlux_relevance": Wie gut passt das Portfolio zu Solarlux (große
  Glas-Schiebe- und Faltanlagen, Glasdächer, Wintergärten, Terrassendächer)?
  Urteile nach der ART DER PROJEKTE — kein Büro schreibt seine Fensterflächen
  auf die Website, das Fehlen solcher Wörter ist also KEIN Gegenbeleg:
    "hoch"   = Gebäudehülle mit großen Öffnungen ist plausibel: Villen und
               Einfamilienhäuser im gehobenen Segment, Hotels, Gastronomie,
               Wohnprojekte mit Terrassen/Loggien/Blick, Neubau mit
               offener oder auf die Landschaft ausgerichteter Architektur.
    "mittel" = Hochbau ohne erkennbaren Schwerpunkt auf der Hülle:
               Standard-Wohnungsbau, öffentliche Bauten, Gewerbe, Sanierung.
    "gering" = das Büro plant keine Gebäudehülle: reine Innenarchitektur,
               Möbel, Infrastruktur, Stadt-/Landschaftsplanung, Gutachten,
               reine Bauleitung ohne Entwurf.
  null NUR wenn der Text die Projekttypen gar nicht erkennen lässt. "gering"
  ist KEIN Auffangwert für fehlende Information — dafür ist null da.
- "assessment_de", 2-3 Sätze: Bürogröße, Projekttypen, Anspruch/Preisklasse,
  regionale Reichweite und die BEGRÜNDUNG der solarlux_relevance. Keine
  erfundenen Zahlen.

Antworte AUSSCHLIESSLICH mit diesem JSON (keine Erklärung, kein Markdown):
{
  "description_de": "<1-2 Sätze: was plant das Büro? oder null>",
  "elements": [<0-6 Werte aus: __PRODUCTS__>],
  "specified_systems": [<genannte Marken aus: __COMPETITORS__>],
  "office_type": "<siehe oben oder null>",
  "decision_role": "<vergibt Aufträge | empfiehlt | null>",
  "project_focus": [<0-4 aus: "Wohnbau", "Objektbau", "Hotel/Gastro", "Sanierung", "Gewerbe", "Öffentlich">],
  "reference_scale": "<kurzer Hinweis auf Deutsch oder null>",
  "memberships": [<Kammern/Auszeichnungen/Verbände, max 6>],
  "founded_year": <Jahr als Zahl oder null>,
  "employee_hint": "<Bürogröße auf Deutsch oder null>",
  "legal_form": "<Rechtsform WÖRTLICH oder null>",
  "service_area": "<Region auf Deutsch oder null>",
  "mentions_solarlux": <true|false>,
  "evidence": {"<feldname>": "<Zitat>", ...},
  "solarlux_relevance": "<hoch | mittel | gering | null>",
  "assessment_de": "<2-3 Sätze Einschätzung wie oben, oder null>"
}

PRODUCT_VOCAB: __PRODUCTS__
COMPETITOR_LIST: __COMPETITORS__
"""

OFFICE_TYPES = ("Generalplaner", "Architekturbüro", "Fachplaner",
                "Innenarchitektur", "Landschaftsplanung")
DECISION_ROLES = ("vergibt Aufträge", "empfiehlt")
RELEVANCE = ("hoch", "mittel", "gering")

# Which prompt profile a company gets. Driven by CRM segment, so it needs no
# extra data and cannot drift from the account's classification.
PROFILE_BETRIEB, PROFILE_ARCHITEKT = "betrieb", "architekt"
_ARCHITEKT_SEGMENTS = ("Architekten",)


def profile_for(segment: str | None, sub_segment: str | None = None) -> str:
    """The extraction profile for a company, from its CRM segment."""
    if (segment or "") in _ARCHITEKT_SEGMENTS:
        return PROFILE_ARCHITEKT
    # planners that sit under other segments but behave like architects
    if (sub_segment or "") in ("Generalplaner", "Architekturbüro",
                              "Fachplanungsbüro", "Innenarchitektur",
                              "Landschaftsplaner"):
        return PROFILE_ARCHITEKT
    return PROFILE_BETRIEB


def _prompt(profile: str = PROFILE_BETRIEB) -> str:
    base = _PROMPT_ARCHITEKT if profile == PROFILE_ARCHITEKT else _PROMPT
    return (base.replace("__PRODUCTS__", ", ".join(PRODUCT_VOCAB))
                .replace("__COMPETITORS__", ", ".join(COMPETITOR_BRANDS)))


PROJECT_FOCUS = ("Wohnbau", "Objektbau", "Hotel/Gastro", "Sanierung",
                 "Gewerbe", "Öffentlich")
POSITIONING = ("premium", "mittel", "budget")


def _tri_state(value) -> bool | None:
    """True / False / None, where None means 'the site does not say'.

    Not `bool(value)`: that collapses null into False, which would turn "no
    information about own fabrication" into "confirmed pure trader" on every
    site that simply never mentions its workshop.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "ja", "yes"):
            return True
        if low in ("false", "nein", "no"):
            return False
    return None


def _legal_form_in_text(value, text: str) -> str | None:
    """Keep the legal form only if it ACTUALLY OCCURS in the crawled text.

    The old prompt offered a closed list of German legal forms, so a Spanish
    "S.L." had no valid option and the model substituted the nearest German
    one — ALLKONZEPT S.L. and Aluminios ALSABEN SL were both stored as "e.K.",
    which is not a translation but a false fact about a legal entity.

    The prompt now asks for it verbatim; this is the deterministic backstop, so a
    fabricated form cannot survive even when the model ignores the instruction.
    Punctuation and spacing are ignored ("S.L." matches "SL", "Lda." matches
    "LDA"), because sites write them inconsistently.
    """
    form = (str(value or "")).strip()
    if not form:
        return None
    # Build a pattern from the form's alphanumerics that tolerates the punctuation
    # and spacing sites vary ("S.L." / "SL" / "S. L.") but stays anchored on word
    # boundaries. Squashing the whole TEXT instead would destroy those boundaries,
    # and short forms would match inside ordinary words — "e.K." -> "ek" hits
    # "Projekte" and "perfekt", which is how three Spanish S.L. companies kept a
    # German "e.K." even after the guard was added.
    chars = [c for c in form.lower() if c.isalnum()]
    if not chars:
        return None
    body = r"[\.\s\-]*".join(re.escape(c) for c in chars)
    pattern = rf"(?<![a-z0-9]){body}\.?(?![a-z0-9])"
    if re.search(pattern, (text or "").lower()):
        return form[:40]
    return None


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


def _loads_first_object(raw: str) -> dict:
    """Parse the FIRST complete JSON object in the reply and ignore any trailing
    content.

    A bare json.loads() raises "Extra data" the moment the model appends
    anything after the closing brace — a stray sentence, a second object, a
    repeated answer. That is not a bad extraction, it is a bad parse: the object
    itself is fine and sits right there at the start. It cost Comervia and MODIKO
    their entire enrichment, and the failure was quiet because the caller stores
    the row as "enriched" with the error tucked into a side field.

    raw_decode stops at the end of the first value instead of demanding that the
    whole string be exactly one object.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        if start < 0:
            raise
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
        if not isinstance(obj, dict):
            raise ValueError("model returned a non-object JSON value")
        return obj


def extract_facts(site_text: str, model: str | None = None,
                  profile: str = PROFILE_BETRIEB) -> dict:
    """One LLM call -> validated fact dict. Raises on a missing key/bad response
    so the caller can record an error rather than store junk.

    `profile` selects the prompt: PROFILE_BETRIEB for dealers/fabricators,
    PROFILE_ARCHITEKT for planning offices (see profile_for()). The architect
    profile returns the same STORAGE keys wherever the meaning carries over —
    competitor_brands = which systems they specify, products = which elements
    they plan with — so downstream filters and the dossier need no special case.
    """
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
        # Headroom, not a budget: at 900 the dealer schema (16 fields + an
        # evidence quote each + the Einschätzung) ran out mid-string and the
        # reply came back as unterminated JSON — a truncated answer is a lost
        # company, and output tokens are only billed as produced.
        max_tokens=1600,
        messages=[{"role": "user", "content": _prompt(profile) + text[:9000]}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    if getattr(msg, "stop_reason", None) == "max_tokens":
        # Say so plainly. Truncation surfaces as "Unterminated string" from the
        # JSON parser, which reads like a bad model answer and sends you looking
        # at the prompt instead of at max_tokens — where the fix actually is.
        raise ValueError("model reply hit max_tokens and was cut off mid-JSON — "
                         "raise max_tokens or shorten the schema")
    data = _loads_first_object(raw)

    desc = (data.get("description_de") or "").strip() or None
    # The assessment is explicitly ALLOWED to infer (size class, target customers,
    # positioning), so it is kept apart from the fact fields and stored with its
    # own provenance + lower confidence — the report labels it as an estimate.
    assess = (data.get("assessment_de") or "").strip() or None
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}

    if profile == PROFILE_ARCHITEKT:
        # Map the architect answer onto the SAME storage keys where the meaning
        # carries over, so every existing filter, export and the dossier keep
        # working without a per-segment branch:
        #   elements          -> products           (plans with, not sells)
        #   specified_systems -> competitor_brands  (whose systems they specify)
        #   memberships       -> certifications     (Kammern/Auszeichnungen)
        # own_fabrication / has_showroom are left NULL on purpose: for a planning
        # office they are not "no", they are not applicable, and null is how this
        # app says "not stated".
        one = lambda v, allowed: (str(v).strip() if str(v or "").strip() in allowed else None)  # noqa: E731
        return {
            "description_de": desc[:600] if desc else None,
            "assessment_de": assess[:700] if assess else None,
            "products": _clean_list(data.get("elements"), PRODUCT_VOCAB, 6),
            "competitor_brands": _clean_list(data.get("specified_systems"),
                                             COMPETITOR_BRANDS, 12),
            "certifications": [str(c).strip()[:40] for c in
                               (data.get("memberships") or []) if str(c).strip()][:6],
            "project_focus": _clean_list(data.get("project_focus"), PROJECT_FOCUS, 4),
            "founded_year": _coerce_year(data.get("founded_year")),
            "employee_hint": ((data.get("employee_hint") or "").strip() or None),
            "legal_form": _legal_form_in_text(data.get("legal_form"), text),
            "service_area": ((data.get("service_area") or "").strip() or None),
            "mentions_solarlux": bool(data.get("mentions_solarlux")),
            "own_fabrication": None, "has_showroom": None, "positioning": None,
            # architect-only facts
            "solarlux_relevance": one(data.get("solarlux_relevance"), RELEVANCE),
            "office_type": one(data.get("office_type"), OFFICE_TYPES),
            "decision_role": one(data.get("decision_role"), DECISION_ROLES),
            "reference_scale": ((data.get("reference_scale") or "").strip() or None),
            "profile": PROFILE_ARCHITEKT,
            "evidence": {str(k)[:40]: str(v)[:200] for k, v in list(evidence.items())[:12]},
            "llm_model": use_model,
        }

    return {
        "description_de": desc[:600] if desc else None,
        "assessment_de": assess[:700] if assess else None,
        "products": _clean_list(data.get("products"), PRODUCT_VOCAB, 6),
        "founded_year": _coerce_year(data.get("founded_year")),
        "employee_hint": ((data.get("employee_hint") or "").strip() or None),
        "legal_form": _legal_form_in_text(data.get("legal_form"), text),
        "service_area": ((data.get("service_area") or "").strip() or None),
        "mentions_solarlux": bool(data.get("mentions_solarlux")),
        "competitor_brands": _clean_list(data.get("competitor_brands"), COMPETITOR_BRANDS, 12),
        # Qualification attributes. The booleans stay TRI-STATE on purpose: null
        # means the site does not say, which is different from a stated "no" — a
        # dealer with no fabrication and a dealer whose site is silent about it
        # must not be scored the same.
        "certifications": [str(c).strip()[:40] for c in (data.get("certifications") or [])
                           if str(c).strip()][:6],
        "own_fabrication": _tri_state(data.get("own_fabrication")),
        "has_showroom": _tri_state(data.get("has_showroom")),
        "installs": _tri_state(data.get("installs")),
        "project_focus": _clean_list(data.get("project_focus"), PROJECT_FOCUS, 4),
        "positioning": (str(data.get("positioning")).strip().lower()
                        if str(data.get("positioning") or "").strip().lower()
                        in POSITIONING else None),
        # An explicit dealership ("distribuidor oficial de CORTIZO") is a
        # contractual tie, not a passing mention — a much stronger signal about
        # who supplies this company today, and who we would have to displace.
        "partner_of": _clean_list(data.get("partner_of"), COMPETITOR_BRANDS, 6),
        "evidence": {str(k)[:40]: str(v)[:200] for k, v in list(evidence.items())[:12]},
        # Judged, not quoted — same rule as the architects' solarlux_relevance.
        "solarlux_fit": (str(data.get("solarlux_fit")).strip()
                         if str(data.get("solarlux_fit") or "").strip() in RELEVANCE else None),
        "profile": PROFILE_BETRIEB,
        "llm_model": use_model,
    }
