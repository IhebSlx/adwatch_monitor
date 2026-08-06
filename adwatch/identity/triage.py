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
VERDICTS = ("wrong_site", "likely_right", "too_thin")

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
- "too_thin": Der Auszug ist zu leer/nichtssagend für ein Urteil (z. B. nur Cookie-Text).

Antworte NUR mit JSON: {"results": [{"id": <id>, "verdict": "...", "what": "...", "clue": "..."}]}
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
    return {"reachable": True,
            "legal_name": facts.get("legal_name"),
            "meta": (facts.get("meta_description") or "")[:200],
            "excerpt": _page_text(html, limit=600)}


def _company_block(c: Company, ev: dict) -> str:
    parts = [f"id={c.id}", f"Firma: {c.name}",
             f"Ort: {c.city or '?'} PLZ: {c.postal_code or '?'}"]
    if c.notes:
        parts.append(f"Notizen: {c.notes[:260]}")
    parts.append(f"Domain: {c.website_domain}")
    parts.append(f"Seite sagt: legal_name={ev.get('legal_name')!r} "
                 f"meta={ev.get('meta')!r}")
    parts.append(f"Auszug: {(ev.get('excerpt') or '')[:420]}")
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
        if v in VERDICTS:
            out[cid] = {"verdict": v, "what": str(r.get("what") or "")[:200],
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

    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        evs = {c.id: _evidence_for(c) for c in chunk}
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

        with SessionLocal() as s:
            for c in judgeable:
                row = s.get(Company, c.id)
                j = verdicts.get(c.id)
                if not j:
                    counts["unjudged"] += 1
                    continue
                counts[j["verdict"]] += 1
                ev = dict(row.identity_evidence or {})
                ev["triage"] = {**j, "at": now.isoformat(), "model": config.ANTHROPIC_MODEL,
                                "domain_at_triage": row.website_domain}
                if j["verdict"] == "wrong_site":
                    # provably-wrong (gate) + diagnosed (triage): the domain is
                    # removed so the strict finder can look for the real one.
                    # Everything is kept in evidence — reversible by hand.
                    row.website_domain = None
                    row.website_source = None
                    row.identity_status = None      # finder's pending_ids picks it up
                elif j["verdict"] == "likely_right":
                    row.identity_status = "needs_review"
                    ev["review_candidate"] = row.website_domain
                # too_thin: stays 'conflict'; ev['triage'] marks it for a JS retry
                row.identity_evidence = ev
                row.identity_checked_at = now
            s.commit()

    out = {"triaged": len(rows), **counts}
    log.info("identity.triage: %s", out)
    return out
