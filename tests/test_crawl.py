"""Crawl und Ortserkennung: Tiefenlauf, Vorabtest, Laender- und Regionszuordnung.

Aufgeteilt aus test_core.py: 197 Tests in einer Datei von 6.000 Zeilen
liessen sich nicht mehr ueberblicken. Fixtures stehen in conftest.py.
"""
import datetime as dt   # noqa: F401

import pytest   # noqa: F401


_ES_HTML = """
<html><body>
  <nav>
    <a href="/">Inicio</a>
    <a href="/productos">Productos</a>
    <a href="/servicios/ventanas-pvc/serie-70">Serie 70</a>
    <a href="/quienes-somos">Quiénes somos</a>
    <a href="/contacto">Contacto</a>
    <a href="/aviso-legal">Aviso legal</a>
    <a href="https://facebook.com/firma">Facebook</a>
  </nav>
  <div class="mobile-menu">
    <a href="/contacto">Contacto</a>
    <a href="/productos/">Productos</a>
  </div>
</body></html>
"""

_FACTS_HTML = """
<html lang="es-ES"><head>
  <meta property="og:description" content="Carpintería de aluminio en Málaga">
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"LocalBusiness","name":"Protec Ventanas",
   "telephone":"+34 952 58 75 73","email":"info@protec.es",
   "foundingDate":"1998-04-01",
   "address":{"@type":"PostalAddress","streetAddress":"Calle Sol 4",
              "postalCode":"29620","addressLocality":"Torremolinos"},
   "sameAs":["https://www.facebook.com/protecventanas",
             "https://www.instagram.com/protec_ventanas"]}
  </script>
</head><body>
  <a href="tel:+34952587573">Llamar</a>
  <a href="mailto:info@protec.es">Escribir</a>
  <a href="https://www.facebook.com/sharer/sharer.php?u=x">Compartir</a>
  <a href="https://www.linkedin.com/company/protec-ventanas">LinkedIn</a>
</body></html>
"""


def test_enrich_domain_derivation_and_salvage():
    """Tier 0: a website derived from the SAP email, with the guards that keep
    freemail/portal domains out — plus salvage of the malformed values that are
    genuinely present in this dataset."""
    from adwatch.enrich.domains import domain_from_email, normalize_domain, salvage_domain

    assert domain_from_email("info@sf-mitschele.de") == "sf-mitschele.de"
    for bad in ("x@gmail.com", "y@t-online.de", "z@web.de",      # freemail
                "a@gelbeseiten.de", "b@facebook.com",             # portals/social
                "nope", "", None):
        assert domain_from_email(bad) is None, bad
    # a competitor's domain is still *derivable* — validation is what rejects it
    assert domain_from_email("kontakt@warema.de") == "warema.de"

    assert normalize_domain("https://WWW.Foo.de/kontakt?a=1") == "foo.de"
    assert normalize_domain("foo") is None
    # the SAP typo pattern 'http.' as a LABEL (live: http.terrassen-freye.de)
    # must NOT normalize — it has to take the salvage+validate+repair path
    assert normalize_domain("http.terrassen-freye.de") is None
    assert salvage_domain("http.terrassen-freye.de") == "terrassen-freye.de"
    # real malformed master-data values
    assert salvage_domain("https://http: //www.tischlerei-tieste.de") == "tischlerei-tieste.de"
    assert salvage_domain("http;//www.thalhammer-bau.com") == "thalhammer-bau.com"
    assert salvage_domain("www.bauelemente-thoms .de") == "bauelemente-thoms.de"
    assert salvage_domain("http./ www.alubau.org") == "alubau.org"
    assert salvage_domain("http://www.kurzbach-sonnenschutz.") is None   # no TLD, unrecoverable
    assert salvage_domain("https://foo.de/index.html") == "foo.de"       # not the file name

def test_enrich_validation_gate():
    """The safety gate: a website is only auto-accepted on hard SAP evidence.
    The `warema.de` case (a competitor's domain sitting in a contact email) must
    NOT validate — that is the wrong-page lesson applied to websites."""
    from adwatch.enrich.validate import validate_site, phone_matches, national_phone_digits

    comp = {"name": "Fensterbau Mitschele", "phone": "+49 5405 1234-0",
            "postal_code": "49134", "street": "Industriestr. 5", "city": "Wallenhorst"}

    # phone: same number formatted differently, and a different Durchwahl, match
    assert national_phone_digits("+49 5405 1234-0") == national_phone_digits("05405/1234-0")
    assert phone_matches("+49 5405 1234-0", "Tel. 05405 / 1234-20 · Fax ...")
    assert not phone_matches("+49 5405 1234-0", "Tel. 0221 / 9876543")
    assert not phone_matches("12345", "kurze nummer 12345")     # too few digits to be evidence

    assert validate_site(comp, "sf-mitschele.de", "Rufen Sie an: 05405 1234-0")["matched_by"] == "phone"
    assert validate_site(comp, "x.de", "49134 Wallenhorst, Industriestr. 5")["matched_by"] == "plz_street"
    assert validate_site(comp, "x.de", "49134 Wallenhorst — Mitschele")["matched_by"] == "plz_name"
    assert validate_site(comp, "sf-mitschele.de", "Willkommen bei Mitschele")["matched_by"] == "domain_plus_name"

    # the competitor domain: its site carries WAREMA's own address, not the dealer's
    warema = validate_site(comp, "warema.de",
                           "WAREMA Renkhoff SE, 97828 Marktheidenfeld, Tel 09391 20-0")
    assert warema["ok"] is False and warema["matched_by"] is None
    # a lone name mention is NOT enough to auto-accept
    weak = validate_site(comp, "irgendwas.de", "… Mitschele …")
    assert weak["ok"] is False

def test_enrich_extract_coercion():
    """extract.py must not let a stray year or an off-vocabulary product through
    (the LLM is told to extract, but the parser still enforces it)."""
    from adwatch.enrich.extract import _clean_list, _coerce_year, PRODUCT_VOCAB, COMPETITOR_BRANDS

    assert _coerce_year("1952") == 1952 and _coerce_year(1978) == 1978
    assert _coerce_year("keine Angabe") is None
    assert _coerce_year(12) is None and _coerce_year("905405") is None   # phone fragment
    assert _clean_list(["Fenster", "fenster", "Raumschiffe"], PRODUCT_VOCAB, 6) == ["Fenster"]
    assert _clean_list(["warema", "Sunflex"], COMPETITOR_BRANDS, 12) == ["WAREMA", "Sunflex"]
    assert _clean_list("not a list", PRODUCT_VOCAB, 6) == []

def test_enrich_never_overwrites_sap_website(temp_db, monkeypatch):
    """The hard rule: an existing (SAP) website is authoritative — enrichment
    fills blanks only. It must also still extract facts for such a company, and
    park an unprovable candidate as needs_review instead of writing it."""
    from adwatch.enrich import service
    import adwatch.enrich.fetchpage as fetchpage
    import adwatch.enrich.extract as extract_mod
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    known = Company(name="Hat Website GmbH", country="DE", website_domain="echte-firma.de",
                    phone="05405 1234-0", postal_code="49134", street="Industriestr. 5")
    blank = Company(name="Ohne Website GmbH", country="DE", email="info@fremde-domain.de",
                    phone="0221 999888", postal_code="50667", street="Domplatz 1")
    s.add_all([known, blank]); s.commit()
    kid, bid = known.id, blank.id
    s.close()

    monkeypatch.setattr(fetchpage, "page_bundle",
                        lambda domain, total_chars=9000: {
                            "domain": domain, "home_url": f"https://{domain}",
                            "text": "Wir bauen Fenster. Tel. 05405 1234-0", "pages": [], "chars": 36})
    monkeypatch.setattr(extract_mod, "extract_facts", lambda text, model=None, **kw: {
        "description_de": "Baut Fenster.", "products": ["Fenster"], "founded_year": 1952,
        "employee_hint": None, "legal_form": "GmbH", "service_area": None,
        "mentions_solarlux": True, "competitor_brands": [],
        "evidence": {"description_de": "Wir bauen Fenster."}, "llm_model": "test-model"})

    # (a) company that already has a website: kept, and facts extracted
    res = service.enrich_company(kid, allow_search=False)
    assert res["status"] == "enriched" and res["website_source"] == "sap"
    s = temp_db.SessionLocal()
    c = s.get(Company, kid)
    assert c.website_domain == "echte-firma.de"          # untouched
    assert c.description == "Baut Fenster." and c.founded_year == 1952
    assert c.products == ["Fenster"]
    s.close()
    assert service.get_enrichment(kid)["fields"]["mentions_solarlux"] is True

    # (b) blank company whose email domain canNOT be proven (page shows another
    #     company's phone) -> parked for review, website NOT written
    res_b = service.enrich_company(bid, allow_search=False)
    assert res_b["status"] == "needs_review" and res_b["website"] is None
    s = temp_db.SessionLocal()
    assert not (s.get(Company, bid).website_domain or "")   # still blank
    s.close()

    # (c) a human approves it -> stored as manual, confidence 1.0
    service.accept_candidate(bid, "fremde-domain.de")
    s = temp_db.SessionLocal()
    assert s.get(Company, bid).website_domain == "fremde-domain.de"
    s.close()
    enr = service.get_enrichment(bid)
    assert enr["website_source"] == "manual"
    assert enr["provenance"]["website_domain"]["confidence"] == 1.0

def test_enrich_fetch_ssrf_guard():
    """The crawler must never fetch non-public addresses — domains come from
    email addresses and search results (attacker-influenceable data). Private,
    loopback and link-local hosts are refused before any HTTP happens."""
    from adwatch.enrich.fetchpage import _host_is_public, page_bundle

    for private in ("127.0.0.1", "localhost", "10.0.0.8", "192.168.1.7",
                    "169.254.1.1", "0.0.0.0", "definitely-not-a-real-host-xyz.invalid"):
        assert _host_is_public(private) is False, private
        assert page_bundle(private) is None, private

def test_enrich_serper_fallback_after_failed_email_domain(temp_db, monkeypatch):
    """The coverage-gap fix: when the email-domain candidate FAILS validation
    (contact address on a supplier's domain), the web search must still run —
    and a search hit that validates via phone must be accepted, with the failed
    email candidate kept in the audit trail."""
    from adwatch.enrich import service
    import adwatch.enrich.fetchpage as fetchpage
    import adwatch.enrich.website_finder as finder
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    c = Company(name="Sonnenschutz Beispiel GmbH", country="DE",
                email="info@lieferanten-portal.de",           # usable but WRONG domain
                phone="0521 555123-0", postal_code="33602", street="Musterweg 3")
    s.add(c); s.commit()
    cid = c.id
    s.close()

    pages = {
        # the email domain's site: someone else's data -> must fail validation
        "lieferanten-portal.de": "Lieferanten-Portal AG, 80331 München, Tel 089 111111",
        # the search hit: carries the company's own phone -> must be accepted
        "sonnenschutz-beispiel.de": "Sonnenschutz Beispiel GmbH · Musterweg 3 · Tel 0521 555123-0",
    }
    monkeypatch.setattr(fetchpage, "page_bundle",
                        lambda domain, total_chars=9000: (
                            {"domain": domain, "home_url": f"https://{domain}",
                             "text": pages[domain], "pages": [], "chars": len(pages[domain])}
                            if domain in pages else None))
    searched = []
    monkeypatch.setattr(finder, "search_candidates",
                        lambda name, city=None, country="DE", limit=6: (
                            searched.append(name) or
                            [{"domain": "sonnenschutz-beispiel.de", "title": "t", "snippet": "s",
                              "position": 1}]))

    res = service.enrich_company(cid, allow_search=True, allow_llm=False)
    assert searched, "Serper fallback did not run after the email candidate failed"
    assert res["website"] == "sonnenschutz-beispiel.de"
    assert res["website_source"] == "serper" and res["validated_by"] == "phone"
    assert res["status"] == "enriched"                       # normalized, never the internal 'ok'
    enr = service.get_enrichment(cid)
    origins = {c.get("origin"): c.get("validated") for c in enr["website_candidates"]}
    assert origins.get("email_domain") is False              # the failed candidate stays auditable
    assert origins.get("serper") is True

def test_enrich_junk_search_hits_dont_reach_review(temp_db, monkeypatch):
    """Unrelated portals a search coughs up (no name signal at all) must NOT
    park the company in the review queue — that's an honest no_website_found.
    A search hit that at least carries the company name in its domain IS
    review-worthy. (Live case: 'Metallbau Thomas Saß' surfacing dastelefonbuch
    and metallbau.com — junk the gate rejected but the queue then showed.)"""
    from adwatch.enrich import service
    import adwatch.enrich.fetchpage as fetchpage
    import adwatch.enrich.website_finder as finder
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    a = Company(name="Metallbau Saßberg GmbH", country="DE", city="Neu Karin",
                phone="0170 111111", postal_code="18230")
    b = Company(name="Fensterbau Wexlinger", country="DE", city="Ulm",
                phone="0731 222222", postal_code="89073")
    s.add_all([a, b]); s.commit()
    aid, bid = a.id, b.id
    s.close()

    # crawls return generic portal text with NO connection to either company
    monkeypatch.setattr(fetchpage, "page_bundle",
                        lambda domain, total_chars=9000: {
                            "domain": domain, "home_url": f"https://{domain}",
                            "text": "Das große Branchenportal für Handwerker in Deutschland.",
                            "pages": [], "chars": 55})
    # company A gets pure junk; company B gets a hit whose DOMAIN carries its name
    def fake_search(name, city=None, country="DE", limit=4):
        if "Saßberg" in name or "Sassberg" in name:
            return [{"domain": "dashandwerk-portal.de", "title": "", "snippet": "", "position": 1}]
        return [{"domain": "fensterbau-wexlinger.de", "title": "", "snippet": "", "position": 1}]
    monkeypatch.setattr(finder, "search_candidates", fake_search)

    res_a = service.enrich_company(aid, allow_search=True, allow_llm=False)
    assert res_a["status"] == "no_website_found"          # junk -> not a review case
    trail = service.get_enrichment(aid)["website_candidates"]
    assert trail and trail[0]["domain"] == "dashandwerk-portal.de"   # but still auditable

    res_b = service.enrich_company(bid, allow_search=True, allow_llm=False)
    assert res_b["status"] == "needs_review"              # name-in-domain -> worth a look
    assert res_b["website"] is None                       # ...but never auto-accepted

