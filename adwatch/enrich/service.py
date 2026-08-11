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
                 "email", "website_domain",
                 # segment/sub_segment select the extraction PROFILE (a planning
                 # office needs different questions than a fabricator)
                 "segment", "sub_segment")


def _company_dict(c: Company) -> dict:
    return {k: getattr(c, k, None) for k in _COMPANY_KEYS}


def _prov(source: str, confidence: float | None = None, evidence: str | None = None) -> dict:
    return {"source": source, "confidence": confidence,
            "evidence": (evidence or None), "fetched_at": dt.datetime.utcnow().isoformat(timespec="seconds")}


def _merge_brands(*lists) -> list[str]:
    """Union of the brand lists, order-stable, deduped case-insensitively."""
    out, seen = [], set()
    for lst in lists:
        for b in (lst or []):
            key = str(b).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(str(b).strip())
    return out


# Signals strong enough to write a website into master data on their own — the
# same four identity/find_website.PROVEN accepts, and the three ONBOARDING
# promises a colleague ("eigene Telefonnummer, PLZ+Straße, PLZ+Name").
_PROVEN_SIGNALS = ("domain_in_name", "phone", "plz_street", "plz_name", "manual")

# Origins that are themselves corroboration, because the candidate came out of
# our OWN master data rather than a search engine's guess.
_CORROBORATED_ORIGINS = ("sap", "sap_salvaged", "email_domain")


def _review_worthy(t: dict) -> bool:
    """Is this candidate worth a human's time?

    Only PLAUSIBLY-RELATED failures are. A candidate that came from the company's
    own data (its e-mail, its SAP typo) always is; a search hit only when a name
    signal connects it to the company. Unrelated portals a search coughed up are
    recorded for the audit trail but do NOT queue the company — that is an
    honest "no website found".

    Module-level and shared on purpose: this decides BOTH whether the company is
    queued and which candidate is shown. While the two were separate rules, seven
    of nine Spanish review items reached the Prüfen tab with an empty Kandidat
    column — a decision with nothing to decide.
    """
    if not t.get("domain"):
        return False
    if t.get("origin") in ("email_domain", "sap_salvaged"):
        return True
    sig = t.get("signals") or {}
    return bool(sig.get("name_in_text") or sig.get("name_in_domain"))


def _accepts(origin: str | None, matched_by: str | None) -> bool:
    """May this (origin, signal) pair be written into master data, or does a
    human decide?

    `domain_plus_name` means only that the domain shares a token with the company
    name. identity/find_website deliberately routes exactly that to review, while
    this module used to accept it — two standards for one question. Measured on
    the first 20 Spanish companies it produced "Montajes Portico Balear SL" ->
    portsdebalears.com and "+ PLUS" -> pressingplus.com, both wrong, and both
    then enriched with a stranger's facts.

    The origin is the second half of the question and the reason this is not just
    a shorter list: the company's own e-mail sitting on that domain is
    corroboration a search result does not have. So email_domain +
    domain_plus_name stays accepted (aluminioscerratosa.com for "Aluminios
    Cerratosa"), serper + domain_plus_name does not.
    """
    if matched_by in _PROVEN_SIGNALS:
        return True
    return bool(matched_by) and origin in _CORROBORATED_ORIGINS


