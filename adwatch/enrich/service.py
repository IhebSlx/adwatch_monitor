"""Enrichment orchestrator — runs the three tiers for ONE company and persists
the result with per-field provenance.

Order is cheapest-first, and it stops as soon as a website is settled:

  website already in SAP ─────────────► authoritative, kept as-is (never replaced)
  else Tier 0 email domain (free) ───┐
  else Tier 1 Serper search (~$0.001)┴─► candidate(s) ──► validate.validate_site
                                                            ├─ proven  → accept
                                                            └─ not     → needs_review
  then Tier 2: crawl text ──► extract.extract_facts (1 LLM call) ──► fields

Guarantees:
  * SAP / human-entered data is never overwritten — a website is only WRITTEN
    when the column was empty.
  * A discovered website is only auto-accepted on a deterministic match
    (phone / PLZ+Straße / name), otherwise a human reviews it.
  * Every stored value carries {source, confidence, evidence, fetched_at}.
  * Idempotent: re-running refreshes facts without re-deciding a settled website.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Company, CompanyEnrichment
from . import extract, fetchpage, validate, website_finder
from .domains import domain_from_email, normalize_domain, salvage_domain

# Confidence per acceptance route — stored, and used by the UI to sort the
# review queue. Deterministic matches are near-certain; 'sap' is definitional.
_CONFIDENCE = {"sap": 1.0, "phone": 0.97, "plz_street": 0.95, "plz_name": 0.9,
               "domain_plus_name": 0.8, "manual": 1.0}

_COMPANY_KEYS = ("id", "name", "phone", "postal_code", "street", "city", "country",
                 "email", "website_domain")


def _company_dict(c: Company) -> dict:
    return {k: getattr(c, k, None) for k in _COMPANY_KEYS}


def _prov(source: str, confidence: float | None = None, evidence: str | None = None) -> dict:
    return {"source": source, "confidence": confidence,
            "evidence": (evidence or None), "fetched_at": dt.datetime.utcnow().isoformat(timespec="seconds")}


def _resolve_website(comp: dict, allow_search: bool) -> dict:
    """Settle which website belongs to this company.

    Returns {domain, source, validated_by, candidates, bundle, status}. `domain`
    is None when nothing could be proven; `candidates` then feeds the review
    queue. `bundle` is the crawl (reused for extraction) when one succeeded."""
    existing = normalize_domain(comp.get("website_domain")) or salvage_domain(comp.get("website_domain"))
    if existing and normalize_domain(comp.get("website_domain")):
        # Already known and well-formed -> authoritative. Crawl only to extract.
        bundle = fetchpage.page_bundle(existing)
        return {"domain": existing, "source": "sap", "validated_by": "sap",
                "candidates": [], "bundle": bundle, "status": "ok"}

    tried: list[dict] = []

    def _try(candidates: list[dict]) -> dict | None:
        """Crawl + validate each candidate; first proven one wins."""
        for cand in candidates:
            dom = cand.get("domain")
            if not dom or dom in {t.get("domain") for t in tried}:
                if not dom:
                    tried.append(cand)     # keep e.g. the serper_error marker for the audit trail
                continue
            bundle = fetchpage.page_bundle(dom)
            result = validate.validate_site(comp, dom, (bundle or {}).get("text"))
            tried.append({**cand, "validated": result["ok"], "matched_by": result["matched_by"],
                          "signals": result["signals"], "reachable": bundle is not None})
            if result["ok"]:
                return {"domain": dom, "source": cand["origin"], "validated_by": result["matched_by"],
                        "candidates": tried, "bundle": bundle, "status": "ok"}
        return None

    # Stage 1 — free local candidates: a salvaged SAP typo, then the email domain.
    local: list[dict] = []
    if existing:
        local.append({"domain": existing, "origin": "sap_salvaged"})
    derived = domain_from_email(comp.get("email"))
    if derived and derived not in [c["domain"] for c in local]:
        local.append({"domain": derived, "origin": "email_domain"})
    hit = _try(local)
    if hit:
        return hit

    # Stage 2 — web search. Runs whenever stage 1 didn't PROVE a site — including
    # when an email-domain candidate existed but failed validation (e.g. the
    # contact address sits on a supplier's domain): that company still deserves
    # a search before being parked for review.
    if allow_search:
        search_hits: list[dict] = []
        try:
            for h in website_finder.search_candidates(
                    comp.get("name") or "", comp.get("city"), comp.get("country") or "DE"):
                search_hits.append({"domain": h["domain"], "origin": "serper",
                                    "title": h.get("title"), "snippet": h.get("snippet")})
        except Exception as exc:  # noqa: BLE001 — a failed search is not a failed company
            tried.append({"domain": None, "origin": "serper_error", "error": str(exc)[:200]})
        hit = _try(search_hits)
        if hit:
            return hit

    # Only PLAUSIBLY-RELATED failures deserve a human's time. A candidate that
    # came from the company's own data (its email / its SAP typo) is always
    # review-worthy; a search hit is only review-worthy when at least a name
    # signal connects it to the company. Unrelated portals a search coughed up
    # (all signals false) are recorded for the audit trail but do NOT put the
    # company into the review queue — that's an honest "no website found".
    def _review_worthy(t: dict) -> bool:
        if not t.get("domain"):
            return False
        if t.get("origin") in ("email_domain", "sap_salvaged"):
            return True
        sig = t.get("signals") or {}
        return bool(sig.get("name_in_text") or sig.get("name_in_domain"))

    status = "needs_review" if any(_review_worthy(t) for t in tried) else "no_website_found"
    return {"domain": None, "source": None, "validated_by": None,
            "candidates": tried, "bundle": None, "status": status}


def enrich_company(company_id: int, allow_search: bool = True, allow_llm: bool = True) -> dict:
    """Enrich one company. Returns a small summary for the job log:
    {status, website, website_source, validated_by, fields_found, error?}."""
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if not c:
            raise ValueError("Company not found")
        comp = _company_dict(c)
        had_website = bool(normalize_domain(comp.get("website_domain")))

    site = _resolve_website(comp, allow_search=allow_search)
    fields: dict = {}
    provenance: dict = {}
    error: str | None = None
    status = site["status"]

    # ---- Tier 2: facts from the site's own text -------------------------------
    bundle = site.get("bundle")
    if site["domain"] and bundle and (bundle.get("text") or "").strip():
        if allow_llm:
            try:
                facts = extract.extract_facts(bundle["text"])
                ev = facts.pop("evidence", {}) or {}
                model = facts.pop("llm_model", None)
                for key, value in facts.items():
                    if value in (None, [], ""):
                        continue
                    fields[key] = value
                    if key == "assessment_de":
                        # an INFERENCE, not an extracted fact: separate source and a
                        # markedly lower confidence, so the report (and any future
                        # consumer) can present it as an estimate rather than a quote
                        provenance[key] = _prov("website+llm-einschaetzung", 0.5,
                                                "begründete Einschätzung, keine belegte Angabe")
                    else:
                        provenance[key] = _prov("website+llm", 0.85, ev.get(key))
                fields["_llm_model"] = model
                status = "enriched"
            except Exception as exc:  # noqa: BLE001 — keep the website win even if extraction fails
                error = f"extraction failed: {exc}"[:300]
                status = "enriched" if had_website or site["domain"] else status
        else:
            status = "enriched"
    elif site["domain"] and not bundle:
        error = f"website unreachable: {site['domain']}"
        status = "error" if had_website else site["status"]

    # A settled website must never leave the internal 'ok' marker behind —
    # Company.enrichment_status only knows {none, enriched, needs_review,
    # no_website_found, error}. Reachable-but-textless sites (e.g. pure-JS
    # pages our crawler can't render) land here: website kept, no facts.
    if site["domain"] and status == "ok":
        status = "enriched"
        if not (bundle and (bundle.get("text") or "").strip()):
            error = error or f"no text extracted from {site['domain']} (JS-only page?)"

    # ---- persist -------------------------------------------------------------
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if not c:
            raise ValueError("Company disappeared mid-enrichment")

        # A website is only WRITTEN into master data when the column was empty —
        # with ONE principled exception: a stored value that is objectively
        # MALFORMED (normalize_domain rejects it, e.g. 'http.terrassen-freye.de',
        # 'www.x .de') may be REPAIRED, but only by a domain that passed the
        # deterministic validation gate. A working SAP value is never replaced.
        raw = (c.website_domain or "").strip()
        _hard_proof = site["validated_by"] in ("phone", "plz_street", "plz_name",
                                               "domain_plus_name", "manual")
        if site["domain"] and (not raw or (normalize_domain(raw) is None and _hard_proof)):
            note = (f"repaired malformed value {raw!r}, validated by {site['validated_by']}"
                    if raw else f"validated by {site['validated_by']}")
            c.website_domain = site["domain"]
            provenance["website_domain"] = _prov(
                site["source"] or "unknown", _CONFIDENCE.get(site["validated_by"] or "", 0.5),
                note)

        # Enrichment-owned columns: safe to refresh on every run.
        if fields.get("description_de"):
            c.description = fields["description_de"]
        if fields.get("products"):
            c.products = fields["products"]
        if fields.get("founded_year"):
            c.founded_year = fields["founded_year"]
        if fields.get("employee_hint"):
            c.employee_hint = fields["employee_hint"]
        c.enrichment_status = status

        row = s.scalar(select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id))
        if row is None:
            row = CompanyEnrichment(company_id=company_id)
            s.add(row)
        merged = dict(row.fields or {})
        merged.update(fields)
        row.fields = merged
        merged_prov = dict(row.provenance or {})
        merged_prov.update(provenance)
        row.provenance = merged_prov
        # A human's decision outranks any automatic label: once a website was
        # approved in the review queue, a later automatic pass (which now simply
        # sees "website present" and would relabel it 'sap') must not overwrite
        # that provenance. Same rule identity uses for manually-set pages.
        if row.website_source != "manual":
            row.website_source = site["source"] or row.website_source
            row.website_validated_by = site["validated_by"] or row.website_validated_by
        row.website_candidates = site["candidates"] or None
        row.status = status
        row.error = error
        row.llm_model = fields.get("_llm_model") or row.llm_model
        row.enriched_at = dt.datetime.utcnow()
        s.commit()

    return {"status": status, "website": site["domain"],
            "website_source": site["source"], "validated_by": site["validated_by"],
            "fields_found": len([k for k in fields if not k.startswith("_")]),
            "candidates": len([c for c in site["candidates"] if c.get("domain")]),
            "error": error}


def accept_candidate(company_id: int, domain: str) -> dict:
    """Human approves one of the review-queue candidates as the real website,
    then re-enriches from it. Recorded as source='manual' (confidence 1.0), so a
    later automatic run never second-guesses it."""
    dom = normalize_domain(domain) or salvage_domain(domain)
    if not dom:
        raise ValueError("Not a valid domain")
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if not c:
            raise ValueError("Company not found")
        c.website_domain = dom
        row = s.scalar(select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id))
        if row is None:
            row = CompanyEnrichment(company_id=company_id)
            s.add(row)
        row.website_source = "manual"
        row.website_validated_by = "manual"
        prov = dict(row.provenance or {})
        prov["website_domain"] = _prov("manual", 1.0, "approved in the review queue")
        row.provenance = prov
        s.commit()
    return enrich_company(company_id, allow_search=False, allow_llm=True)


def reject_candidates(company_id: int) -> None:
    """Human says none of the candidates is right — clear them so the row leaves
    the review queue and isn't re-proposed."""
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if c:
            c.enrichment_status = "no_website_found"
        row = s.scalar(select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id))
        if row:
            row.website_candidates = None
            row.status = "no_website_found"
        s.commit()


def get_enrichment(company_id: int) -> dict | None:
    with SessionLocal() as s:
        row = s.scalar(select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id))
        if not row:
            return None
        return {"company_id": row.company_id, "fields": row.fields or {},
                "provenance": row.provenance or {}, "website_source": row.website_source,
                "website_candidates": row.website_candidates or [],
                "website_validated_by": row.website_validated_by, "status": row.status,
                "error": row.error, "llm_model": row.llm_model,
                "enriched_at": row.enriched_at.isoformat(timespec="minutes") if row.enriched_at else None}