def test_enrich_repairs_malformed_sap_website_only_with_proof(temp_db, monkeypatch):
    """A stored website that is objectively MALFORMED ('http.x.de' — live SAP
    typo) may be repaired, but ONLY by a domain that passed hard validation.
    Unprovable -> the broken value stays and the row goes to review."""
    from adwatch.enrich import service
    import adwatch.enrich.fetchpage as fetchpage
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    good = Company(name="Terrassen Freye", country="DE",
                   website_domain="http.terrassen-freye.de",     # malformed
                   phone="04441 88877-0", postal_code="49377")
    bad = Company(name="Kaputt GmbH", country="DE",
                  website_domain="http.kaputt-typo.de",          # malformed
                  phone="0999 123456", postal_code="99999")
    s.add_all([good, bad]); s.commit()
    gid, bid = good.id, bad.id
    s.close()

    texts = {
        "terrassen-freye.de": "Terrassen Freye · 49377 Vechta · Tel. 04441 88877-0",  # proves it
        "kaputt-typo.de": "Irgendein anderer Inhalt ohne Bezug.",                     # proves nothing
    }
    monkeypatch.setattr(fetchpage, "page_bundle",
                        lambda domain, total_chars=9000: (
                            {"domain": domain, "home_url": f"https://{domain}",
                             "text": texts[domain], "pages": [], "chars": 1}
                            if domain in texts else None))

    res = service.enrich_company(gid, allow_search=False, allow_llm=False)
    assert res["validated_by"] == "phone" and res["website"] == "terrassen-freye.de"
    s = temp_db.SessionLocal()
    assert s.get(Company, gid).website_domain == "terrassen-freye.de"     # REPAIRED
    assert s.get(Company, bid).website_domain == "http.kaputt-typo.de" or True
    s.close()
    prov = service.get_enrichment(gid)["provenance"]["website_domain"]
    assert "repaired malformed" in prov["evidence"]

    res_b = service.enrich_company(bid, allow_search=False, allow_llm=False)
    assert res_b["status"] == "needs_review"                              # salvaged origin -> reviewable
    s = temp_db.SessionLocal()
    assert s.get(Company, bid).website_domain == "http.kaputt-typo.de"    # NOT repaired without proof
    s.close()

def test_enrich_status_never_leaks_ok(temp_db, monkeypatch):
    """A reachable site with NO extractable text (JS-only page) must end as
    'enriched' with an explanatory error — never the internal 'ok' marker,
    which Company.enrichment_status doesn't know."""
    from adwatch.enrich import service
    import adwatch.enrich.fetchpage as fetchpage
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    c = Company(name="JS Only GmbH", country="DE", website_domain="js-only.de")
    s.add(c); s.commit()
    cid = c.id
    s.close()

    monkeypatch.setattr(fetchpage, "page_bundle",
                        lambda domain, total_chars=9000: {
                            "domain": domain, "home_url": f"https://{domain}",
                            "text": "   ", "pages": [], "chars": 0})
    res = service.enrich_company(cid, allow_search=False, allow_llm=True)
    assert res["status"] == "enriched"
    assert "no text extracted" in (res["error"] or "")
    s = temp_db.SessionLocal()
    assert s.get(Company, cid).enrichment_status == "enriched"
    s.close()

def test_legal_form_must_occur_in_the_source_text():
    """A Spanish S.L. was stored as the GERMAN form 'e.K.' — the old prompt offered
    a closed list of German forms, so the model substituted the nearest one. That
    is a false fact about a legal entity, not a translation. The form now only
    survives if it actually appears in the crawled text."""
    from adwatch.enrich.extract import _legal_form_in_text

    es = "ALLKONZEPT S.L. · Aviso legal · Calle Mayor 1, Mallorca"
    assert _legal_form_in_text("S.L.", es) == "S.L."
    assert _legal_form_in_text("e.K.", es) is None          # the fabrication is dropped
    # punctuation and spacing differ between sites, so matching ignores them
    assert _legal_form_in_text("S.L.", "Aluminios ALSABEN, SL — Las Palmas") == "S.L."
    assert _legal_form_in_text("Lda.", "Afcamoes Solutions LDA, Porto") == "Lda."
    # a German company keeps its German form
    assert _legal_form_in_text("GmbH", "Muster Fenster GmbH, Osnabrück") == "GmbH"
    assert _legal_form_in_text(None, es) is None
    assert _legal_form_in_text("GmbH", "") is None

    # The match must be anchored on word boundaries. A first attempt at this guard
    # compared punctuation-stripped strings, so "e.K." -> "ek" matched inside
    # ordinary words and three Spanish S.L. companies kept their fake German form.
    for word in ("Unsere Projekte in Mallorca", "perfekte Lösungen",
                 "Elektrische Antriebe", "Rekord"):
        assert _legal_form_in_text("e.K.", word) is None, word
    assert _legal_form_in_text("AG", "Wir sind Ihr Partner am Tag und Nacht") is None
    assert _legal_form_in_text("SL", "Alle Schlösser und Beschläge") is None
    # but a standalone occurrence still counts, however it is punctuated
    assert _legal_form_in_text("e.K.", "Fenster Meier e. K. — Impressum") == "e.K."
    assert _legal_form_in_text("AG", "Glas Trösch AG, Bützberg") == "AG"

def test_reenrichment_can_retract_a_fact_but_not_a_human_edit(temp_db, monkeypatch):
    """A corrected extractor is useless if the wrong value cannot be removed.
    Stored fields were only ever merged, and null results were skipped, so three
    Spanish S.L. companies kept a fabricated German 'e.K.' long after the
    extractor started returning null for it. A human's edit still wins."""
    from sqlalchemy import select
    from adwatch.enrich import extract, fetchpage, service as enrich_service
    from adwatch.models import Company, CompanyEnrichment

    s = temp_db.SessionLocal()
    c = Company(name="Retract SL", country="ES", website_domain="retract.es")
    s.add(c); s.commit()
    cid = c.id
    s.close()

    monkeypatch.setattr(fetchpage, "page_bundle",
                        lambda d: {"text": "Retract SL, Alicante", "pages": [d]})

    # run 1: the old, wrong extraction
    monkeypatch.setattr(extract, "extract_facts", lambda t, **kw: {
        "description_de": "Baut Fenster.", "legal_form": "e.K.",
        "employee_hint": "Un gran equipo", "products": ["Fenster"],
        "founded_year": None, "service_area": None, "mentions_solarlux": False,
        "competitor_brands": [], "assessment_de": None, "evidence": {}, "llm_model": "m"})
    enrich_service.enrich_company(cid, allow_search=False)

    s = temp_db.SessionLocal()
    e = s.scalar(select(CompanyEnrichment).where(CompanyEnrichment.company_id == cid))
    assert e.fields["legal_form"] == "e.K."
    assert s.get(Company, cid).employee_hint == "Un gran equipo"
    # a human corrects the size by hand
    e.provenance = {**(e.provenance or {}), "employee_hint": {"source": "manual"}}
    s.commit(); s.close()

    # run 2: the corrected extractor returns null for both
    monkeypatch.setattr(extract, "extract_facts", lambda t, **kw: {
        "description_de": "Baut Fenster und Türen.", "legal_form": None,
        "employee_hint": None, "products": ["Fenster", "Türen"],
        "founded_year": None, "service_area": None, "mentions_solarlux": False,
        "competitor_brands": [], "assessment_de": None, "evidence": {}, "llm_model": "m"})
    enrich_service.enrich_company(cid, allow_search=False)

    s = temp_db.SessionLocal()
    e = s.scalar(select(CompanyEnrichment).where(CompanyEnrichment.company_id == cid))
    comp = s.get(Company, cid)
    assert "legal_form" not in e.fields          # retracted
    assert e.fields["employee_hint"] == "Un gran equipo"   # human edit survives
    assert e.fields["description_de"] == "Baut Fenster und Türen."   # refreshed
    assert comp.products == ["Fenster", "Türen"]
    s.close()

def test_domain_prepass_is_free_and_never_writes_a_verdict(temp_db, monkeypatch):
    """The pre-pass must (a) never call the paid search, (b) write a proven domain,
    and (c) write NOTHING on a miss — 'no website found' is a verdict only the
    full run may reach, and stamping it here would hide the company from a later
    Serper attempt."""
    from adwatch.enrich import service as enrich_service, website_finder
    from adwatch.models import Company, CompanyEnrichment

    def _no_search(*a, **k):
        raise AssertionError("the free pre-pass must never call Serper")
    monkeypatch.setattr(website_finder, "search_candidates", _no_search)

    s = temp_db.SessionLocal()
    hit = Company(name="Mit Mail GmbH", country="DE", email="info@mitmail-gmbh.de",
                  phone="0541 123456", postal_code="49080", city="Osnabrück")
    miss = Company(name="Nur Freemail GmbH", country="DE", email="chef@t-online.de")
    had = Company(name="Hat Schon GmbH", country="DE", website_domain="hatschon.de")
    s.add_all([hit, miss, had]); s.commit()
    hid, mid, did = hit.id, miss.id, had.id
    s.close()

    # the derived domain validates via the phone number on the page
    monkeypatch.setattr(enrich_service.fetchpage, "page_bundle",
                        lambda d: {"text": "Impressum Musterstr. 1, 49080 Osnabrück, Tel. 0541 123456",
                                   "pages": [d]})

    assert enrich_service.derive_domain(did)["status"] == "already_had"
    r_hit = enrich_service.derive_domain(hid)
    r_miss = enrich_service.derive_domain(mid)

    assert r_hit["status"] == "domain_found" and r_hit["website"] == "mitmail-gmbh.de"
    assert r_miss["status"] == "no_domain_derived"

    s = temp_db.SessionLocal()
    assert s.get(Company, hid).website_domain == "mitmail-gmbh.de"
    # the miss is untouched: no website, no status, no enrichment row
    m = s.get(Company, mid)
    assert not m.website_domain
    assert m.enrichment_status in (None, "none")
    assert s.query(CompanyEnrichment).filter_by(company_id=mid).count() == 0
    s.close()

def test_report_shows_enriched_profiles_and_marks_the_estimate(temp_db, tmp_path):
    """The Firmenprofile section must render the enriched picture per company —
    WITHOUT any ad data (the Spain case) — and must keep the verified description
    and the AI assessment visibly separate, so an inference can't be read as a
    documented fact."""
    from adwatch.models import Company, CompanyEnrichment
    from adwatch.report import build_report

    s = temp_db.SessionLocal()
    c = Company(name="Cerramientos Test SL", country="ES", segment="Handel",
                sales_channel="Fachhandelsvertrieb", city="Barcelona",
                website_domain="cerramientos-test.es",
                description="Fachbetrieb für Glasfaltwände und Terrassenverglasung.",
                products=["Fenster", "Terrassendach"], founded_year=1998,
                employee_hint="12 Mitarbeiter", enrichment_status="enriched")
    s.add(c); s.flush()
    s.add(CompanyEnrichment(company_id=c.id, status="enriched", fields={
        "description_de": "Fachbetrieb für Glasfaltwände und Terrassenverglasung.",
        "assessment_de": "Dürfte ein Kleinbetrieb mit regionalem Fokus sein; "
                         "der Auftritt wirkt privatkundenorientiert.",
        "products": ["Fenster", "Terrassendach"], "founded_year": 1998,
        "employee_hint": "12 Mitarbeiter", "legal_form": "SL",
        "mentions_solarlux": False, "competitor_brands": ["WAREMA"],
    }))
    s.commit()
    cid = c.id
    s.close()

    out = str(tmp_path / "profile_report.pdf")
    build_report(path=out, filters={"ids": [cid]})

    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader(out).pages)
    assert "Firmenprofile" in text
    assert "Cerramientos Test SL" in text
    assert "Beschreibung:" in text and "Glasfaltw" in text
    # the inference is present AND labelled as an estimate, not as a fact
    assert "Einschätzung:" in text and "Kleinbetrieb" in text
    assert "keine belegte Angabe" in text
    # the hard fields and the brand signal made it in
    assert "1998" in text and "12 Mitarbeiter" in text
    assert "WAREMA" in text

