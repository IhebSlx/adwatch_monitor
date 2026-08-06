"""Haiku triage for identity CONFLICTS — diagnose, never verify.

A conflict verdict says the site does not carry the company's own address or
name. It does not say WHY, and the three whys need opposite actions:

    wrong_site     a manufacturer's portal (technal.com on several dealer
                   rows), a directory, a namesake       -> discard the domain,
                                                           search for the real one
    likely_right   the correct dealer site, but our row has no postcode and the
                   name token is not distinctive        -> human review, with
                                                           the reasoning attached
    too_thin       JS-heavy page yields almost no text  -> retry queue

The design rule, agreed with Iheb and non-negotiable after the migration
incident: **Haiku diagnoses, it never verifies.** Its verdict only ROUTES the
row; the deterministic gate (enrich/validate.py) stays the only writer of
'verified'. A persuasive-but-wrong LLM answer can therefore never contaminate
identity, and every routing decision is stored with its reasoning so it can be
audited or reversed mechanically.

Cost: companies are judged in BATCHES of ~8 per Haiku call (~$0.15 for all 191
Spanish conflicts) after a free homepage re-fetch for current evidence. The
colleague's research notes ride along — "Schüco + Drutex, eigener
Ausstellungsraum" against the site's own text is often the decisive clue.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import select

from .. import config
from ..db import SessionLocal
from ..enrich import site_facts
from ..enrich.fetchpage import _fetch
from ..models import Company
from .website_source import _page_text

log = logging.getLogger("adwatch.identity.triage")

BATCH = 8
VERDICTS = ("wrong_site", "likely_right", "stammdaten_falsch", "too_thin")

# Below this self-reported confidence a "likely_right" is downgraded to too_thin.
# An LLM asked to choose between four labels will always pick one; the confidence
# is what separates "I can quote the town name" from "it looks vaguely plausible",
# and only the former is worth a human's attention in the review queue.
MIN_CONFIDENCE = 0.6

# Brands worth cross-checking between the colleague's notes and the site text.
# This is DETERMINISTIC corroboration: if the researcher wrote "Schüco + Drutex"
# and the site says Schüco, that is real evidence independent of the LLM's
# opinion — and it is free. Kept in sync with market reality, not exhaustive.
# canonical name -> spellings seen in the wild. Variants are essential, not
# cosmetic: the researcher typed "Schueco" while the Spanish site writes "Schüco",
# and a naive substring match found no overlap at all — silently discarding the
# strongest free evidence the triage has.
_BRAND_VARIANTS: dict[str, tuple[str, ...]] = {
    "Schüco": ("schuco", "schueco",),
    "Cortizo": ("cortizo",),
    "Technal": ("technal",),
    "Reynaers": ("reynaers",),
    "Sunflex": ("sunflex",),
    "Vitrocsa": ("vitrocsa",),
    "Renson": ("renson",),
    "Finstral": ("finstral",),
    "Sky-Frame": ("skyframe", "sky frame"),
    "Kömmerling": ("kommerling", "koemmerling"),
    "Veka": ("veka",),
    "Rehau": ("rehau",),
    "Velux": ("velux",),
    "Drutex": ("drutex",),
    "Strugal": ("strugal",),
    "Aluprof": ("aluprof",),
    "Wicona": ("wicona",),
    "Hueck": ("hueck",),
    "Solarlux": ("solarlux",),
    "Brustor": ("brustor",),
    "Weinor": ("weinor",),
    "Markilux": ("markilux",),
}


def _fold(text: str | None) -> str:
    """Lowercase, umlauts collapsed both ways, hyphens dropped — so 'Schüco',
    'Schueco' and 'Schuco' all become the same needle."""
    low = (text or "").lower()
    for a, b in (("ü", "u"), ("ue", "u"), ("ö", "o"), ("oe", "o"),
                 ("ä", "a"), ("ae", "a"), ("ß", "ss")):
        low = low.replace(a, b)
    return low.replace("-", "").replace("_", "")


def _brands_in(text: str | None) -> set[str]:
    hay = _fold(text)
    return {canon for canon, variants in _BRAND_VARIANTS.items()
            if any(_fold(v) in hay for v in variants)}


def _brand_overlap(notes: str | None, page_text: str | None) -> list[str]:
    """Brands named in BOTH the colleague's research and the site itself.

    Strong, LLM-independent corroboration that a site belongs to the researched
    firm: a random namesake in another province does not happen to carry the same
    profile systems the researcher wrote down.
    """
    return sorted(_brands_in(notes) & _brands_in(page_text))

_PROMPT = """Du prüfst, ob Websites wirklich zu den genannten spanischen Firmen gehören.
Für jede Firma bekommst du: unsere Stammdaten (Name, Ort, ggf. PLZ), Recherche-Notizen
eines Kollegen, und was die Website über sich selbst sagt (Titel, Beschreibung, Auszug).
Eine automatische Prüfung hat bereits festgestellt, dass KEIN harter Beweis (Telefon,
PLZ+Straße, PLZ+Name) auf der Seite steht.