def _resolve_website(comp: dict, allow_search: bool) -> dict:
    """Settle which website belongs to this company.

    Returns {domain, source, validated_by, candidates, bundle, status}. `domain`
    is None when nothing could be proven; `candidates` then feeds the review
    queue. `bundle` is the crawl (reused for extraction) when one succeeded."""
    existing = normalize_domain(comp.get("website_domain")) or salvage_domain(comp.get("website_domain"))
    if existing and normalize_domain(comp.get("website_domain")):
        # Already known and well-formed -> authoritative, never replaced. But the
        # page is being crawled anyway, so validate it while we are here: a CRM
        # domain is a CLAIM (ONBOARDING: "Eine Website gilt erst als 'diese
        # Firma', wenn ein harter Beweis vorliegt"), and 22.794 rows sit at
        # 'unverified' for want of anyone checking. Costs nothing extra and turns
        # a claim into evidence — or leaves it a claim, which is also the truth.
        bundle = fetchpage.page_bundle(existing)
        proof = validate.validate_site(comp, existing, (bundle or {}).get("text"))
        return {"domain": existing, "source": "sap",
                "validated_by": proof["matched_by"] or "sap",
                "proved_stored_domain": bool(proof["matched_by"]),
                "signals": proof["signals"],
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
            if result["ok"] and _accepts(cand["origin"], result["matched_by"]):
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

    # The SAME predicate decides the status and picks what the human is shown.
    # They used to be two rules: _review_worthy queued the company (name appears
    # on the page or in the domain), while the picker only offered candidates
    # carrying a match signal. Seven of nine Spanish review items therefore
    # landed in the queue with an empty Kandidat column — a decision with nothing
    # to decide.
    review = next((t for t in tried if _review_worthy(t)), None)
    status = "needs_review" if review else "no_website_found"
    return {"domain": None, "source": None, "validated_by": None,
            "review_candidate": (review or {}).get("domain"),
            "candidates": tried, "bundle": None, "status": status}


def derive_domain(company_id: int) -> dict:
    """FREE pre-pass: settle a website from the company's OWN data only — no web
    search, no LLM. Writes it only when the validation gate proved it.

    Why this exists as its own step: the identity check's only free and
    authoritative tier crawls `Company.website_domain` looking for the company's
    self-declared Facebook link (see identity.resolver.run_identity_check — every
    one of the hard-`locked` identities came from there). Enrichment is what
    fills that column. Populating it BEFORE the identity check therefore turns
    paid identity resolutions into free hard-locks, and 1,244 companies in the
    current base have no website but do have a non-freemail email address.

    A miss writes NOTHING — not even a status. Serper has not been tried yet, so
    'no website found' would be a verdict this pass has no right to reach; that
    call belongs to the full enrichment run. On a hit, only `website_domain` is
    written and `enrichment_status` is deliberately left alone, because no facts
    were extracted and the company still needs the real run.

    Returns {status, website?, validated_by?, source?} where status is one of
    already_had | domain_found | no_domain_derived.
    """
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if not c:
            raise ValueError("Company not found")
        comp = _company_dict(c)
    if normalize_domain(comp.get("website_domain")):
        return {"status": "already_had", "website": normalize_domain(comp["website_domain"])}

    site = _resolve_website(comp, allow_search=False)
    if not site["domain"]:
        return {"status": "no_domain_derived",
                "candidates": len(site.get("candidates") or [])}

    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if c and not (c.website_domain or "").strip():
            c.website_domain = site["domain"]
            s.commit()
    return {"status": "domain_found", "website": site["domain"],
            "validated_by": site["validated_by"], "source": site["source"]}


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
    retractable: list[str] = []      # fact fields this run explicitly returned as null

    # ---- Tier 2: facts from the site's own text -------------------------------
    bundle = site.get("bundle")
    if site["domain"] and bundle and (bundle.get("text") or "").strip():
        # Deterministic self-declared facts first — free, and more trustworthy
        # than prose extraction, so they are recorded even when the LLM stage is
        # skipped or fails.
        if bundle.get("facts"):
            fields["site_facts"] = bundle["facts"]
            provenance["site_facts"] = _prov(
                "website-strukturdaten", 0.9,
                "JSON-LD / tel: / mailto: / meta — kein LLM")
        # Brands found by scanning the FULL page against the closed vocabulary.
        # Recorded before the LLM stage so the conquest signal survives even when
        # extraction is skipped or fails — and so it covers the navigation menus
        # and logo strips the model's trimmed extract never sees.
        if bundle.get("brands"):
            fields["scanned_brands"] = bundle["brands"]
            provenance["scanned_brands"] = _prov(
                "website-markenabgleich", 0.9,
                "Wortgenauer Treffer im Seitentext — kein LLM")
        if allow_llm:
            try:
                # Architekturbüros get their own prompt: they SPECIFY systems,
                # they never sell or fabricate them, so the dealer prompt asks
                # the wrong questions and yields empty or misleading fields.
                prof = extract.profile_for(comp.get("segment"),
                                           comp.get("sub_segment"))
                facts = extract.extract_facts(bundle["text"], profile=prof)
                ev = facts.pop("evidence", {}) or {}
                model = facts.pop("llm_model", None)
                for key, value in facts.items():
                    if value in (None, [], ""):
                        continue
                    fields[key] = value
                    if key in ("assessment_de", "solarlux_relevance", "solarlux_fit"):
                        # an INFERENCE, not an extracted fact: separate source and a
                        # markedly lower confidence, so the report (and any future
                        # consumer) can present it as an estimate rather than a quote.
                        # solarlux_relevance belongs here too — it is graded from the
                        # project types, never quoted from the page, because no
                        # architect describes their own glazing areas in words.
                        provenance[key] = _prov("website+llm-einschaetzung", 0.5,
                                                "begründete Einschätzung, keine belegte Angabe")
                    else:
                        provenance[key] = _prov("website+llm", 0.85, ev.get(key))
                fields["_llm_model"] = model
                # Which fact fields this run had an opinion about. Needed because
                # the loop above skips null values, so a re-run could never RETRACT
                # a fact — see the retraction block in the persist step.
                retractable = [k for k in facts if k not in fields]
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
        # Same four signals identity/find_website.PROVEN accepts, and the same
        # three ONBOARDING promises a colleague ("eigene Telefonnummer,
        # PLZ+Straße, PLZ+Name"). `domain_plus_name` used to be in here, so this
        # path auto-accepted evidence the identity finder deliberately sends to a
        # human — two standards for one question. It writes a domain whose only
        # claim is a shared token: measured on the first 20 Spanish companies it
        # gave "Montajes Portico Balear SL" -> portsdebalears.com and "+ PLUS" ->
        # pressingplus.com, both wrong, both then fully enriched.
        _hard_proof = site["validated_by"] in ("domain_in_name", "phone",
                                               "plz_street", "plz_name", "manual")
        # A weak signal still counts when the CANDIDATE ITSELF came from our own
        # master data: the company's own e-mail sitting on that domain is
        # corroboration a search result does not have. So email_domain +
        # domain_plus_name is accepted (aluminioscerratosa.com for "Aluminios
        # Cerratosa"), serper + domain_plus_name is not (portsdebalears.com for
        # "Montajes Portico Balear SL"). The signal and its origin are two
        # different questions and only judging both separates those two cases.
        if site["domain"] and (not raw or (normalize_domain(raw) is None and _hard_proof)):
            note = (f"repaired malformed value {raw!r}, validated by {site['validated_by']}"
                    if raw else f"validated by {site['validated_by']}")
            c.website_domain = site["domain"]
            provenance["website_domain"] = _prov(
                site["source"] or "unknown", _CONFIDENCE.get(site["validated_by"] or "", 0.5),
                note)

        row = s.scalar(select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id))
        if row is None:
            row = CompanyEnrichment(company_id=company_id)
            s.add(row)
        merged = dict(row.fields or {})
        merged.update(fields)
        merged_prov = dict(row.provenance or {})
        merged_prov.update(provenance)

        # A re-run must be able to RETRACT a fact that is no longer supported.
        # merged.update() alone cannot: the extraction loop skips nulls, so a wrong
        # value survives every future pass. Three Spanish S.L. companies kept a
        # fabricated German "e.K." even after the extractor correctly began
        # returning null for it — the fix landed but the data never moved.
        # Only fields THIS run had an opinion about, and never a human's edit.
        for key in retractable:
            if (merged_prov.get(key) or {}).get("source") == "manual":
                continue
            merged.pop(key, None)
            merged_prov.pop(key, None)

        row.fields = merged
        row.provenance = merged_prov

        # Enrichment-owned columns mirror the merged result (not just this run's
        # additions), so a retraction clears them too instead of leaving the stale
        # value visible in the Explorer and the report.
        if retractable or fields:
            c.description = merged.get("description_de") or None
            c.products = merged.get("products") or None
            c.founded_year = merged.get("founded_year") or None
            c.employee_hint = merged.get("employee_hint") or None
            # Previously stopped here, leaving everything below trapped in
            # `row.fields` where no filter, export, report or ICP feature could
            # reach it — we paid the extraction and then hid the result.
            c.legal_form = merged.get("legal_form") or None
            c.service_area = merged.get("service_area") or None
            # Union of both routes: the scan guarantees completeness over the whole
            # page, the model contributes anything phrased so loosely that a literal
            # match missed it. Neither alone was enough.
            brands = _merge_brands(merged.get("competitor_brands"),
                                   merged.get("scanned_brands"))
            c.competitor_brands = brands or None
            c.mentions_solarlux = merged.get("mentions_solarlux")
            c.assessment = merged.get("assessment_de") or None
            c.certifications = merged.get("certifications") or None
            c.own_fabrication = merged.get("own_fabrication")
            c.has_showroom = merged.get("has_showroom")
            c.project_focus = merged.get("project_focus") or None
            c.positioning = merged.get("positioning") or None
            # architect profile only — null for dealers, which is correct:
            # 'not applicable' and 'not stated' are both null in this app
            c.solarlux_relevance = merged.get("solarlux_relevance") or None
            c.office_type = merged.get("office_type") or None
            c.decision_role = merged.get("decision_role") or None
            c.reference_scale = merged.get("reference_scale") or None
            # dealer profile only — the mirror question: does this company already
            # SELL what we make, and whose systems does it carry today
            # The brands are evidence, the fit is an inference; where they
            # disagree the evidence wins. Carrying Sunflex proves the company
            # sells our category, whatever a partial page made the model think.
            c.solarlux_fit = extract.apply_fit_floor(merged.get("solarlux_fit"), brands)
            c.partner_of = merged.get("partner_of") or None
            c.installs = merged.get("installs")
            c.enrich_profile = merged.get("profile") or c.enrich_profile
            # Machine-readable self-declared facts (site_facts). These only ever
            # FILL a gap: a phone or address from CRM is authoritative master
            # data and must not be replaced by a website's version.
            facts = merged.get("site_facts") or {}
            social = facts.get("social") or {}
            for col, key in (("facebook_url", "facebook"),
                             ("instagram_url", "instagram"),
                             ("linkedin_url", "linkedin")):
                if social.get(key) and not getattr(c, col):
                    setattr(c, col, social[key][:300])
            if facts.get("language") and not c.site_language:
                c.site_language = facts["language"]
            if facts.get("phone") and not c.phone:
                c.phone = str(facts["phone"])[:80]
                provenance["phone"] = _prov("site_facts", 0.8, "tel/JSON-LD")
            if facts.get("email") and not c.email:
                c.email = str(facts["email"])[:300]
                provenance["email"] = _prov("site_facts", 0.8, "mailto/JSON-LD")
            if facts.get("postal_code") and not c.postal_code:
                c.postal_code = str(facts["postal_code"])[:20]
            if facts.get("street") and not c.street:
                c.street = str(facts["street"])[:300]
            if facts.get("city") and not c.city:
                c.city = str(facts["city"])[:200]
            if facts.get("founded_year") and not c.founded_year:
                c.founded_year = facts["founded_year"]
        c.enrichment_status = status

        # ---- the identity verdict this run just reached --------------------
        # It was computed (validate_site ran on every candidate) and then thrown
        # away: only CompanyEnrichment.website_validated_by kept it, while the
        # Company identity columns stayed empty. Two consequences, both measured
        # on the first 20 Spanish companies:
        #
        #   * the Pruefen queue selects on Company.identity_status == 'needs_review',
        #     so seven companies with website suggestions were invisible to the
        #     human who has to decide them;
        #   * six companies carried a domain and a full profile with no verdict at
        #     all — the state dataquality.clear_unbacked_enrichment exists to clean
        #     up, arriving fresh from the pipeline that should prevent it.
        #
        # A human's decision always outranks this (accept_candidate writes
        # 'manual'), and a verdict is never downgraded from verified.
        if c.identity_matched_by != "manual" and (row.website_source != "manual"):
            considered = site.get("candidates") or []
            # Chosen by _resolve_website with the same predicate that set the
            # status, so a queued company always has something to look at.
            # Picking it separately offered "Montajes Portico Balear SL" ->
            # elpais.com and "Carpintería Guerrero" -> qdq.com — search results
            # that matched nothing, dressed up as suggestions. A queue full of
            # newspapers teaches the reader to click Nein without looking.
            review_domain = site.get("review_candidate")
            # A stored domain counts as proven only when the page itself proved it
            # this run (`proved_stored_domain`) — "it was already in the column"
            # is provenance, not evidence, and must stay 'unverified'.
            _proved = (site.get("proved_stored_domain") if site["source"] == "sap"
                       else bool(site["validated_by"]))
            if site["domain"] and _proved:
                c.identity_status = "verified"
                c.identity_matched_by = site["validated_by"]
                c.website_source = site["source"] or c.website_source
                c.identity_evidence = {"searched": bool(considered),
                                       "candidates": considered,
                                       "accepted": site["domain"],
                                       "signals": site.get("signals"),
                                       "review_candidate": None}
                c.identity_checked_at = dt.datetime.utcnow()
            elif status == "needs_review" and c.identity_status != "verified":
                c.identity_status = "needs_review"
                c.identity_evidence = {"searched": True, "candidates": considered,
                                       "accepted": None,
                                       "review_candidate": review_domain}
                c.identity_checked_at = dt.datetime.utcnow()
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
        # The identity columns must agree with the enrichment provenance, or the
        # row keeps surfacing in the review queue after a human already decided
        # — and the Google ad gate would treat a human-approved site as unknown.
        c.website_source = "manual"
        c.identity_status = "verified"
        c.identity_matched_by = "manual"
        c.identity_checked_at = dt.datetime.utcnow()
        ev = dict(c.identity_evidence or {})
        ev["review"] = {"decision": "accepted", "domain": dom}
        c.identity_evidence = ev
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