def test_ad_products_are_normalised_to_german_families():
    """Ads are written in the local market's language but the report is German.
    The first Spanish run listed 'cerramiento', 'Porche-Verschluss (porch closure)'
    and 'windows and doors (wood, PVC)' as separate products — the same family in
    three languages, plus materials. Everything must fold onto one vocabulary."""
    from adwatch.products import PRODUCT_VOCAB, canonical_products

    # the four strings that actually appeared for one Spanish company
    assert canonical_products([
        "cerramiento", "Porcheschließung/Porche-Verschluss",
        "Porche-Verschluss (porch closure)", "Porcheverglasungen/Terrassenverglasung",
    ]) == ["Terrassenverglasung"]

    assert canonical_products(
        ["windows and doors (wood, wood-aluminium, aluminium, PVC)"]) == ["Fenster", "Türen"]

    # German inflections must not survive as separate families
    assert canonical_products(["Wintergärten", "Wintergarten"]) == ["Wintergarten"]
    # materials are not products, and unmappable free text is dropped, not shown
    assert canonical_products(["Aluminium", "PVC", "Holz"]) == []
    assert canonical_products(["Raumschiffe"]) == []
    # short keys need word boundaries: 'tor' inside 'Motor' is not a Tor
    assert canonical_products(["Motor", "importante"]) == []
    # every result is a member of the shared vocabulary, always
    for out in (canonical_products(["toldos y persianas"]),
                canonical_products(["puertas correderas de cristal"])):
        assert out and all(p in PRODUCT_VOCAB for p in out)

    # the enrichment side imports the very same tuple, so the two can't drift
    from adwatch.enrich.extract import PRODUCT_VOCAB as VOCAB_ENRICH
    assert VOCAB_ENRICH is PRODUCT_VOCAB

def test_extract_separates_facts_from_assessment(monkeypatch):
    """The assessment is capped and kept as its own field; the fact fields stay
    extract-only (the prompt enforces that, the parser enforces the shape)."""
    from adwatch.enrich.extract import _clean_list, PRODUCT_VOCAB
    import adwatch.enrich.extract as ex

    raw = {
        "description_de": "Baut Fenster.", "products": ["Fenster", "Unfug"],
        "founded_year": 1990, "employee_hint": None, "legal_form": "GmbH",
        "service_area": None, "mentions_solarlux": True, "competitor_brands": ["warema"],
        "evidence": {"description_de": "Wir bauen Fenster."},
        "assessment_de": "X" * 900,
    }

    class _Blk:
        type = "text"
        text = __import__("json").dumps(raw)

    class _Msg:
        content = [_Blk()]

    class _Client:
        def __init__(self, **k): self.messages = self
        def create(self, **k): return _Msg()

    import sys, types
    mod = types.ModuleType("anthropic"); mod.Anthropic = _Client
    # setitem statt direkter Zuweisung: `sys.modules["anthropic"] = mod` hat den
    # Stub fuer den REST DES LAUFS stehen lassen. Jeder spaetere Test in dieser
    # Datei sah dann ein anthropic ohne APIStatusError -- gemerkt hat das
    # niemand, bis ein neuer Test das echte Modul brauchte und mit
    # AttributeError umfiel statt mit einer verstaendlichen Meldung.
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    ex.config.ANTHROPIC_API_KEY = "test-key"

    got = ex.extract_facts("x" * 200)
    assert got["description_de"] == "Baut Fenster."
    assert got["products"] == ["Fenster"]                 # off-vocabulary dropped
    assert got["competitor_brands"] == ["WAREMA"]         # canonicalised
    assert len(got["assessment_de"]) == 700               # capped, not unbounded
    assert "evidence" in got and got["evidence"]["description_de"]

def test_postcode_backed_match_is_accepted(temp_db, monkeypatch):
    from adwatch.identity import find_website as fw
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    c = Company(name="Aluminios Ejemplo", country="ES", city="Valencia",
                postal_code="46020", street="Av. Catalunya 13",
                lead_source="t", segment="Verarbeiter")
    s.add(c); s.commit(); cid = c.id; s.close()

    monkeypatch.setattr(fw, "search_candidates",
                        lambda *a, **k: [{"domain": "aluminios-ejemplo.com"}])
    monkeypatch.setattr(fw, "page_bundle", lambda d, **k: {
        "text": "Aluminios Ejemplo, Av. Catalunya 13, 46020 Valencia",
        "pages": [f"https://{d}"]})
    r = fw.find_for(cid)
    assert r["status"] == fw.VERIFIED
    assert r["matched_by"] in ("plz_street", "plz_name")
    with temp_db.SessionLocal() as s:
        got = s.get(Company, cid)
        assert got.website_domain == "aluminios-ejemplo.com"
        assert got.website_source == "serper"

def test_spanish_trade_words_are_not_identifying():
    """The AURIA incident: the generic-word list was German-only, so 'estudio' and
    'arquitectura' counted as identifying. A DIFFERENT architecture studio in the
    same town (same postcode, 'estudio de arquitectura' on its homepage) then
    passed the plz_name gate and was stored as AURIA's website — the exact
    wrong-website failure the identity gate exists to prevent."""
    from adwatch.enrich.validate import distinctive_tokens, validate_site

    assert distinctive_tokens("Estudio de Arquitectura AURIA") == {"auria"}
    assert distinctive_tokens("Protec Ventanas") == {"protec"}
    assert distinctive_tokens("Aluminios Baraza") == {"baraza"}
    assert distinctive_tokens("Carpinteria Metalica FEVEGAR") == {"fevegar"}

    # the live failure, replayed: another studio's page in the same town
    company = {"name": "Estudio de Arquitectura AURIA", "phone": None,
               "postal_code": "06220", "street": "Calle Cisneros 12"}
    other_studio = "Estudio de arquitectura en Villafranca de los Barros, 06220"
    res = validate_site(company, "thau.es", other_studio)
    assert res["ok"] is False, "generic trade words must not prove identity"
    # ...while the real match (name token present) still works
    own = "AURIA estudio, Calle Cisneros 12, 06220 Villafranca"
    assert validate_site(company, "auria.es", own)["ok"] is True

def test_spanish_product_pages_are_selected_not_just_contacto():
    """The old rule matched only impressum|kontakt|contact and took the first two
    in document order, so a Spanish site yielded homepage + 'contacto' and the
    product pages were NEVER read — the products list then came from whatever the
    homepage happened to mention."""
    from adwatch.identity import website_source as ws
    picked = ws._subpage_urls("https://ejemplo.es/", _ES_HTML, max_pages=3)
    cats = [ws._classify_link(u) for u in picked]
    assert "products" in cats, picked
    assert "legal" in cats, picked
    assert "https://ejemplo.es/productos" in picked
    # the section index is read BEFORE a deep single-item page: '/productos' is
    # the whole range, '/servicios/ventanas-pvc/serie-70' is one article
    prods = [u for u in picked if ws._classify_link(u) == "products"]
    assert prods[0] == "https://ejemplo.es/productos", prods
    # and with only two slots the deep page must never displace the index
    picked2 = ws._subpage_urls("https://ejemplo.es/", _ES_HTML, max_pages=2)
    assert not any("serie-70" in u for u in picked2), picked2

def test_duplicate_nav_targets_do_not_consume_slots():
    """A nav bar repeats the same href in the desktop and mobile menus; each
    duplicate used to eat one of only two available slots."""
    from adwatch.identity import website_source as ws
    picked = ws._subpage_urls("https://ejemplo.es/", _ES_HTML, max_pages=4)
    paths = [u.rstrip("/").rsplit("ejemplo.es", 1)[-1] for u in picked]
    assert len(paths) == len(set(paths)), picked

def test_offsite_and_non_http_links_are_never_fetched():
    from adwatch.identity import website_source as ws
    picked = ws._subpage_urls("https://ejemplo.es/", _ES_HTML, max_pages=6)
    assert all("ejemplo.es" in u for u in picked), picked

def test_link_categories_cover_the_app_markets():
    """config/markets.yaml already knew ES='aviso legal', FR='mentions légales',
    IT='contatti' while the crawler only looked for German terms."""
    from adwatch.identity.website_source import _classify_link
    assert _classify_link("https://x.es/aviso-legal") == "legal"
    assert _classify_link("https://x.fr/mentions-legales") == "legal"
    assert _classify_link("https://x.it/contatti") == "legal"
    assert _classify_link("https://x.es/productos") == "products"
    assert _classify_link("https://x.pt/produtos") == "products"
    assert _classify_link("https://x.es/quienes-somos") == "about"
    assert _classify_link("https://x.de/referenzen") == "references"
    assert _classify_link("https://x.es/") is None

def test_site_facts_reads_jsonld_contact_and_socials():
    """Free, deterministic, and it unlocks the two strongest identity signals:
    phone (ranked first by validate_site) and the company's OWN Facebook page.
    Not one of the 39 Spain rows without a website had a phone number."""
    from adwatch.enrich import site_facts
    f = site_facts.extract(_FACTS_HTML, base_url="https://protec.es/")
    assert f["phone"] == "+34 952 58 75 73"
    assert f["email"] == "info@protec.es"
    assert f["postal_code"] == "29620" and f["city"] == "Torremolinos"
    assert f["street"] == "Calle Sol 4"
    assert f["founded_year"] == 1998
    assert f["language"] == "es-es"
    assert f["social"]["facebook"] == "https://www.facebook.com/protecventanas"
    assert f["social"]["instagram"] == "https://www.instagram.com/protec_ventanas"
    assert f["social"]["linkedin"] == "https://www.linkedin.com/company/protec-ventanas"
    assert f["sources"]["phone"] == "json-ld"

def test_share_widgets_are_not_mistaken_for_the_company_profile():
    """A sharer link points at OUR page on Facebook, not the company's — treating
    it as the company's profile would attribute someone else's ads."""
    from adwatch.enrich import site_facts
    html = ('<a href="https://www.facebook.com/sharer/sharer.php?u=x">s</a>'
            '<a href="https://facebook.com/plugins/like.php">l</a>')
    assert "social" not in site_facts.extract(html)

def test_personal_mailboxes_are_not_harvested():
    """A role inbox is a company address; a named person's is personal data the
    app has no reason to store (same rule that excludes Geschäftsführer names)."""
    from adwatch.enrich import site_facts
    f = site_facts.extract('<a href="mailto:maria.gomez@firma.es">Maria</a>')
    assert "email" not in f
    f2 = site_facts.extract('<a href="mailto:info@firma.es">Info</a>')
    assert f2["email"] == "info@firma.es"

def test_malformed_jsonld_never_breaks_extraction():
    from adwatch.enrich import site_facts
    html = ('<script type="application/ld+json">{"@type":"Organization",}</script>'
            '<a href="tel:+34911223344">x</a>')
    f = site_facts.extract(html)
    assert f["phone"] == "+34911223344"   # salvaged the trailing comma, or fell back

def test_tri_state_booleans_keep_not_stated_distinct_from_no():
    """A site that never mentions its workshop must not be recorded as a
    confirmed pure trader."""
    from adwatch.enrich.extract import _tri_state
    assert _tri_state(True) is True
    assert _tri_state(False) is False
    assert _tri_state(None) is None
    assert _tri_state("ja") is True
    assert _tri_state("unklar") is None

def test_postcode_check_is_country_aware():
    """Requiring exactly 5 digits was German thinking. It silently never matched
    for ~8,200 companies: AT/DK/NO/BE (4 digits), NL/GB (alphanumeric) — so one
    of only three hard identity proofs was dead in six countries, with no error
    anywhere."""
    from adwatch.enrich.validate import plz_matches

    # DE: unchanged 5-digit behaviour
    assert plz_matches("49134", "Wallenhorst, 49134 Deutschland", country="DE")
    assert not plz_matches("49134", "nothing here", country="DE")

    # AT 4-digit: needs the town too, so a year cannot pass as a postcode
    at_page = "Musterweg 3, 4020 Linz, Österreich"
    assert plz_matches("4020", at_page, country="AT", city="Linz")
    assert not plz_matches("4020", "gegründet 4020 Stück verkauft", country="AT",
                           city="Linz"), "bare 4-digit match must not count"
    assert not plz_matches("1998", "Firma seit 1998", country="AT", city="Linz")

    # NL alphanumeric, spacing-insensitive
    assert plz_matches("1234 AB", "Straat 5, 1234AB Amsterdam", country="NL")
    assert plz_matches("1234AB", "Straat 5, 1234 AB Amsterdam", country="NL")

    # GB outcode+incode
    assert plz_matches("SW1A 1AA", "London SW1A 1AA", country="GB")

    # unknown country falls back to the safe 5-digit rule
    assert plz_matches("28001", "Madrid 28001") is True
    assert plz_matches("4020", "Linz 4020") is False

def test_architect_profile_is_selected_from_segment():
    from adwatch.enrich.extract import (profile_for, PROFILE_ARCHITEKT,
                                        PROFILE_BETRIEB)
    assert profile_for("Architekten") == PROFILE_ARCHITEKT
    # planners filed under another segment still behave like architects
    assert profile_for("Baudienstleister", "Generalplaner") == PROFILE_ARCHITEKT
    assert profile_for("Handel", "Bauelementehandel") == PROFILE_BETRIEB
    assert profile_for(None) == PROFILE_BETRIEB

def test_architect_prompt_never_asks_a_planner_to_sell():
    """The dealer prompt opens with 'Bauelemente-/Handwerksbetrieb' and asks for
    own_fabrication and has_showroom — wrong in kind for a planning office, which
    SPECIFIES systems. The architect prompt must ask the architect questions."""
    from adwatch.enrich.extract import _prompt, PROFILE_ARCHITEKT, PROFILE_BETRIEB
    arch = _prompt(PROFILE_ARCHITEKT)
    deal = _prompt(PROFILE_BETRIEB)
    assert "ARCHITEKTUR" in arch and "VERKAUFT keine Bauelemente" in arch
    assert "own_fabrication" not in arch and "has_showroom" not in arch
    assert "solarlux_relevance" in arch and "decision_role" in arch
    # and the dealer prompt is untouched
    assert "own_fabrication" in deal and "Bauelemente-/Handwerksbetrieb" in deal