Entscheide je Firma GENAU EINES:
- "wrong_site": Die Seite gehört erkennbar jemand anderem (Herstellerportal wie Technal/
  Cortizo/Schüco, Verzeichnis, Namensvetter, andere Branche/Region). Nenne in "what",
  was die Seite tatsächlich ist.
- "likely_right": Inhalt passt klar zur Firma (gleicher Ort, gleiche Marken wie in den
  Notizen, passendes Gewerk). Zitiere in "clue" den entscheidenden Hinweis WÖRTLICH.
- "stammdaten_falsch": Die Seite gehört sehr wahrscheinlich der Firma, aber UNSERE
  Stammdaten passen nicht (anderer Ort/PLZ als auf der Seite — Umzug, Filiale, Tippfehler).
  Nenne in "what" die Adresse, die auf der Seite steht.
- "too_thin": Der Auszug ist zu leer/nichtssagend für ein Urteil (z. B. nur Cookie-Text).

Sei streng: "likely_right" nur, wenn du einen konkreten, zitierbaren Beleg hast
(Ortsname, Marke aus den Notizen, Gewerk). Bei Zweifel "too_thin".
Gib "confidence" von 0.0 bis 1.0 an, wie sicher du bist.

Antworte NUR mit JSON:
{"results": [{"id": <id>, "verdict": "...", "confidence": 0.0, "what": "...", "clue": "..."}]}
Firmen:
"""


def _evidence_for(c: Company) -> dict:
    """Free homepage re-fetch: title/meta/legal_name plus a short text excerpt."""
    dom = (c.website_domain or "").strip()
    got = _fetch(f"https://{dom}", wall_clock=15) or _fetch(f"http://{dom}", wall_clock=15)
    if not got:
        return {"reachable": False}
    html = got[0]
    facts = {}
    try:
        facts = site_facts.extract(html, base_url=got[1])
    except Exception:  # noqa: BLE001
        pass
    excerpt = _page_text(html, limit=900)
    return {"reachable": True,
            "legal_name": facts.get("legal_name"),
            "meta": (facts.get("meta_description") or "")[:200],
            "phone_on_site": facts.get("phone"),
            "excerpt": excerpt,
            # deterministic, LLM-independent corroboration
            "brand_overlap": _brand_overlap(c.notes, f"{excerpt} {facts.get('meta_description') or ''}")}


def _company_block(c: Company, ev: dict) -> str:
    parts = [f"id={c.id}", f"Firma: {c.name}",
             f"Ort: {c.city or '?'} PLZ: {c.postal_code or '?'}"]
    if c.notes:
        parts.append(f"Notizen: {c.notes[:260]}")
    parts.append(f"Domain: {c.website_domain}")
    parts.append(f"Seite sagt: legal_name={ev.get('legal_name')!r} "
                 f"meta={ev.get('meta')!r}")
    if ev.get("brand_overlap"):
        parts.append("Marken in Notizen UND auf der Seite: "
                     + ", ".join(ev["brand_overlap"]))
    parts.append(f"Auszug: {(ev.get('excerpt') or '')[:700]}")
    return "\n".join(parts)


def _judge_batch(blocks: list[str]) -> dict[int, dict]:
    from anthropic import Anthropic
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=config.ANTHROPIC_MODEL, max_tokens=1200,
        messages=[{"role": "user",
                   "content": _PROMPT + "\n\n---\n".join(blocks)}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    out: dict[int, dict] = {}
    for r in data.get("results", []):
        try:
            cid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        v = str(r.get("verdict") or "").strip()
        if v not in VERDICTS:
            continue
        try:
            conf = float(r.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.5
        out[cid] = {"verdict": v, "confidence": round(max(0.0, min(conf, 1.0)), 2),
                    "what": str(r.get("what") or "")[:200],
                    "clue": str(r.get("clue") or "")[:200]}
    return out


def run(lead_source: str | None = None, limit: int = 250) -> dict:
    """Triage every conflict. Routes rows, never verifies:

      wrong_site    -> domain cleared (kept in evidence), so the strict website
                       finder can search for the real one; identity_status stays
                       accurate ('not_found' until the finder decides)
      likely_right  -> 'needs_review' with the clue attached — a 5-second human
                       confirm instead of a site visit
      too_thin      -> stays 'conflict', marked for a JS-capable retry
    """
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY fehlt — für die Triage nötig")
    with SessionLocal() as s:
        stmt = (select(Company).where(Company.identity_status == "conflict")
                .order_by(Company.beleg_sum.desc(), Company.id).limit(limit))
        if lead_source:
            stmt = stmt.where(Company.lead_source == lead_source)
        rows = list(s.scalars(stmt))

    counts = {v: 0 for v in VERDICTS}
    counts.update({"unreachable": 0, "unjudged": 0})
    now = dt.datetime.utcnow()

    from concurrent.futures import ThreadPoolExecutor

    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        # Fetch the batch's evidence in parallel — 8 sequential HTTP round-trips
        # per batch dominated the runtime and none of them depend on each other.
        with ThreadPoolExecutor(max_workers=BATCH) as pool:
            evs = dict(zip([c.id for c in chunk],
                           pool.map(_evidence_for, chunk)))
        judgeable = [c for c in chunk if evs[c.id].get("reachable")]
        for c in chunk:
            if not evs[c.id].get("reachable"):
                counts["unreachable"] += 1
        verdicts: dict[int, dict] = {}
        if judgeable:
            try:
                verdicts = _judge_batch([_company_block(c, evs[c.id]) for c in judgeable])
            except Exception:  # noqa: BLE001 — one bad batch must not kill the run
                log.exception("triage batch failed (companies %s..)", judgeable[0].id)
                # Retry each company alone: a single unparseable response
                # otherwise costs all 8 their verdict.
                for c in judgeable:
                    try:
                        verdicts.update(_judge_batch([_company_block(c, evs[c.id])]))
                    except Exception:  # noqa: BLE001
                        log.warning("triage: company %s unjudged", c.id)

        with SessionLocal() as s:
            for c in judgeable:
                row = s.get(Company, c.id)
                j = verdicts.get(c.id)
                if not j:
                    counts["unjudged"] += 1
                    continue
                overlap = evs[c.id].get("brand_overlap") or []
                # A confident-sounding label with nothing quotable behind it is
                # not worth a human's time. A brand named in BOTH the research
                # notes and the site is hard evidence, so it substitutes for
                # confidence; otherwise the model must be sure of itself.
                # .get: a verdict stored before confidence existed must not crash
                # the routing; an unstated confidence is treated as middling.
                conf = j.get("confidence", 0.5)
                if (j["verdict"] == "likely_right"
                        and conf < MIN_CONFIDENCE and not overlap):
                    j = {**j, "verdict": "too_thin",
                         "what": f"zu unsicher ({conf}) ohne Markenbeleg"}
                counts[j["verdict"]] += 1
                ev = dict(row.identity_evidence or {})
                ev["triage"] = {**j, "at": now.isoformat(), "model": config.ANTHROPIC_MODEL,
                                "domain_at_triage": row.website_domain,
                                "brand_overlap": overlap}
                if j["verdict"] == "wrong_site":
                    # provably-wrong (gate) + diagnosed (triage): the domain is
                    # removed so the strict finder can look for the real one.
                    # Everything is kept in evidence — reversible by hand.
                    row.website_domain = None
                    row.website_source = None
                    row.identity_status = None      # finder's pending_ids picks it up
                elif j["verdict"] in ("likely_right", "stammdaten_falsch"):
                    # Both mean "probably theirs, not provable by our data" —
                    # exactly what the review queue is for. stammdaten_falsch
                    # additionally tells the human WHY the gate could not pass:
                    # the address on the site differs from ours.
                    row.identity_status = "needs_review"
                    ev["review_candidate"] = row.website_domain
                # too_thin: stays 'conflict'; ev['triage'] marks it for a JS retry
                row.identity_evidence = ev
                row.identity_checked_at = now
            s.commit()

    out = {"triaged": len(rows), **counts}
    log.info("identity.triage: %s", out)
    return out