def test_spa_shell_is_rendered_and_relinked(monkeypatch, temp_db):
    """A single-page app answers 200 with an empty shell, so the fetch "succeeds"
    and yields nothing — the company enriches to nothing and drops out of every
    list. Rendering also has to restore LINK DISCOVERY: a SPA builds its nav in
    JavaScript, so the raw HTML has no <a href> and the subpages vanish too."""
    from adwatch.enrich import fetchpage as fp

    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    painted = ('<html><body><main>Carpintería de aluminio en Marbella. '
               'Fabricamos ventanales y cerramientos.</main>'
               '<a href="https://spa.example/productos">Productos</a>'
               '<footer>Distribuidor oficial de Sunflex</footer></body></html>')
    sub = '<html><body><main>Ventanales correderas y cerramientos de terraza.</main></body></html>'

    monkeypatch.setattr(fp, "_host_is_public", lambda h: True)
    monkeypatch.setattr(fp, "_robots_allows", lambda d: True)
    monkeypatch.setattr(fp, "_fetch", lambda url, wall_clock=None: (
        (shell, "https://spa.example") if url.rstrip("/").endswith("spa.example") else (sub, url)))
    monkeypatch.setattr(fp.render, "available", lambda: True)
    monkeypatch.setattr(fp.render, "render_html", lambda url, timeout_ms=None: painted)

    b = fp.page_bundle("spa.example")
    assert b["rendered"] is True
    assert "Carpintería de aluminio" in b["text"]
    # the brand in the rendered footer is reachable now
    assert "Sunflex" in (b["brands"] or [])
    # and the link that only exists after rendering was followed
    assert any("productos" in p for p in b["pages"])

def test_without_playwright_the_pipeline_is_unchanged(monkeypatch):
    """Nobody has to install a browser to run AdWatch. With no renderer the
    starved page stays starved — same result as before this existed — and
    nothing raises."""
    from adwatch.enrich import fetchpage as fp

    shell = '<html><body><div id="root"></div></body></html>'
    monkeypatch.setattr(fp, "_host_is_public", lambda h: True)
    monkeypatch.setattr(fp, "_robots_allows", lambda d: True)
    monkeypatch.setattr(fp, "_fetch", lambda url, wall_clock=None: (shell, "https://spa.example"))
    monkeypatch.setattr(fp.render, "available", lambda: False)
    monkeypatch.setattr(fp.render, "render_html",
                        lambda url, timeout_ms=None: pytest.fail("must not launch a browser"))

    b = fp.page_bundle("spa.example")
    assert b["rendered"] is False and b["brands"] == []

def test_render_never_raises_into_the_pipeline(monkeypatch):
    """A browser crash must degrade to the plain-fetch result, not take down the
    enrichment of a company whose site merely happens to be slow."""
    from adwatch.enrich import render as rd
    monkeypatch.setattr(rd, "available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("browser exploded")
    monkeypatch.setattr(rd, "_ua", boom)
    assert rd.render_html("https://example.com") is None

def test_shared_domain_is_reported_not_merged(temp_db, monkeypatch):
    """Nearly a bad automated fix. Most shared domains are corporate GROUPS, not
    duplicates: Lindner has 8 legal entities on one website, each with its own
    SAP number and revenue. Only a shared domain AND a matching name means the
    same firm twice."""
    from adwatch import dataquality as dq
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    s.add_all([
        Company(name="Lindner Building Envelope GmbH", segment="Handel", website_domain="lindner.com"),
        Company(name="Lindner Scandinavia AB", segment="Handel", website_domain="lindner.com"),
        Company(name="CBF", segment="Handel", website_domain="calviabalear.com"),
        Company(name="CBF S.L.", segment="Handel", website_domain="calviabalear.com"),
    ])
    s.commit(); s.close()
    monkeypatch.setattr(dq, "SessionLocal", temp_db.SessionLocal)

    out = dq.find_domain_duplicates()
    by_dom = {g["domain"]: g for g in out["top"]}
    assert by_dom["calviabalear.com"]["duplicate_pairs"] == [["CBF", "CBF S.L."]]
    # the Lindner entities share a domain but are different firms, so no pair
    assert "lindner.com" not in by_dom
    assert out["groups_with_a_duplicate_pair"] == 1
    # and nothing was deleted
    with temp_db.SessionLocal() as s2:
        assert s2.query(Company).count() == 4

def test_brand_evidence_outranks_an_inferred_fit():
    """Proymetal trades as "SUNFLEX Top-Partner", the scan found Sunflex in its
    logo strip, and the model still graded it "gering" — the extract it was given
    stopped before the brand and read like a general metalwork shop. Storing both
    would put a proven conquest target at the bottom of the ranking. Carrying a
    direct competitor is a FACT about the company and outranks the inference."""
    from adwatch.enrich.extract import apply_fit_floor, fit_floor
    assert apply_fit_floor("gering", ["Sunflex"]) == "hoch"
    assert apply_fit_floor(None, ["Vitrocsa"]) == "hoch"
    # terrace brands only lift to mittel — Markilux makes awnings, we do not
    assert apply_fit_floor("gering", ["Markilux"]) == "mittel"
    assert apply_fit_floor("hoch", ["Renson"]) == "hoch"       # never lowers
    # profile suppliers prove nothing about the category, so no floor at all
    assert apply_fit_floor("mittel", ["Cortizo", "Schüco"]) == "mittel"
    assert apply_fit_floor(None, ["Cortizo"]) is None
    assert fit_floor([]) is None

def test_brand_scan_survives_the_chrome_strip_and_the_char_budget():
    """The two edits that make the prose extract good — dropping navigation and
    capping characters — are the two that hide brand names, because a "Marcas"
    menu and a partner logo strip are chrome by every structural test. Dekovent
    lost Vitrocsa, Renson and Griesser that way and Proymetal lost Sunflex: the
    direct competitors, the ones worth most. Brands are a closed vocabulary, so
    they are found by scanning the WHOLE page, not by the model's trimmed extract."""
    from adwatch.identity.website_source import _page_text
    from adwatch.enrich.extract import scan_brands, brand_tiers
    html = ("<html><body>"
            "<nav><ul><li>Marcas</li><li>Vitrocsa</li><li>Renson</li></ul></nav>"
            "<main>Carpintería de aluminio en Valencia.</main>"
            "<footer>Distribuidor oficial de Sunflex</footer></body></html>")
    # the model's view has lost both menu brands ...
    assert "Vitrocsa" not in _page_text(html, limit=5000, drop_chrome=True)
    # ... the scan reads the full page and keeps them
    found = scan_brands(_page_text(html, limit=10 ** 7))
    assert {"Vitrocsa", "Renson", "Sunflex"} <= set(found)
    assert brand_tiers(found) == ["direkt", "terrasse"]

def test_brand_scan_does_not_match_ordinary_words():
    """A regex over brand names is only safe because the risky ones are excluded.
    "Roma" is a Mallorca street, "Keller" is a German surname and a basement —
    matching those would invent a supplier relationship out of an address."""
    from adwatch.enrich.extract import scan_brands
    assert scan_brands("Calle Roma 14, Palma. Herr Keller. Guardian Sapa Hydro") == []
    # and a real brand still has to stand alone, not sit inside another word
    assert scan_brands("Wir bauen Sunflex-Anlagen") == ["Sunflex"]
    assert scan_brands("cortizona ist keine Marke") == []

def test_trailing_text_after_the_json_does_not_lose_the_extraction():
    """The model sometimes appends a sentence or a second object after the
    closing brace. json.loads() rejects the WHOLE reply as "Extra data", the
    caller swallows the exception and still stores the row as enriched — so
    Comervia and MODIKO ended up with every field empty and no visible failure.
    The object is intact and sits at the start; parse that and drop the rest."""
    import json as _json
    from adwatch.enrich.extract import _loads_first_object
    good = '{"description_de": "Architekturbüro.", "solarlux_relevance": "hoch"}'
    assert _loads_first_object(good)["solarlux_relevance"] == "hoch"
    # trailing prose, a second object, and leading chatter all survive
    assert _loads_first_object(good + "\n\nHinweis: geschätzt.")["description_de"] == "Architekturbüro."
    assert _loads_first_object(good + "\n" + good)["solarlux_relevance"] == "hoch"
    assert _loads_first_object("Hier das JSON:\n" + good)["solarlux_relevance"] == "hoch"
    # genuinely unparseable input must still raise, not return a silent {}
    import pytest as _pytest
    with _pytest.raises((ValueError, _json.JSONDecodeError)):
        _loads_first_object("kein JSON hier")

def test_navigation_menus_do_not_eat_the_extraction_budget():
    """TYPSA handed the extractor 9.000 characters of mega-menu — "Carreteras ·
    Ferrocarriles · Aeropuertos" — and not one sentence of prose, so every field
    came back null. Text is kept in document order, so a big menu starves the
    budget. Enrichment strips the chrome; the identity check must NOT, because it
    matches on the phone number and postcode that live in the footer."""
    from adwatch.identity.website_source import _page_text
    html = ("<html><head><title>Estudio</title></head><body>"
            "<nav><ul><li>Carreteras</li><li>Ferrocarriles</li><li>Aeropuertos</li></ul></nav>"
            "<header><ul class='main-menu'><li>Quiénes somos</li></ul></header>"
            "<main>Wir planen Villen an der Costa del Sol.</main>"
            "<footer>Tel. 952 123 456 · 29601 Marbella</footer></body></html>")
    clean = _page_text(html, limit=5000, drop_chrome=True)
    assert "Villen" in clean and "Carreteras" not in clean and "Quiénes somos" not in clean
    # the footer survives — it is evidence, not chrome
    assert "952 123 456" in clean and "29601" in clean
    # identity path is untouched: it still sees everything
    raw = _page_text(html, limit=5000)
    assert "Carreteras" in raw and "952 123 456" in raw

def test_stripping_chrome_never_empties_a_page():
    """Some small sites put their whole body inside <header>. Stripping would
    leave nothing, and a flooded extraction still beats an empty one, so the
    strip is abandoned when it removes essentially everything."""
    from adwatch.identity.website_source import _page_text
    html = ("<html><body><header>Estudio de arquitectura en Marbella. "
            "Wir planen Villen und Hotels seit 1998.</header></body></html>")
    kept = _page_text(html, limit=5000, drop_chrome=True)
    assert "Villen und Hotels" in kept

def test_relevance_is_judged_not_quoted():
    """Regression: the first architect run graded 0 of 72 Spanish offices "hoch"
    and put Costa-del-Sol villa studios on "gering". Cause: solarlux_relevance was
    asked for inside TEIL 1 — FAKTEN ("nichts schätzen"), while its "hoch" rubric
    required the site to MENTION large glazing. No architect writes that about
    their own work, so the top grade was unreachable and the residual bucket
    swallowed the best targets. Relevance must stay in the judgement half, key off
    project type, and never be the fallback for missing information."""
    from adwatch.enrich.extract import _prompt, PROFILE_ARCHITEKT
    arch = _prompt(PROFILE_ARCHITEKT)
    facts, judgement = arch.split("TEIL 2")
    # the grade is judged, not quoted
    assert "solarlux_relevance" not in facts
    assert "solarlux_relevance" in judgement
    # graded on what they build, and absence of glazing words is not evidence
    assert "ART DER PROJEKTE" in judgement and "KEIN Gegenbeleg" in judgement
    assert "Villen" in judgement and "Hotels" in judgement
    # null, not "gering", is the bucket for "cannot tell"
    assert "KEIN Auffangwert" in judgement
    # emitted after project_focus, so the grade is conditioned on the facts
    assert arch.index('"project_focus"') < arch.rindex('"solarlux_relevance"')

def test_architect_answer_maps_onto_shared_storage_keys(monkeypatch):
    """Same columns wherever the meaning carries over, so no downstream filter
    needs a per-segment branch: elements->products, specified_systems->
    competitor_brands, memberships->certifications. own_fabrication/has_showroom
    stay NULL — for a planner they are not 'no', they are not applicable."""
    import json as _json
    from adwatch.enrich import extract

    class _Blk:
        type = "text"
        text = _json.dumps({
            "description_de": "Architekturbüro für Wohn- und Hotelbauten.",
            "elements": ["Schiebetüren", "Fassade"],
            "specified_systems": ["Schüco", "Sky-Frame"],
            "solarlux_relevance": "hoch",
            "office_type": "Architekturbüro",
            "decision_role": "vergibt Aufträge",
            "project_focus": ["Wohnbau", "Hotel/Gastro"],
            "reference_scale": "über 200 Projekte",
            "memberships": ["COAM"],
            "founded_year": 1998, "employee_hint": "12 Architekten",
            "legal_form": "S.L.P.", "service_area": "Madrid",
            "mentions_solarlux": False, "evidence": {}, "assessment_de": "Gross.",
        })

    class _Msg:
        content = [_Blk()]

    class _Client:
        def __init__(self, **kw): self.messages = self
        def create(self, **kw):
            assert "ARCHITEKTUR" in kw["messages"][0]["content"]
            return _Msg()

    monkeypatch.setattr(extract.config, "ANTHROPIC_API_KEY", "test", raising=False)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Client)

    # the form must be IN the text — _legal_form_in_text rejects one that is not
    f = extract.extract_facts("Estudio S.L.P. " + "x" * 200,
                              profile=extract.PROFILE_ARCHITEKT)
    assert f["products"] == ["Schiebetüren", "Fassade"]      # plans with
    assert set(f["competitor_brands"]) == {"Schüco", "Sky-Frame"}  # specifies
    assert f["certifications"] == ["COAM"]
    assert f["solarlux_relevance"] == "hoch"
    assert f["decision_role"] == "vergibt Aufträge"
    assert f["own_fabrication"] is None and f["has_showroom"] is None
    assert f["legal_form"] == "S.L.P."
    assert f["profile"] == extract.PROFILE_ARCHITEKT

def test_architect_relevance_rejects_invented_values(monkeypatch):
    """A free-text answer outside the allowed set must become null, not be stored."""
    import json as _json
    from adwatch.enrich import extract

    class _Blk:
        type = "text"
        text = _json.dumps({"description_de": "x", "solarlux_relevance": "sehr hoch",
                            "office_type": "Weltmeister", "decision_role": "vielleicht",
                            "evidence": {}})

    class _Msg:
        content = [_Blk()]

    class _Client:
        def __init__(self, **kw): self.messages = self
        def create(self, **kw): return _Msg()

    monkeypatch.setattr(extract.config, "ANTHROPIC_API_KEY", "test", raising=False)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    f = extract.extract_facts("y" * 200, profile=extract.PROFILE_ARCHITEKT)
    assert f["solarlux_relevance"] is None
    assert f["office_type"] is None and f["decision_role"] is None

def test_spanish_directories_never_reach_the_review_queue():
    """The directory blocklist was built during German testing — 60+ entries, all
    German portals — so the Spanish equivalents ranked straight through it. A
    directory contains every company name by definition, which is exactly the
    signal _review_worthy trusts, so they clogged the queue instead of failing
    closed: "Carpintería Guerrero S.L." was offered qdq.com and "Montajes Portico
    Balear SL" got elpais.com.

    Substring matching cuts both ways, so the real company sites are asserted
    too — `elpaisajista.es` must survive `elpais.`."""
    from adwatch.enrich.website_finder import _is_directory
    from adwatch.enrich.domains import is_usable_company_domain

    for junk in ("qdq.com", "paginasamarillas.es", "einforma.com", "axesor.es",
                 "eleconomista.es", "elpais.com", "idealista.com", "expansion.com"):
        assert _is_directory(junk), f"{junk} ist ein Verzeichnis/Portal"
    for freemail in ("gmail.co.uk", "hotmail.es", "terra.es", "wanadoo.es"):
        assert not is_usable_company_domain(freemail), f"{freemail} ist Freemail"

    # and the ones that merely LOOK like an entry above
    for real in ("elpaisajista.es", "expansion-metallbau.de", "alurei.com",
                 "dorflex.net", "aluminioscerratosa.com"):
        assert _is_directory(real) is False, f"{real} ist eine echte Firmenseite"
        assert is_usable_company_domain(real)

def test_the_map_never_pins_a_private_household(temp_db):
    """Die Karte zeigt Firmen. Private Endkunden sind Privatadressen — ein Pin
    auf deren Wohnung ist genau die Sorte Leck, die die scope-Klausel
    verhindern soll, und der Filter sitzt deshalb im SERVER, nicht im Frontend.

    Außerdem festgenagelt: der Zentroid-Lauf überschreibt nie eine genauere
    Koordinate ('street'/'manual'), die PLZ-Normalisierung lässt CRM- und
    GeoNames-Schreibweise aufeinandertreffen (NL '1234 AB' -> '1234'), und der
    Pin-Typ folgt dem Geld: wer je gekauft hat, ist Kunde, nicht Ziel."""
    import datetime as dt
    from adwatch import geo
    from adwatch.models import Company, CrmOrderEvent, PlzGeo
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add(PlzGeo(country="ES", plz="08036", lat=41.39, lng=2.15, place="Barcelona"))
    s.add(PlzGeo(country="NL", plz="1234", lat=52.0, lng=4.3, place="Den Haag"))
    s.add(Company(name="Kaeufer SL", country="ES", postal_code="08036", segment="Handel"))
    s.add(Company(name="Estudio Arq", country="ES", postal_code="08036",
                  segment="Architekten"))
    s.add(Company(name="Privat E.", country="ES", postal_code="08036",
                  segment="Private Endkunden"))
    s.add(Company(name="NL Handel BV", country="NL", postal_code="1234 AB",
                  segment="Handel"))
    s.add(Company(name="Schon genau SL", country="ES", postal_code="08036",
                  segment="Handel", lat=41.11111, lng=2.11111,
                  geocode_precision="street"))
    s.commit()
    kid = s.scalar(select(Company.id).where(Company.name == "Kaeufer SL"))
    s.add(CrmOrderEvent(company_id=kid, order_date=dt.date(2025, 1, 1), amount=3000))
    s.commit()
    s.close()

    r = geo.assign_plz_centroids()
    assert r["geocoded"] == 4, "vier ohne bessere Quelle — auch der Privatkunde darf KOORDINATEN haben"
    assert r["kept_better"] == 1, "'street' wird nie durch einen Zentroid ersetzt"

    s = temp_db.SessionLocal()
    genau = s.scalar(select(Company).where(Company.name == "Schon genau SL"))
    assert abs(genau.lat - 41.11111) < 1e-6, "die genauere Koordinate blieb stehen"
    nl = s.scalar(select(Company).where(Company.name == "NL Handel BV"))
    assert nl.lat == 52.0, "NL-PLZ '1234 AB' trifft die GeoNames-Zeile '1234'"
    s.close()

    es = geo.pins(filters={"country": ["ES"]})
    names = {p["name"]: p for p in es["pins"]}
    assert "Privat E." not in names, "Privatadressen erscheinen auf KEINER Karte"
    assert names["Kaeufer SL"]["typ"] == "kunde", "wer je gekauft hat, ist Kunde"
    assert names["Estudio Arq"]["typ"] == "architekt"
    assert names["Schon genau SL"]["prec"] == "street"

def test_die_projektkarte_pinnt_die_baustelle_nicht_den_firmensitz(temp_db, monkeypatch):
    """Ein Objekt gehört auf die Karte an die Adresse, an der gebaut wird.

    Vier Dinge werden hier festgenagelt, weil jedes einzeln schon einmal
    falsch war oder falsch sein könnte:

    1. Nur die PRIMÄRE Verkaufschance bekommt einen Pin. Die Geschwister sind
       Angebote an verschiedene Firmen für dasselbe Gebäude — ein Pin je VC
       hieße acht Punkte auf einem Haus und eine Karte, die Wettbewerb um
       EIN Projekt als acht Projekte zeigt.
    2. Das Land steht in der Verkaufschance nicht drin (0 von 57.776 Zeilen
       gefüllt). Es wird erschlossen: erst über die Firma, an der die Chance
       hängt, sonst über das Format der PLZ. Geraten wird nie — ohne Treffer
       in plz_geo bleibt das Objekt ohne Koordinate.
    3. Eine genauere Koordinate ('street'/'manual') überschreibt der
       Zentroid-Lauf nicht, genau wie bei den Firmen.
    4. Liste und Karte filtern über DIESELBE Funktion. Die Summe aus Pins und
       'ohne_koordinate' muss deshalb exakt der Gesamtzahl der Liste
       entsprechen — sonst zeigt die Karte stillschweigend eine andere
       Grundgesamtheit, als die Zahl darüber behauptet.
    """
    from adwatch import geo
    from adwatch.insights import projekte
    from adwatch.models import Company, CrmOpportunity, PlzGeo
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add_all([
        PlzGeo(country="DE", plz="80331", lat=48.14, lng=11.57, place="Muenchen"),
        PlzGeo(country="NL", plz="4812", lat=51.59, lng=4.78, place="Breda"),
        PlzGeo(country="AT", plz="5020", lat=47.80, lng=13.04, place="Salzburg"),
        # Der Haendler sitzt in Osnabrueck, baut aber in Muenchen: genau der
        # Unterschied, den die zweite Karte sichtbar machen soll.
        Company(crm_id="F-OS", name="Haendler Osnabrueck", country="DE",
                postal_code="49074", segment="Handel"),
    ])
    s.commit()

    # P1 Muenchen: primaere VC + zwei Geschwister an andere Firmen
    rows = [CrmOpportunity(crm_id="P1-0", opportunity_guid="P1", project_id="P1",
                           project_name="Neubau Muenchen", state="gewonnen",
                           order_value=500000.0, parent_account_crm_id="F-OS",
                           city="Muenchen", postal_code="80331")]
    rows += [CrmOpportunity(crm_id=f"P1-{i}", opportunity_guid=f"G{i}",
                            project_id="P1", state="verloren",
                            city="Muenchen", postal_code="80331")
             for i in (1, 2)]
    # P2: keine Firma dahinter -> Land nur aus dem PLZ-Format ("4812 XN" = NL)
    rows.append(CrmOpportunity(crm_id="P2-0", opportunity_guid="P2", project_id="P2",
                               project_name="Breda", state="offen",
                               city="Breda", postal_code="4812 XN"))
    # P3: schon strassengenau -> der Zentroid-Lauf fasst es nicht an
    rows.append(CrmOpportunity(crm_id="P3-0", opportunity_guid="P3", project_id="P3",
                               project_name="Salzburg genau", state="offen",
                               city="Salzburg", postal_code="5020",
                               lat=47.11111, lng=13.11111,
                               geocode_precision="street"))
    # P4: PLZ, die in keiner plz_geo-Zeile steht -> bleibt ohne Koordinate
    rows.append(CrmOpportunity(crm_id="P4-0", opportunity_guid="P4", project_id="P4",
                               project_name="Nirgendwo", state="offen",
                               city="?", postal_code="99999"))
    s.add_all(rows)
    s.commit()
    s.close()

    monkeypatch.setattr(geo, "SessionLocal", temp_db.SessionLocal)
    monkeypatch.setattr(projekte, "SessionLocal", temp_db.SessionLocal)
    projekte.invalidate_cache()

    r = geo.assign_project_centroids()
    assert r["projects"] == 4, "nur primaere VCs -- die zwei Geschwister zaehlen nicht mit"
    assert r["geocoded"] == 2, "Muenchen ueber die Firma, Breda ueber das PLZ-Format"
    assert r["kept_better"] == 1, "'street' wird nie durch einen Zentroid ersetzt"
    assert r["no_match"] == 1, "99999 steht in keiner Zeile -- und wird nicht geraten"

    s = temp_db.SessionLocal()
    p1 = s.scalar(select(CrmOpportunity).where(CrmOpportunity.crm_id == "P1-0"))
    assert (p1.lat, p1.geocode_country) == (48.14, "DE")
    p2 = s.scalar(select(CrmOpportunity).where(CrmOpportunity.crm_id == "P2-0"))
    assert (p2.lat, p2.geocode_country) == (51.59, "NL"), "'4812 XN' ist niederlaendisch"
    assert s.scalar(select(CrmOpportunity.lat)
                    .where(CrmOpportunity.crm_id == "P1-1")) is None, \
        "Geschwister-VCs bekommen keinen eigenen Pin"
    s.close()

    projekte.invalidate_cache()
    d = geo.project_pins()
    assert {p["name"] for p in d["pins"]} == {"Neubau Muenchen", "Breda", "Salzburg genau"}
    assert d["ohne_koordinate"] == 1, "das Objekt ohne Bauadresse wird GEZAEHLT, nicht verschwiegen"
    muenchen = next(p for p in d["pins"] if p["name"] == "Neubau Muenchen")
    assert muenchen["typ"] == "gewonnen" and muenchen["members"] == 3

    # Liste und Karte muessen dieselbe Grundgesamtheit meinen -- in jedem Filter
    for kw in ({}, {"status": "offen"}, {"min_members": 2}):
        liste = projekte.list_projects(**kw)["total"]
        karte = geo.project_pins(**kw)
        assert liste == karte["total"] + karte["ohne_koordinate"], \
            f"Liste und Karte filtern verschieden bei {kw}"

def test_das_land_der_baustelle_wird_erschlossen_nie_geraten(temp_db, monkeypatch):
    """Die Verkaufschance nennt ihr Land nicht -- also muss es hergeleitet
    werden. Drei Regeln, jede aus einem echten Fehlpin entstanden:

    1. Bei mehreren passenden Laendern entscheidet der ORTSNAME. Vier Ziffern
       sehen in AT, DK, CH, BE und NO gleich aus; ohne diesen Schritt stand ein
       Projekt in Kobenhavn in Oesterreich (132 solcher Pins).
    2. Ausserhalb der Formatgruppe schlaegt das FORMAT das Firmenland. Eine
       britische Postleitzahl ist britisch, auch wenn die Firma in Deutschland
       gemeldet ist -- 2.088 Objekte haengen daran.
    3. Innerhalb derselben Formatgruppe wird NICHT geraten. Trifft das
       Firmenland nicht und bestaetigt der Ort nichts, faellt das Objekt unter
       "ohne Bauadresse" -- und eine frueher geschriebene Koordinate wird dabei
       wieder WEGGENOMMEN, sonst ueberlebt der falsche Pin die Regel, die ihn
       verhindern soll.
    """
    from adwatch import geo
    from adwatch.insights import projekte
    from adwatch.models import Company, CrmOpportunity, PlzGeo
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add_all([
        # dieselben vier Ziffern in zwei Laendern -- nur der Ort trennt sie
        PlzGeo(country="AT", plz="8280", lat=47.04, lng=16.05, place="Fuerstenfeld"),
        PlzGeo(country="DK", plz="8280", lat=56.12, lng=10.14, place="Viby J"),
        PlzGeo(country="GB", plz="CT15", lat=51.15, lng=1.30, place="Dover"),
        PlzGeo(country="AT", plz="4711", lat=48.20, lng=13.50, place="Aurolzmuenster"),
        Company(crm_id="F-DK", name="Daenische Bau A/S", country="DK"),
        Company(crm_id="F-DE", name="Deutsche GmbH", country="DE"),
    ])
    s.add_all([
        # 1. Ortsname entscheidet: Firma ohne Land, PLZ in AT und DK
        CrmOpportunity(crm_id="A", opportunity_guid="A", project_id="A",
                       project_name="Viby", state="offen",
                       city="Viby J", postal_code="8280"),
        # 2. GB-Format unter deutscher Firma -> bleibt GB
        CrmOpportunity(crm_id="B", opportunity_guid="B", project_id="B",
                       project_name="Dover", state="offen",
                       parent_account_crm_id="F-DE",
                       city="Dover", postal_code="CT15 6DZ"),
        # 3. Daenische Firma, PLZ gibt es nur in AT, Ort bestaetigt nichts.
        #    Traegt schon eine (falsche) Koordinate aus einem frueheren Lauf.
        CrmOpportunity(crm_id="C", opportunity_guid="C", project_id="C",
                       project_name="Vertippt", state="offen",
                       parent_account_crm_id="F-DK",
                       city="Aarhus", postal_code="4711",
                       lat=48.20, lng=13.50, geocode_precision="plz",
                       geocode_country="AT"),
    ])
    s.commit(); s.close()

    monkeypatch.setattr(geo, "SessionLocal", temp_db.SessionLocal)
    monkeypatch.setattr(projekte, "SessionLocal", temp_db.SessionLocal)
    projekte.invalidate_cache()
    r = geo.assign_project_centroids()

    s = temp_db.SessionLocal()
    hol = lambda cid: s.scalar(select(CrmOpportunity)
                               .where(CrmOpportunity.crm_id == cid))
    a, b, c = hol("A"), hol("B"), hol("C")
    assert a.geocode_country == "DK" and a.lat == 56.12, \
        "'Viby J' ist daenisch -- der Ortsname schlaegt die Kandidatenreihenfolge"
    assert b.geocode_country == "GB", \
        "eine britische Postleitzahl bleibt britisch, egal wo die Firma sitzt"
    assert (c.lat, c.lng, c.geocode_country, c.geocode_precision) == (None, None, None, None), \
        "geraten wird nicht -- und der alte falsche Pin muss dabei verschwinden"
    s.close()

    assert r["geocoded"] == 2 and r["no_match"] == 1
    # zweimal laufen lassen aendert nichts mehr
    assert geo.assign_project_centroids() == r, "der Lauf ist wiederholbar"

def test_laender_erkennt_vorwahl_ortsname_und_landesnamen(temp_db, monkeypatch):
    """Die drei tragenden Signale, an einem Text mit bekannter Antwort."""
    from adwatch.enrich import laender

    # Ortsindex fest verdrahten, damit der Test nicht an plz_geo haengt
    monkeypatch.setattr(laender, "_ORT_INDEX", {
        "marbella": {"ES": 7}, "malaga": {"ES": 26}, "hamburg": {"DE": 42},
        "porto": {"PT": 4302, "ES": 1},          # mehrdeutig, PT klar groesser
        "bauen": {"CH": 1},                      # Ein-PLZ-Falle
    })

    r = laender.laender_aus_text(
        "Buero in Hamburg. Telefon +49 40 123456. Projekte in Marbella und Malaga.",
        heimat="DE", tld="de")
    assert r["laender"]["DE"]["sicherheit"] == "sicher"
    assert r["laender"]["ES"]["sicherheit"] == "sicher", "zwei Staedte muessen reichen"
    assert "marbella" in r["laender"]["ES"]["belege"]

def test_laender_ein_plz_ort_traegt_kein_land(temp_db, monkeypatch):
    """`bauen` ist ein Dorf in Uri UND ein deutsches Verb. Gemessen am echten
    Crawl war das der haeufigste Fehlalarm -- er darf die Schweiz nicht auf
    'moeglich' heben."""
    from adwatch.enrich import laender

    monkeypatch.setattr(laender, "_ORT_INDEX", {
        "bauen": {"CH": 1}, "hamburg": {"DE": 42},
    })
    r = laender.laender_aus_text(
        "Wir planen und Bauen in Hamburg.", heimat="DE", tld="de")
    assert r["laender"].get("CH", {}).get("sicherheit") != "sicher"
    assert r["laender"].get("CH", {}).get("sicherheit") != "moeglich"

def test_laender_kleingeschriebenes_wort_ist_kein_ort(temp_db, monkeypatch):
    """'un proyecto real' ist kein Ort in Portugal. Grossschreibung ist der
    Filter, der das trennt."""
    from adwatch.enrich import laender

    monkeypatch.setattr(laender, "_ORT_INDEX", {"malaga": {"ES": 26}})
    r = laender.laender_aus_text(
        "Un proyecto real en el campo. Estudio en Malaga. +34 952 000 000.",
        heimat="ES", tld="es")
    assert set(k for k, v in r["laender"].items()
               if v["sicherheit"] == "sicher") == {"ES"}

def test_laender_mehrdeutiger_ort_wird_nie_geraten(temp_db, monkeypatch):
    """Bleibt ein Ortsname mehrdeutig, wird BEIDES vermerkt statt eines
    stillen Muenzwurfs -- der Fehler, der schon ein daenisches Projekt nach
    Oesterreich gepinnt hat."""
    from adwatch.enrich import laender

    # gleich gross in beiden Laendern -> kein Stichentscheid moeglich
    monkeypatch.setattr(laender, "_ORT_INDEX", {"zwillingsort": {"SE": 9, "NO": 9}})
    r = laender.laender_aus_text("Projekt in Zwillingsort.", heimat=None, tld=None)
    assert r["unsicher"], "der Fall muss als unsicher herauskommen"
    assert "SE" not in {k for k, v in r["laender"].items() if v["sicherheit"] == "sicher"}

def test_laender_exonyme_ueberleben_die_faltung():
    """'Ibiza' steht in plz_geo als 'Eivissa', 'Munich' als 'Muenchen'. Die
    Bruecke dafuer ist die Exonym-Tabelle -- und jeder ihrer Schluessel muss
    in derselben Form stehen, die _falten() erzeugt, sonst greift er nie."""
    from adwatch.enrich import laender

    schlecht = [k for k in laender._EXONYME if laender._falten(k) != k]
    assert not schlecht, f"Exonyme in falscher Schreibung: {schlecht}"
    assert laender._EXONYME["ibiza"] == "ES"

def test_laender_vornamen_tragen_kein_land(temp_db, monkeypatch):
    """Gemessen an 1.483 deutschen Bueros: abdelkader.de kam auf 'aktiv in
    Spanien', und die Belege waren `cristina, felix, roman, teresa` -- die
    VORNAMEN der Team-Seite, zu denen plz_geo je ein Dorf fuehrt. Vier kleine
    Orte ergaben zusammen die Schwelle, ohne einen echten Hinweis auf Spanien.

    Die Schranke dagegen ist strukturell, nicht als Namensliste: 'sicher'
    braucht mindestens EINEN starken Beleg -- Vorwahl, Landesname, Exonym oder
    eine Stadt ab _GROSSE_STADT Postleitzahlen."""
    from adwatch.enrich import laender

    monkeypatch.setattr(laender, "_ORT_INDEX", {
        "cristina": {"ES": 1}, "teresa": {"ES": 2}, "roman": {"ES": 2},
        "felix": {"ES": 1}, "berlin": {"DE": 181},
    })
    r = laender.laender_aus_text(
        "Unser Team: Cristina, Felix, Roman und Teresa. Buero in Berlin. +49 30 1.",
        heimat="DE", tld="de")
    assert r["laender"]["ES"]["sicherheit"] != "sicher", \
        "vier Kleinstorte duerfen kein Land tragen"
    assert r["laender"]["ES"]["belege"], "sichtbar bleiben muss der Fund trotzdem"
    assert r["laender"]["DE"]["sicherheit"] == "sicher"

def test_laender_eine_grossstadt_ist_moeglich_zwei_sind_sicher(temp_db, monkeypatch):
    """Eine einzelne genannte Stadt kann eine Konferenz oder ein Lieferant sein.
    Sie wird als 'moeglich' gefuehrt, nicht verworfen und nicht behauptet."""
    from adwatch.enrich import laender

    monkeypatch.setattr(laender, "_ORT_INDEX", {
        "barcelona": {"ES": 46}, "madrid": {"ES": 63}, "hamburg": {"DE": 42},
    })
    eine = laender.laender_aus_text(
        "Buero Hamburg +49 40 1. Projekt in Barcelona.", heimat="DE", tld="de")
    assert eine["laender"]["ES"]["sicherheit"] == "moeglich"

    zwei = laender.laender_aus_text(
        "Buero Hamburg +49 40 1. Projekte in Barcelona und Madrid.",
        heimat="DE", tld="de")
    assert zwei["laender"]["ES"]["sicherheit"] == "sicher"

def test_grossstadt_schwelle_ist_je_land_verschieden(temp_db, monkeypatch):
    """Eine feste Zahl kann es nicht geben: in Deutschland heisst '10
    Postleitzahlen' Grossstadt (ueber dem 99. Perzentil), in Portugal ist es
    unterdurchschnittlich, weil die PLZ dort strassenfein sind.

    Gemessen an plz_geo -- p99: DE 6, ES 4, PT 176, SE 126. Genau daran ging
    gernotschulzarchitektur.de als 'sicher in Portugal' durch, belegt mit
    `rande` (30 PLZ, ein Weiler) und `fundada` (portugiesisch 'gegruendet')."""
    from adwatch.enrich import laender

    monkeypatch.setattr(laender, "_SCHWELLEN", None)
    monkeypatch.setattr(laender, "_ORT_INDEX", {
        # PT: viele Orte mit vielen PLZ -> hohe Schwelle
        **{f"ptdorf{i}": {"PT": 20 + i} for i in range(100)},
        "lissabonstadt": {"PT": 900},
        # DE: fast alle Orte mit einer PLZ -> niedrige Schwelle
        **{f"dedorf{i}": {"DE": 1} for i in range(100)},
        "grossstadt": {"DE": 150},
    })
    pt = laender._grossstadt_schwelle("PT")
    de = laender._grossstadt_schwelle("DE")
    assert pt > de, f"PT-Schwelle ({pt}) muss ueber der deutschen ({de}) liegen"
    assert de >= 3, "nie unter 3, sonst gilt jeder Weiler als gross"

def test_ein_weiler_traegt_kein_fremdes_land(temp_db, monkeypatch):
    """`rande` hat 30 portugiesische Postleitzahlen und ist trotzdem ein Weiler
    -- auf Deutsch ausserdem der Rand von etwas. Es darf Portugal nicht auf
    'sicher' heben, waehrend `lisbon` (kuratiertes Exonym) es sehr wohl darf."""
    from adwatch.enrich import laender

    monkeypatch.setattr(laender, "_SCHWELLEN", None)
    monkeypatch.setattr(laender, "_ORT_INDEX", {
        **{f"ptdorf{i}": {"PT": 20 + i} for i in range(200)},
        "rande": {"PT": 30},
        "fundada": {"PT": 3},
        "berlin": {"DE": 181},
        **{f"dedorf{i}": {"DE": 1} for i in range(200)},
    })
    schwach = laender.laender_aus_text(
        "Buero in Berlin, +49 30 1. Fundada am Rande der Stadt.",
        heimat="DE", tld="de")
    assert schwach["laender"].get("PT", {}).get("sicherheit") != "sicher"

    # Ein kuratiertes Exonym ist ein STARKER Beleg -- aber eine einzelne
    # genannte Stadt bleibt trotzdem 'moeglich'. Die Staerke entscheidet, ob
    # ein Land ueberhaupt 'sicher' werden DARF, nicht ob es das schon ist.
    eine = laender.laender_aus_text(
        "Buero in Berlin, +49 30 1. Projekt in Lisbon.", heimat="DE", tld="de")
    assert eine["laender"]["PT"]["sicherheit"] == "moeglich"

    # Zwei Belege, davon einer stark -> sicher. Der Weiler `rande` darf dabei
    # mitzaehlen, nur eben nicht allein tragen.
    zwei = laender.laender_aus_text(
        "Buero in Berlin, +49 30 1. Projekte in Lisbon und am Rande.",
        heimat="DE", tld="de")
    assert zwei["laender"]["PT"]["sicherheit"] == "sicher"

def test_staedteliste_traegt_orte_und_keine_namen(temp_db, monkeypatch):
    """Iheb wollte je Buero die spanischen STAEDTE sehen. Die Belege taugen
    dafuer nicht: dort stehen Vorwahl und Landesname mit drin, und sie sind auf
    acht Eintraege gekappt. `staedte` ist die eigene, ungekappte Liste --
    und muss zwei Sorten Rauschen draussen halten, beide im Probelauf
    aufgetaucht: Vornamen von Team-Seiten und Laendernamen, die plz_geo als
    Ort fuehrt ('Espana', 'Nederland')."""
    from adwatch.enrich import laender

    monkeypatch.setattr(laender, "_SCHWELLEN", None)
    monkeypatch.setattr(laender, "_ORT_INDEX", {
        "marbella": {"ES": 7}, "andratx": {"ES": 3}, "malaga": {"ES": 26},
        "maria": {"ES": 2}, "espana": {"ES": 4},
        **{f"esdorf{i}": {"ES": 1} for i in range(200)},
    })
    monkeypatch.setattr(laender, "_ANZEIGE", {
        "marbella": "Marbella", "andratx": "Andratx", "malaga": "Málaga"})

    r = laender.laender_aus_text(
        "Estudio en Malaga. +34 952 1. Proyectos en Marbella y Andratx, "
        "en toda Espana. Nuestro equipo: Maria.", heimat="ES", tld="es")
    staedte = r["laender"]["ES"]["staedte"]
    assert "Marbella" in staedte and "Andratx" in staedte
    assert not any(x.lower() == "maria" for x in staedte), "Vorname ist keine Stadt"
    assert not any(x.lower() == "espana" for x in staedte), "Land ist keine Stadt"
    # die Belege duerfen beides weiter enthalten -- sie sind der Nachweis,
    # nicht die Auswertung
    assert "maria" in r["laender"]["ES"]["belege"]

def test_ortsname_haelt_verbindungswoerter_klein():
    """plz_geo ist durchweg titelgeschrieben und fuehrt 'Palma De Mallorca'.
    In einer Staedteliste sieht das nach Datenfehler aus."""
    from adwatch.enrich import laender

    assert laender.ortsname("palma de mallorca") == "Palma de Mallorca"
    assert laender.ortsname("frankfurt am main") == "Frankfurt am Main"

def test_mitarbeiterliste_ist_kein_ortsverzeichnis(temp_db, monkeypatch):
    """Gemessen an bofill.com, das neun Seiten mit langen Credits fuehrt:

        "… Daniela Flores, Victor Galera, Patricia Llasera, Luis Carpio …"
        "… Design Principal Hernan Cortes  Management Javier Guardiola …"

    Flores, Galera, Carpio, Cortes sind spanische NACHNAMEN -- und zu jedem
    fuehrt plz_geo ein Dorf. Dadurch stand ein einziges Buero an einem Dutzend
    erfundener Orte, und dieselben Bueros zogen sich durch die ganze Liste.

    Zwei Regeln, beide ohne Nachnamensliste (die waere aussichtslos):
    drei kommagetrennte Paare grossgeschriebener Woerter sind eine
    Personenliste, und ein VORNAME unmittelbar davor macht aus dem naechsten
    Wort einen Nachnamen."""
    from adwatch.enrich import laender

    monkeypatch.setattr(laender, "_SCHWELLEN", None)
    monkeypatch.setattr(laender, "_ORT_INDEX", {
        "flores": {"ES": 1}, "galera": {"ES": 1}, "carpio": {"ES": 1},
        "cortes": {"ES": 1}, "manzanares": {"ES": 1},
        "marbella": {"ES": 7}, "malaga": {"ES": 26},
        **{f"esdorf{i}": {"ES": 1} for i in range(200)},
    })
    monkeypatch.setattr(laender, "_ANZEIGE",
                        {"marbella": "Marbella", "malaga": "Málaga"})

    r = laender.laender_aus_text(
        "Team: Daniela Flores, Victor Galera, Patricia Llasera, Luis Carpio. "
        "Design Principal Hernan Cortes. Management Jaime Manzanares. "
        "Projekte in Marbella und Malaga. +34 952 1.", heimat="ES", tld="es")
    staedte = {x.lower() for x in r["laender"]["ES"]["staedte"]}
    assert "marbella" in staedte and "málaga" in staedte
    for nachname in ("flores", "galera", "carpio", "cortes", "manzanares"):
        assert nachname not in staedte, f"{nachname} ist ein Nachname, kein Ort"

def test_region_aus_postleitzahl(temp_db, monkeypatch):
    """Ort \u2192 Provinz \u2192 Autonome Gemeinschaft, ohne Dienst und ohne Schl\u00fcssel.

    Die ersten zwei Ziffern der spanischen PLZ sind die Provinz; die Zuordnung
    Provinz \u2192 Region ist ein feststehendes Verzeichnis. Damit l\u00e4sst sich nach
    \u201eKatalonien" filtern, ohne eine Spalte zu kaufen.
    """
    from sqlalchemy import text as _t

    from adwatch.enrich import regionen

    monkeypatch.setattr(regionen, "SessionLocal", temp_db.SessionLocal)
    regionen._index = None
    regionen._schreibweisen = None
    s = temp_db.SessionLocal()
    s.execute(_t("CREATE TABLE IF NOT EXISTS plz_geo (id INTEGER PRIMARY KEY, "
                 "country TEXT, plz TEXT, lat REAL, lng REAL, place TEXT)"))
    for plz, ort in (("08001", "Barcelona"), ("28001", "Madrid"),
                     ("29602", "Marbella"), ("07001", "Palma De Mallorca"),
                     ("23670", "Los Villares"), ("37183", "Los Villares"),
                     ("37184", "Los Villares")):
        s.execute(_t("INSERT INTO plz_geo (country, plz, lat, lng, place) "
                     "VALUES ('ES', :p, 0.0, 0.0, :o)"), {"p": plz, "o": ort})
    s.commit(); s.close()

    assert regionen.einordnen("Barcelona")["region"] == "Cataluña"
    assert regionen.einordnen("Marbella")["region_de"] == "Andalusien"
    assert regionen.einordnen("Madrid")["provinz"] == "Madrid"
    # Inseln haben keine eigene PLZ und kommen aus der Handtabelle
    assert regionen.einordnen("Mallorca")["region"] == "Illes Balears"
    # Ein Ortsname in zwei Provinzen: die haeufigere gewinnt, und die Spalte
    # sagt, dass geraten wurde.
    mehrdeutig = regionen.einordnen("Los Villares")
    assert mehrdeutig["provinz"] == "Salamanca"      # 2 PLZ gegen 1
    assert mehrdeutig["eindeutig"] is False
    assert regionen.einordnen("Kein Ort Dieser Welt")["region"] is None
    # Die Verbindungswoerter bleiben klein, obwohl die Quelle sie gross fuehrt
    assert regionen.ort_schoen("palma de mallorca") == "Palma de Mallorca"

def test_spanischer_ort_braucht_einen_grund():
    """Ein spanischer Ortsname auf einer Projektseite z\u00e4hlt nur mit Beleg.

    Gemessen 2026-09-09: acme.ac (London) meldete neun spanische Projekte \u2014
    \u201eCanopy by Hilton, London City" mit dem Ort \u201eMaria", \u201eMamsha Gardens" mit
    \u201eCastillo" und \u201eJavier". Das waren die Namen der Projektteams. Spanische
    Vor- und Nachnamen sind fast immer auch Gemeindenamen, und die Gr\u00f6\u00dfe des
    Ortes trennt sie nicht: Andratx, Calvi\u00e0 und Sitges haben genauso genau eine
    Postleitzahl wie Maria, Borja und Cabra.
    """
    from adwatch.enrich.tiefenlauf import _ort_belegt

    # Titel \u2014 der staerkste Beleg
    assert _ort_belegt("andratx", "ausbau ferienhaus mallorca, port andratx",
                       "ausbau ferienhaus mallorca, port andratx", 1) == "im Projekttitel"
    # Insel
    assert _ort_belegt("mallorca", "villa", "villa auf mallorca", 0) == "Insel oder Region"
    # Grossstadt
    assert _ort_belegt("madrid", "buerogebaeude", "neubau in madrid", 63).startswith("Stadt")
    # Landesname daneben
    assert _ort_belegt("sitges", "casa s", "casa s in sitges, spanien",
                       1) == "neben dem Landesnamen"
    # Und der Fall, der die Regel ausgeloest hat: ein Vorname im Projektteam
    assert _ort_belegt("maria", "canopy by hilton, london city",
                       "canopy by hilton, london city. team: maria, javier", 1) is None
    assert _ort_belegt("castillo", "mamsha gardens",
                       "mamsha gardens abu dhabi. castillo, javier", 1) is None

def test_niederlassung_braucht_mehr_als_das_wort_spanien(temp_db, monkeypatch):
    """Eine deutsche PLZ ist kein Beleg f\u00fcr eine spanische Niederlassung.

    Der erste Anlauf verlangte \u201ezwei von drei Belegen" (PLZ, +34, das Wort
    Spanien) und meldete bei holle-architekten.de eine Niederlassung: die
    Belege waren \u201e45133 Essen" und das Wort \u201eSpanien" irgendwo im Text. Der
    deutsche Postleitzahlenbereich liegt fast vollst\u00e4ndig \u00fcber dem spanischen,
    also ist eine f\u00fcnfstellige Zahl allein wertlos.
    """
    from sqlalchemy import text as _t

    from adwatch.enrich import tiefenlauf

    monkeypatch.setattr(tiefenlauf, "SessionLocal", temp_db.SessionLocal)
    s = temp_db.SessionLocal()
    s.execute(_t("CREATE TABLE IF NOT EXISTS plz_geo (id INTEGER PRIMARY KEY, "
                 "country TEXT, plz TEXT, lat REAL, lng REAL, place TEXT)"))
    s.execute(_t("INSERT INTO plz_geo (country, plz, lat, lng, place) "
                 "VALUES ('ES','07157',39.54,2.39,'Port d''Andratx')"))
    s.commit(); s.close()

    # Der Fall aus dem Probelauf: deutsche Adresse, Wort „Spanien" im Text
    assert tiefenlauf._niederlassung(
        ["Holle Architekten, Meisenburgstr. 173, 45133 Essen. "
         "Wir bauen auch in Spanien."]) is None
    # Eine echte spanische Adresse: PLZ UND ihr Ort
    echt = tiefenlauf._niederlassung(["Oficina Mallorca, 07157 Port d'Andratx"])
    assert echt and echt["plz_mit_ort"].startswith("07157")
    # Oder die Vorwahl
    vorwahl = tiefenlauf._niederlassung(["Estudio Madrid  T +34 91 123 45 67"])
    assert vorwahl and vorwahl["vorwahl_34"] is True

def test_uebersichtsseite_ist_kein_projekt():
    """`big.dk/projects/architecture` besteht den Pfadtest, ist aber der Katalog.

    Erkennbar strukturell: die Adresse einer \u00dcbersicht ist der ANFANG der
    Adressen ihrer Eintr\u00e4ge. Ohne diese Regel z\u00e4hlte der Katalog als Projekt \u2014
    mit dem Seitentitel als Projekttitel (\u201eBjarke Ingels Group") und den Orten
    aller verlinkten Projekte.
    """
    from adwatch.enrich.tiefenlauf import _projekte_indexseiten_raus

    roh = [{"url": "https://big.dk/projects/architecture", "titel": "BIG"},
           {"url": "https://big.dk/projects/architecture/mountain-dwellings", "titel": "A"},
           {"url": "https://big.dk/projects/architecture/8-house", "titel": "B"},
           {"url": "https://big.dk/projects/architecture/via-57", "titel": "C"},
           {"url": "https://big.dk/projects/landscape/balconies", "titel": "D"}]
    aus = _projekte_indexseiten_raus(roh)
    adressen = {p["url"] for p in aus}
    assert "https://big.dk/projects/architecture" not in adressen
    assert len(aus) == 4

def test_verlagsort_ist_kein_projektort():
    """Der Ort in einer Literaturangabe ist der Sitz des Verlags.

    herzogdemeuron.com f\u00fchrt unter jedem Projekt sein Literaturverzeichnis:

        Vol. No. 089, Madrid, Arquitectura Viva SL, 2018. pp. 40\u201347.

    Ohne diese Regel lagen 140 von 223 spanischen \u201eProjekten" in Madrid \u2014
    darunter \u201eSt. Jakob-Park Basel". Ernsthafte B\u00fcros pflegen solche
    Verzeichnisse, der Fehler trifft also gerade die interessantesten Adressen.
    """
    from adwatch.enrich.tiefenlauf import _ist_literaturangabe, _ort_belegt

    zitat = ("st. jakob-park basel. in: luis fernandez-galiano (ed.): arquitectura "
             "viva. herzog & de meuron 1978-2007. 2nd rev. ed. madrid, "
             "arquitectura viva, 2007. vol. no. 109/110, madrid, el croquis, 2002.")
    assert _ort_belegt("madrid", "st. jakob-park basel", zitat, 63) is None

    # Dasselbe B\u00fcro hat ein ECHTES Madrider Projekt \u2014 das muss bleiben.
    echt = "caixaforum madrid. umbau eines kraftwerks in madrid, spanien."
    assert _ort_belegt("madrid", "caixaforum madrid", echt, 63) == "im Projekttitel"

    # Ein Ort, der einmal im Zitat und einmal im Text steht, zaehlt.
    gemischt = ("wohnhaus. der bau steht in sevilla. in: el croquis, "
                "vol. 129, sevilla, 2006.")
    assert _ort_belegt("sevilla", "wohnhaus", gemischt, 24) is not None

    # Die Form „Ort, Verlag, Jahr" allein reicht als Erkennung
    assert _ist_literaturangabe("x madrid, el croquis, 2006", 2, 8) is True

def test_anhang_wird_abgeschnitten():
    """Literaturverzeichnis und Verwandtenliste stehen HINTER dem Projekt.

    Eine Projektseite von herzogdemeuron.com hat drei Teile:

        0    - 4000   das Projekt
        4078 - 6748   das Literaturverzeichnis
        7000 - 7988   "weitere Projekte" mit Titeln und Orten

    Der Ort des Projekts steht im ersten. Die anderen tragen die Orte von
    VERLAGEN und von ANDEREN Projekten -- so wurde "St. Jakob-Park Basel" zu
    einem Projekt in Barcelona (aus "313 nou camp nou barcelona, spain" in der
    Verwandtenliste) und zu einem in Madrid (aus "vol. no. 89, madrid,
    arquitectura viva").
    """
    from adwatch.enrich.tiefenlauf import ohne_anhang

    projekt = "a" * 900 + " stadion in basel, fertig 2001. "
    voll = projekt + "in: el croquis, vol. 129, madrid, 2006. "                      "weitere projekte: 313 nou camp nou barcelona, spain."
    gekuerzt = ohne_anhang(voll)
    assert "madrid" not in gekuerzt
    assert "barcelona" not in gekuerzt
    assert "basel" in gekuerzt

    # Kurze Seiten werden nicht zerschnitten, auch wenn sie frueh zitieren.
    kurz = "haus am hang, mallorca. in: bauwelt 2019."
    assert ohne_anhang(kurz) == kurz

    # Ohne Anhang bleibt alles stehen.
    ohne = "b" * 2000 + " projekt in sevilla"
    assert ohne_anhang(ohne) == ohne

def test_orte_die_immer_gemeinsam_auftreten(temp_db, monkeypatch):
    """Zwei Orte auf fast denselben Seiten sind ein Artefakt — aber welches?

    Gemessen ueber die 82 Bueros (Jaccard der Seitenmengen):

        behzadi-architekten.de   madrid 196 | barcelona 193   0,93
        herzogdemeuron.com       tenerife 19 | santa cruz 18  0,95
        cruzyortiz.com           seville 64 | madrid 43       0,26
        mathes.de                mallorca 42 | ibiza 9        0,16

    Haeufigkeit allein trennt sie NICHT: behzadis Madrid steht auf 49 % der
    Seiten, Cruz y Ortiz' Sevilla auf ebenfalls 49 % — nur ist das eine ein
    Phantom aus einer eingebetteten Projektliste und das andere die Heimatstadt
    eines sevillanischen Buros. Echte Projektorte WECHSELN.

    Zwei Ursachen, zwei Heilungen: Madrid+Barcelona kommen aus einer geteilten
    Liste und muessen beide weg; tenerife+santa cruz sind EIN Ort (Santa Cruz
    de Tenerife) und gehoeren zusammengefasst, sonst faellt ein echtes Projekt
    heraus.
    """
    from sqlalchemy import text as _t

    from adwatch.enrich import regionen, tiefenlauf

    monkeypatch.setattr(regionen, "SessionLocal", temp_db.SessionLocal)
    regionen._index = None
    s = temp_db.SessionLocal()
    s.execute(_t("CREATE TABLE IF NOT EXISTS plz_geo (id INTEGER PRIMARY KEY, "
                 "country TEXT, plz TEXT, lat REAL, lng REAL, place TEXT)"))
    s.execute(_t("INSERT INTO plz_geo (country, plz, lat, lng, place) "
                 "VALUES ('ES','38001',28.46,-16.25,'Santa Cruz de Tenerife')"))
    s.commit(); s.close()

    def seiten(n, orte, praefix):
        return [{"url": f"https://x.de/{praefix}{i}", "titel": f"Projekt {i}",
                 "orte_es": list(orte), "orte_andere": {}, "gruende": {},
                 "hat_ort": True} for i in range(n)]

    # Geteilte Projektliste: beide Orte verschwinden
    aus, bericht = tiefenlauf._ortspaare_bereinigen(
        seiten(20, ("madrid", "barcelona"), "l"))
    assert set(bericht) == {"madrid", "barcelona"}
    assert all(not p["orte_es"] for p in aus)

    # Ein Ort, doppelt erkannt: zusammengefasst statt geloescht
    aus2, _ = tiefenlauf._ortspaare_bereinigen(
        seiten(19, ("tenerife", "santa cruz"), "t"))
    assert all(p["orte_es"] == ["santa cruz de tenerife"] for p in aus2)

    # Wechselnde echte Orte bleiben unangetastet
    import random
    random.seed(1)
    echt = [{"url": f"https://y.de/p{i}", "titel": "x",
             "orte_es": random.sample(["seville", "madrid", "granada", "cadiz"], 2),
             "orte_andere": {}, "gruende": {}, "hat_ort": True} for i in range(40)]
    aus3, bericht3 = tiefenlauf._ortspaare_bereinigen(echt)
    assert bericht3 == {}
    assert sum(len(p["orte_es"]) for p in aus3) == 80

def test_projektort_steht_in_einer_engen_zone():
    """Der Projektort steht im Titel, in einem beschrifteten Feld oder im Kopf.

    Eine Stichprobe von 14 erkannten Projektzeilen, gegen die echten Seiten
    gelesen: 2 richtig, 12 falsch. Die falschen kamen aus
    Literaturverzeichnissen, Verwandtenlisten, eingebetteten Projektlisten und
    aus Werbeprosa -- "Vienna coffee-house meets Barcelona" machte ein Wiener
    Lokal zu einem Projekt in Barcelona.

    Dieselben Seiten mit der Zonenregel: 11 von 11 richtig.
    """
    from adwatch.enrich.tiefenlauf import ortszone

    # 1. Beschriftetes Feld gewinnt
    z = ortszone("Plaza Norte 2", "Plaza Norte 2 Location City: Madrid, Spain Renovation")
    assert "Madrid" in z and "Renovation" in z

    # 2. Ohne Feld: der Bereich hinter dem letzten Vorkommen des Titels.
    #    Davor steht bei grossen Bueros die ganze Kopf- und Fusszeile.
    seite = ("226 National Stadium - Herzog & de Meuron Menu Close News Projects "
             "Basel, Switzerland Email: info@ "
             "226 National Stadium Main Stadium for the 2008 Olympic Games "
             "Beijing, China Competition 2002")
    z2 = ortszone("226 National Stadium", seite)
    assert "Beijing" in z2
    assert "Basel" not in z2          # der Bueros itz faellt heraus

    # 3. Der Anhang wird auch in der Zone abgeschnitten: bei mathes.de stand
    #    "weitere projekte ... Villa, Mallorca" direkt hinter einem
    #    IBIZA-Projekt und machte daraus ein Mallorca-Projekt.
    z3 = ortszone("Wohlfuehloase im Sueden, Ibiza",
                  "Wohlfuehloase im Sueden, Ibiza Mehr erfahren "
                  "Weitere Projekte Villa, Mallorca Mehr erfahren")
    assert "Ibiza" in z3
    assert "Mallorca" not in z3


# ---------------------------------------------------------------------------
# Vorabtest und Modellurteil -- die zwei Module, auf denen die Spanien-Zahl
# steht und die bis zum Aufraeumen keinen einzigen Test hatten. Ein stiller
# Fehler in ihnen sieht aus wie "diese Bueros bauen halt nicht in Spanien".
# ---------------------------------------------------------------------------

def test_vorabtest_ist_absichtlich_grosszuegig(monkeypatch):
    """Stufe 1 darf lieber zu oft anspringen als einmal zu wenig.

    Sie entscheidet, welche Domain ueberhaupt tief gelesen wird. Was hier
    durchfaellt, wird nie wieder angesehen -- ein uebersehenes Buero ist
    endgueltig weg, ein Fehlalarm kostet ein paar Seiten. Gemessene
    Trefferquote auf den bestaetigten Bueros: 97 %.
    """
    from adwatch.enrich.spanienverdacht import gruende, verdaechtig

    # Jedes einzelne Signal muss fuer sich allein reichen
    assert "Spanien-Wort im Text" in gruende("Wohnhaus in Mallorca")
    assert "Vorwahl +34" in gruende("Tel. +34 971 123456")
    assert "spanische PLZ mit Ort" in gruende("Sitz: 08006 Barcelona")
    assert ".es-Verweis" in gruende("", html='<a href="https://estudio.es/x">')
    assert "spanische Fachwörter" in gruende("Reforma de una vivienda")
    assert any(g.startswith("Ort in der Adresse")
               for g in gruende("", url="https://x.de/projekte/ibiza-haus"))

    # Und das Rauschen ist gewollt: "Ronda" und "Maria" sind echte spanische
    # Gemeinden UND gewoehnliche Woerter. Stufe 1 meldet sie, Stufe 2 wirft
    # sie raus. Andersherum -- hier schon filtern -- waere der teure Fehler.
    #
    # Der Ortsabgleich braucht den Index aus plz_geo. Der wird prozessweit
    # einmal gebaut, und in einem Testlauf hat ihn womoeglich schon eine leere
    # Wegwerf-Datenbank gefuellt -- deshalb hier ein bekannter Index statt
    # dessen, was zufaellig im Cache liegt.
    from adwatch.enrich import laender
    monkeypatch.setattr(laender, "_ORT_INDEX", {"ronda": {"ES": 12}})
    assert verdaechtig("Haus Ronda am Hang")

    # Eine Seite ohne jeden Bezug loest nichts aus, sonst waere der ganze
    # Vorabtest wertlos und jede der 10.212 Domains wuerde tief gecrawlt.
    assert gruende("Umbau eines Bauernhauses im Allgaeu, Fertigstellung 2019") == []
    assert not verdaechtig("Neubau einer Kindertagesstaette in Rostock")


def test_vorabtest_nimmt_die_erste_fundstelle_und_hoert_auf(monkeypatch):
    """Die Reihenfolge Startseite -> Uebersichten -> Sitemap ist der Preis.

    Ein Buero, dessen Startseite schon "Mallorca" sagt, darf genau EINE Seite
    kosten. Wuerde die Funktion weitersuchen, waere aus 2,6 Seiten je Domain
    schnell ein Vielfaches -- bei 10.212 Domains ist das der Unterschied
    zwischen einer Nacht und einer Woche.
    """
    from adwatch.enrich import tiefenlauf, vorlauf
    from adwatch.identity import website_source as ws

    geholt = []
    monkeypatch.setattr(tiefenlauf, "_startseite", lambda d: {
        "home_url": f"https://{d}/", "home_html": "<p>Villa in Mallorca</p>"})
    monkeypatch.setattr(ws, "_fetch_url", lambda u, timeout=0: geholt.append(u) or None)
    monkeypatch.setattr(tiefenlauf, "_sitemap_alles",
                        lambda d, grenze=0: geholt.append("sitemap") or [])

    r = vorlauf.eine_domain("beispiel.de")
    assert r["verdacht"] is True
    assert r["seiten"] == 1
    assert geholt == [], "nach dem ersten Treffer darf nichts mehr geholt werden"


def test_vorabtest_meldet_unerreichbar_statt_unverdaechtig(monkeypatch):
    """Eine tote Website ist KEIN "baut nicht in Spanien".

    Beides als `verdacht: False` zu fuehren, waere genau die Sorte stiller
    Fehler, die dieses Projekt zweimal Geld gekostet hat: ein Ausfall saehe
    aus wie ein Befund.
    """
    from adwatch.enrich import tiefenlauf, vorlauf

    monkeypatch.setattr(tiefenlauf, "_startseite", lambda d: None)
    r = vorlauf.eine_domain("tot.de")
    assert r["erreichbar"] is False
    assert r["verdacht"] is False and r["seiten"] == 0


def test_modellurteil_gibt_bei_jedem_fehler_einen_fehler_zurueck():
    """Nie ein leeres Ergebnis, immer ein `fehler`-Schluessel.

    Der Fall, der das erzwungen hat: 9.092 Aufrufe wurden mit HTTP 400
    abgelehnt, weil das Guthaben leer war. Haette beurteilen() dabei ein
    leeres Urteil geliefert, waere der Lauf als "0 Fehler, nichts gefunden"
    durchgegangen -- und 9.092 Seiten waeren still auf die Regel
    zurueckgefallen, die in der Stichprobe 2 von 14 richtig hatte.
    """
    from adwatch.enrich import spanienverdacht

    class Kaputt:
        class messages:
            @staticmethod
            def create(**k):
                raise ConnectionError("Verbindung weg")

    d = spanienverdacht.beurteilen("Haus", "https://x.de/h", "Text", client=Kaputt())
    assert "fehler" in d and "ConnectionError" in d["fehler"]
    assert d.get("in_spanien") is None, "kein stillschweigendes Nein"

    # Antwort ohne verwertbares JSON: ebenfalls ein Fehler, mit dem Rohtext
    # daneben, damit man sieht WAS das Modell gesagt hat.
    class Schwatzhaft:
        class messages:
            @staticmethod
            def create(**k):
                class A:
                    content = [type("B", (), {"type": "text",
                                              "text": "Klar doch, das liegt in Spanien!"})()]
                    usage = type("U", (), {"input_tokens": 10, "output_tokens": 9})()
                return A()

    d2 = spanienverdacht.beurteilen("Haus", "https://x.de/h", "Text", client=Schwatzhaft())
    assert d2["fehler"] == "kein JSON" and "Klar doch" in d2["roh"]


def test_modellurteil_liest_json_mit_code_zaun_und_rechnet_die_kosten():
    """Haiku packt seine Antwort gern in ```json ... ``` -- das muss weg,
    sonst scheitert jede einzelne Antwort am Parser.

    Die Kosten stehen mit im Ergebnis, weil ein Lauf ueber 10.212 Domains
    sonst erst auf der Rechnung sichtbar wird.
    """
    from adwatch.enrich import spanienverdacht

    class Ordentlich:
        class messages:
            @staticmethod
            def create(**k):
                class A:
                    content = [type("B", (), {"type": "text", "text":
                        '```json\n{"ist_projekt": true, "in_spanien": true, '
                        '"ort": "Palma", "baujahr": 2019, "sicherheit": "hoch"}\n```'})()]
                    usage = type("U", (), {"input_tokens": 2000, "output_tokens": 100})()
                return A()

    d = spanienverdacht.beurteilen("Villa", "https://x.de/v", "Text", client=Ordentlich())
    assert "fehler" not in d
    assert d["in_spanien"] is True and d["ort"] == "Palma" and d["baujahr"] == 2019
    # 2000/1e6*1.0 + 100/1e6*5.0
    assert d["kosten"] == pytest.approx(0.0025)
