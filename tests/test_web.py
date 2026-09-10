"""Oberflaeche und Betrieb: Routen, Auftraege, Identitaet, Sammel-Pipeline.

Aufgeteilt aus test_core.py: 197 Tests in einer Datei von 6.000 Zeilen
liessen sich nicht mehr ueberblicken. Fixtures stehen in conftest.py.
"""
import datetime as dt   # noqa: F401

import pytest   # noqa: F401

from hilfen import _write_markt



def test_is_actor_error_sentinel():
    from adwatch.collect.meta_source import _is_actor_error
    # the real record the Apify actor emits for a page with no matching ads
    assert _is_actor_error({"error": "Ads not found", "errorCode": "ADS_NOT_FOUND",
                            "url": "https://www.facebook.com/ads/library/?x"}) is True
    # genuine ads are never errors
    assert _is_actor_error({"ad_archive_id": "123", "is_active": True}) is False
    assert _is_actor_error({"id": "123", "snapshot": {}}) is False
    # an error-shaped record that still carries a real ad id is kept (defensive)
    assert _is_actor_error({"error": "partial", "ad_archive_id": "123"}) is False

def test_fetch_ads_drops_actor_error_stub():
    """A page with zero active ads returns the actor's ADS_NOT_FOUND sentinel.
    It must be filtered out so the page reads as 0 ads — not 1 phantom active
    ad with empty text/no id (the bug that gave 72/90/135 a false score)."""
    from adwatch.collect.meta_source import MetaAdSource
    src = object.__new__(MetaAdSource)     # skip __init__ (no token needed)
    src.backend = "apify"
    stub = {"error": "Ads not found", "errorCode": "ADS_NOT_FOUND", "url": "x"}
    real = {"ad_archive_id": "999", "is_active": True, "page_id": "111",
            "snapshot": {"body": {"text": "Neue Fenster"}}}
    # only the sentinel -> zero ads
    src._run_actor = lambda payload: [stub]
    assert src.fetch_ads("111", active_only=True) == []
    # sentinel mixed with a real ad -> only the real ad survives
    src._run_actor = lambda payload: [stub, real]
    out = src.fetch_ads("111", active_only=True)
    assert len(out) == 1 and out[0].external_ad_id == "999"

def test_shares_distinctive_token():
    from adwatch.identity.serper_source import _shares_distinctive_token as sh
    assert sh("Grantz GmbH & Co. KG", "Grantz Metallbau") is True         # real: shared surname
    assert sh("Albrecht GmbH", "Heideck - Waescheweiher") is False        # unrelated
    assert sh("SH-Fenstersysteme GmbH", "WS-Fenstersysteme") is False     # only a compound trade word
    assert sh("Pabst Metallbau GmbH", "Candidate Flow Jobs: Metallbau") is False  # only 'Metallbau'

def test_prefer_facebook_over_instagram():
    from adwatch.identity.serper_source import _prefer_facebook as pf
    ig = {"platform": "instagram", "name": "Grantz Metallbau", "similarity": 1.0}
    fb = {"platform": "facebook", "name": "Grantz GmbH & Co. KG", "page_id": None, "similarity": 1.0}
    # judge picked IG, a co-equal token-sharing FB exists -> switch to FB
    assert pf("Grantz GmbH & Co. KG", ig, [fb, ig]) is fb
    # an already-Facebook pick is never touched
    assert pf("Grantz GmbH & Co. KG", fb, [fb, ig]) is fb
    # FB candidate shares no distinctive token -> keep the IG pick
    bad = {"platform": "facebook", "name": "Heideck", "page_id": None, "similarity": 1.0}
    ig2 = {"platform": "instagram", "name": "Albrecht", "similarity": 1.0}
    assert pf("Albrecht GmbH", ig2, [bad, ig2]) is ig2
    # prefer the fetch-ready FB (numeric page_id) over a handle-only one
    fb_pid = {"platform": "facebook", "name": "Grantz Bau", "page_id": "123", "similarity": 1.0}
    assert pf("Grantz GmbH", ig, [fb, fb_pid, ig]).get("page_id") == "123"

def test_apify_quota_error_detection(monkeypatch):
    """A monthly usage / hard-limit 403 must raise ApifyQuotaError (batch-fatal),
    while an ordinary 4xx stays a plain RuntimeError (per-company error)."""
    import requests
    from adwatch.collect.meta_source import ApifyQuotaError, MetaAdSource
    src = object.__new__(MetaAdSource)
    src.backend, src.actor_id, src.token = "apify", "x", "y"

    class Resp:
        def __init__(self, code, text): self.status_code, self.text = code, text

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(403, "monthly usage hard limit exceeded"))
    with pytest.raises(ApifyQuotaError):
        src._run_actor({})

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(400, "invalid input schema"))
    with pytest.raises(RuntimeError) as ei:
        src._run_actor({})
    assert not isinstance(ei.value, ApifyQuotaError)

def test_google_source_has_backend():
    """run_once_google reads source.backend for its progress/summary — the Google
    source must define it (its absence failed every Google fetch with
    'GoogleAdSource object has no attribute backend')."""
    from adwatch.collect.google_source import GoogleAdSource
    assert GoogleAdSource.backend == "apify"

def test_failed_fetch_does_not_zero_metrics(temp_db):
    from adwatch.collect.pipeline import _store_metrics
    from adwatch.models import Company, WeeklyCompanyMetric
    from sqlalchemy import select
    s = temp_db.SessionLocal()
    c = Company(name="T Co", resolution_status="confirmed", country="DE")
    s.add(c); s.commit()
    week = dt.date(2026, 7, 6)
    # a good week: 5 active ads
    ad = type("A", (), {"category": "product_sale", "product": "Fenster", "is_active": True,
                        "start_date": week})
    _store_metrics(s, c, week, [{"raw": ad, "category": "product_sale", "product": "Fenster"}] * 5, "ok")
    s.commit()
    before = s.scalar(select(WeeklyCompanyMetric).where(WeeklyCompanyMetric.company_id == c.id))
    assert before.total_active_ads == 5
    # now a FAILED fetch for the same week must NOT overwrite it with 0
    _store_metrics(s, c, week, [], "error")
    s.commit()
    after = s.scalar(select(WeeklyCompanyMetric).where(WeeklyCompanyMetric.company_id == c.id))
    assert after.total_active_ads == 5, "failed fetch overwrote a good week with 0"
    s.close()

def test_fetch_job_source_routing(temp_db):
    """A fetch job routes sources per company: a Meta unit only where a page was
    found, a Google unit only where a website is set, and a company with NEITHER
    is dropped from the job entirely. The estimate + the job's `total` count only
    the units that will really run — never the raw company × source product."""
    from adwatch import jobs
    from adwatch.jobs import _google_fetchable_ids, _plan_units
    from adwatch.models import Company, CompanyPage
    s = temp_db.SessionLocal()

    def mk(name, website=None):
        c = Company(name=name, resolution_status="pending", country="DE", website_domain=website)
        s.add(c); s.flush()
        return c

    a = mk("A page+web", website="a.de")     # Meta page + website  -> both sources
    s.add(CompanyPage(company_id=a.id, source="meta", page_id="111", page_name="A", role="main", active=True))
    b = mk("B web only", website="b.de")     # website, no page     -> Google only
    c = mk("C page only")                    # page, no website     -> Meta only
    s.add(CompanyPage(company_id=c.id, source="meta", page_id="222", page_name="C", role="main", active=True))
    d = mk("D neither")                      # neither              -> no units at all
    s.commit()
    ids = [a.id, b.id, c.id, d.id]

    assert _google_fetchable_ids(s, ids) == {a.id, b.id}
    # deterministic order: company order, Meta before Google
    assert _plan_units(s, ids, ["meta", "google"]) == \
        [(a.id, "meta"), (a.id, "google"), (b.id, "google"), (c.id, "meta")]
    assert _plan_units(s, ids, ["meta"]) == [(a.id, "meta"), (c.id, "meta")]
    assert _plan_units(s, ids, ["google"]) == [(a.id, "google"), (b.id, "google")]
    s.close()

    est = jobs.estimate(ids, ["meta", "google"])
    assert est["total_units"] == 4              # not 4 companies × 2 sources = 8
    assert est["meta_fetchable"] == 2 and est["meta_skipped"] == 2
    assert est["google_fetchable"] == 2 and est["google_skipped"] == 2

    job = jobs.create_job(ids, ["meta", "google"], label="t")
    assert job["total"] == 4                     # job sized to the routed plan

    # a selection with no fetchable source at all is refused, not created empty
    with pytest.raises(ValueError):
        jobs.create_job([d.id], ["meta", "google"])

def test_consumers_are_excluded_from_every_count(temp_db):
    """Private Endkunden are 36% of the base and none of them will ever run an ad
    campaign, so including them made every ratio wrong ("14 of 4618"). They stay in
    the database but must not reach a single count, list or report — and no filter
    combination may put them back."""
    from sqlalchemy import select
    from adwatch import scope, services
    from adwatch.customers import _apply_filters
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    s.add_all([
        Company(name="Partner Handel", segment="Handel", country="DE"),
        Company(name="Partner Verarbeiter", segment="Verarbeiter", country="DE"),
        Company(name="Herr Müller", segment="Private Endkunden", country="DE"),
        Company(name="Frau Schmidt", segment="Private Endkunden", country="DE"),
        Company(name="Unklar", segment=None, country="DE"),   # unknown != consumer
    ])
    s.commit(); s.close()

    def names(f):
        with temp_db.SessionLocal() as s2:
            return {c.name for c in s2.scalars(_apply_filters(select(Company), f))}

    partners = {"Partner Handel", "Partner Verarbeiter", "Unklar"}
    assert names({}) == partners                       # no filter at all
    assert names({"country": ["DE"]}) == partners      # an unrelated filter
    # a hand-picked id list must not smuggle them back in
    with temp_db.SessionLocal() as s2:
        all_ids = [c.id for c in s2.scalars(select(Company))]
    assert names({"ids": all_ids}) == partners
    # nor may an explicit "don't exclude anything" style filter
    assert names({"exclude_segment": []}) == partners

    # the read model the dashboard KPIs are built from
    assert {c["name"] for c in services.list_companies()} == partners
    assert {m["company"] for m in services.latest_metrics()} == partners
    # even when consumer ids are passed in explicitly
    assert {m["company"] for m in services.latest_metrics(all_ids)} == partners

    # a NULL segment is kept — unknown is not the same as consumer
    assert scope.is_in_scope(None) and scope.is_in_scope("Handel")
    assert not scope.is_in_scope("Private Endkunden")

    # the deliberate ways in still work, so the data is not unreachable
    assert "Herr Müller" in names({"include_consumers": True})
    assert names({"segment": ["Private Endkunden"]}) == {"Herr Müller", "Frau Schmidt"}

def test_pipeline_runs_steps_in_the_working_order(temp_db, monkeypatch):
    """The pipeline must execute domains -> identity -> enrich -> ads -> report ->
    send, skip unchecked steps, and never send without a report. Execution has to
    follow the RESOLVED order, not the order the code happens to be written in."""
    from adwatch import jobs
    from adwatch.models import Company, FetchJob, ReportRecipient

    s = temp_db.SessionLocal()
    a = Company(name="Pipe Eins", country="DE"); b = Company(name="Pipe Zwei", country="DE")
    r = ReportRecipient(name="BD", email="bd@x.de", active=True)
    s.add_all([a, b, r]); s.commit()
    ids, rid = [a.id, b.id], r.id
    s.close()

    calls = []
    import adwatch.enrich.service as enrich_service
    import adwatch.identity.resolver as resolver
    import adwatch.collect.pipeline as coll
    import adwatch.report as report_mod
    import adwatch.emailer as emailer_mod

    monkeypatch.setattr(enrich_service, "derive_domain",
                        lambda cid, **k: calls.append(("domains", cid)) or {"status": "domain_found",
                                                                           "website": "x.de", "source": "email_domain",
                                                                           "validated_by": "phone"})
    monkeypatch.setattr(enrich_service, "enrich_company",
                        lambda cid, **k: calls.append(("enrich", cid)) or {"status": "enriched", "website": "x.de",
                                                                           "website_source": "email_domain",
                                                                           "validated_by": "phone", "fields_found": 3})
    monkeypatch.setattr(resolver, "run_identity_check",
                        lambda cid, **k: calls.append(("identity", cid)) or {"status": "confirmed", "page_name": "P"})
    monkeypatch.setattr(coll, "run_once", lambda company_id=None: calls.append(("meta", company_id)))
    monkeypatch.setattr(coll, "run_once_google", lambda company_id=None: calls.append(("google", company_id)))
    monkeypatch.setattr(jobs, "_plan_units", lambda s_, cids, srcs: [(cids[0], "meta")])
    monkeypatch.setattr(report_mod, "build_report",
                        lambda filters=None: calls.append(("report", None)) or "output/adwatch_report_KW31_2026.pdf")
    monkeypatch.setattr(report_mod, "write_report_meta", lambda *a, **k: None)
    monkeypatch.setattr(report_mod, "subject_for_filename", lambda f: "Bericht")
    monkeypatch.setattr(emailer_mod, "send_report_email",
                        lambda path, recipient=None, subject=None, **k: calls.append(
                            ("send", tuple(recipient))))

    plan = {"enrich": True, "identity": True, "ads": ["meta"], "report": "full", "send_to": [rid]}
    job = jobs.create_pipeline_job(ids, plan, label="t")
    # domains + identity + enrich per company, ads upper bound, report, send
    assert job["total"] == 2 + 2 + 2 + 2 + 1 + 1
    jobs._run_pipeline(job["id"])                     # run inline, no thread

    order = [c[0] for c in calls]
    # the free pass first, then identity, and only then the paid enrichment
    assert order.index("domains") < order.index("identity") < order.index("enrich")
    assert order.index("enrich") < order.index("meta") < order.index("report") < order.index("send")
    assert [c for c in calls if c[0] == "domains"] == [("domains", ids[0]), ("domains", ids[1])]
    assert ("send", ("bd@x.de",)) in calls

    s = temp_db.SessionLocal()
    j = s.get(FetchJob, job["id"])
    assert j.status == "done"
    txt = " ".join(e["text"] for e in (j.log or []))
    for marker in ("Schritt 1/6", "Schritt 2/6", "Schritt 3/6", "Schritt 4/6",
                   "Schritt 5/6", "Schritt 6/6", "Pipeline abgeschlossen"):
        assert marker in txt, marker
    # the log states the order it used, and that it was the default
    assert "Standard-Reihenfolge" in txt
    s.close()

    # an explicit order is obeyed instead of the default
    calls.clear()
    job3 = jobs.create_pipeline_job(ids, {"enrich": True, "identity": True,
                                          "order": ["enrich", "identity"]}, label="t3")
    jobs._run_pipeline(job3["id"])
    o3 = [c[0] for c in calls]
    assert "domains" not in o3
    assert o3.index("enrich") < o3.index("identity")

    # a plan with only some steps skips the rest; sending without a report is refused
    calls.clear()
    job2 = jobs.create_pipeline_job(ids, {"enrich": True}, label="t2")
    jobs._run_pipeline(job2["id"])
    assert {c[0] for c in calls} == {"enrich"}
    with pytest.raises(ValueError):
        jobs.create_pipeline_job(ids, {"send_to": [rid]})
    with pytest.raises(ValueError):
        jobs.create_pipeline_job(ids, {})

def test_recipient_tick_state_persists_and_is_not_active(temp_db):
    """Unticking a recipient must survive a reload, and must NOT disable the
    address — a saved weekly definition still has to reach them."""
    from adwatch import services
    from adwatch.models import ReportRecipient

    a = services.add_recipient("a@solarlux.com", "A")
    services.add_recipient("b@solarlux.com", "B")
    # everyone starts ticked, so behaviour is unchanged until the user acts
    assert all(r["preselected"] for r in services.list_recipients())

    services.set_recipient_preselected(a["id"], False)
    rows = {r["email"]: r for r in services.list_recipients()}
    assert rows["a@solarlux.com"]["preselected"] is False
    assert rows["b@solarlux.com"]["preselected"] is True
    # the crucial separation: unticked but still mailable
    assert rows["a@solarlux.com"]["active"] is True
    with temp_db.SessionLocal() as s:
        assert s.get(ReportRecipient, a["id"]).active is True

    # and it toggles back. NB the fixture also seeds the configured default
    # recipient via raw SQL — it must carry preselected=True too, which is why
    # the column needs a server default and not just an ORM-side one.
    services.set_recipient_preselected(a["id"], True)
    assert all(r["preselected"] for r in services.list_recipients())

    with pytest.raises(ValueError):
        services.set_recipient_preselected(9999, False)

def test_downgrade_resets_collected_ads(temp_db):
    """A recheck that DOWNGRADES an auto-confirmed page (page before, none now)
    must clear that page's collected ads/metric at the point the page is dropped
    — not leave a phantom active-ad count on a now-page-less company (the
    Andreas-Schimke bug)."""
    from adwatch.identity import resolver
    from adwatch.models import Ad, CollectionRun, Company, CompanyPage, WeeklyCompanyMetric
    from sqlalchemy import select
    s = temp_db.SessionLocal()
    c = Company(name="Downgrade Co", resolution_status="confirmed", country="DE",
                page_id="777", page_name="Wrong Page")
    s.add(c); s.flush()
    s.add(CompanyPage(company_id=c.id, source="meta", page_id="777", role="main", status="auto"))
    run = CollectionRun(company_id=c.id, source="meta", week_start=dt.date(2026, 7, 6),
                        page_id="777", status="ok", ads_scraped=1)
    s.add(run); s.flush()
    s.add(Ad(run_id=run.id, source="meta", external_ad_id="a1", is_active=True))
    s.add(WeeklyCompanyMetric(company_id=c.id, source="meta", week_start=dt.date(2026, 7, 6),
                             total_active_ads=1, score=10))
    s.commit(); cid = c.id

    c = s.get(Company, cid)
    resolver._apply_identity_result(
        s, c, {"status": "no_ads_found", "page_id": None, "page_name": None,
               "page_url": None, "candidates": []}, method="serper")
    s.commit()

    assert s.scalar(select(CollectionRun).where(CollectionRun.company_id == cid)) is None
    assert s.scalar(select(WeeklyCompanyMetric).where(WeeklyCompanyMetric.company_id == cid)) is None
    assert s.scalar(select(CompanyPage).where(CompanyPage.company_id == cid, CompanyPage.role == "main")) is None
    c = s.get(Company, cid)
    assert c.page_id is None and c.resolution_status == "no_ads_found"
    s.close()

def test_unlink_resets_collected_ads(temp_db):
    """Unlinking a wrong page must clear its collected ads/score — otherwise
    the wrong page's numbers linger on the company (the Bau-DL bug)."""
    from adwatch.identity import resolver
    from adwatch.models import Company, CompanyPage, CollectionRun, Ad, WeeklyCompanyMetric
    from sqlalchemy import select
    s = temp_db.SessionLocal()
    c = Company(name="Wrong Page Co", resolution_status="confirmed", country="DE",
                page_id="999", page_name="Wrong")
    s.add(c); s.flush()
    s.add(CompanyPage(company_id=c.id, source="meta", page_id="999", role="main", status="auto"))
    run = CollectionRun(company_id=c.id, source="meta", week_start=dt.date(2026, 7, 6),
                        page_id="999", status="ok", ads_scraped=42)
    s.add(run); s.flush()
    s.add(Ad(run_id=run.id, source="meta", external_ad_id="a1", is_active=True))
    s.add(WeeklyCompanyMetric(company_id=c.id, source="meta", week_start=dt.date(2026, 7, 6),
                             total_active_ads=42, score=85))
    s.commit(); cid = c.id
    s.close()

    resolver.unlink_main(cid)

    s = temp_db.SessionLocal()
    assert s.scalar(select(WeeklyCompanyMetric).where(WeeklyCompanyMetric.company_id == cid)) is None
    assert s.scalar(select(CollectionRun).where(CollectionRun.company_id == cid)) is None
    assert s.scalar(select(Ad).where(Ad.external_ad_id == "a1")) is None
    c = s.get(Company, cid)
    assert c.page_id is None and c.resolution_status in ("ambiguous", "pending")
    s.close()

def test_market_list_rows_are_distinguishable_and_not_monitored(temp_db, tmp_path):
    """A scraped list must never be mistakable for CRM master data, and must not
    silently enter the paid ad-fetch queue."""
    from sqlalchemy import select
    from adwatch import market_list as ml
    from adwatch.models import Company
    from adwatch import scope

    ml.import_list(_write_markt(tmp_path), lead_source="test_es")
    with temp_db.SessionLocal() as s:
        rows = s.scalars(select(Company).where(Company.lead_source == "test_es")).all()
        assert rows
        assert all(c.crm_id is None for c in rows)
        assert all(c.source == "marktanalyse" for c in rows)
        assert all(c.monitored is False for c in rows)
        # the competitor is present but excluded from every count
        in_scope = s.scalars(scope.apply(select(Company)).where(
            Company.lead_source == "test_es")).all()
        assert "Schueco Showroom Madrid" not in {c.name for c in in_scope}
        assert "Premial" in {c.name for c in in_scope}

def test_domain_in_name_is_extracted_but_socials_are_not():
    """'CBF (calviabalear.com)' states its own domain — that is the researcher
    telling us, not a guess. A LinkedIn URL in the notes is NOT a company site."""
    from adwatch.identity.find_website import domain_from_name
    assert domain_from_name("CBF (calviabalear.com)") == "calviabalear.com"
    assert domain_from_name("Aluminios Lago, S.L.") is None
    assert domain_from_name("Óscar RV Arquitecto linkedin.com/in/oscar") is None
    assert domain_from_name("Studio facebook.com/studio") is None

def test_only_locality_backed_matches_are_auto_accepted():
    """The gate here is deliberately STRICTER than enrichment's. domain_plus_name
    proves a name coincidence, not that this is the right company — 'Premial' or
    'Al-Andalus' would match a namesake in another province, and a wrong website
    silently produces a description and an ad history for the wrong firm."""
    from adwatch.identity.find_website import PROVEN
    assert "domain_plus_name" not in PROVEN
    for strong in ("phone", "plz_street", "plz_name", "domain_in_name"):
        assert strong in PROVEN

def test_unproven_candidate_is_queued_not_written(temp_db, monkeypatch):
    """A plausible-but-unproven candidate must leave website_domain EMPTY."""
    from sqlalchemy import select
    from adwatch.identity import find_website as fw
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    c = Company(name="Aluminios Ejemplo", country="ES", city="Valencia",
                postal_code="46020", street="Av. Catalunya 13",
                lead_source="t", segment="Verarbeiter")
    s.add(c); s.commit(); cid = c.id; s.close()

    monkeypatch.setattr(fw, "search_candidates",
                        lambda *a, **k: [{"domain": "aluminios-ejemplo.com",
                                          "title": "Aluminios Ejemplo"}])
    # a page that confirms the NAME but carries neither the postcode nor the street
    monkeypatch.setattr(fw, "page_bundle",
                        lambda d, **k: {"text": "Aluminios Ejemplo — ventanas",
                                        "pages": [f"https://{d}"]})
    r = fw.find_for(cid)
    assert r["status"] == fw.NEEDS_REVIEW
    with temp_db.SessionLocal() as s:
        got = s.get(Company, cid)
        assert got.website_domain is None, "an unproven domain must not be stored"
        assert got.identity_status == fw.NEEDS_REVIEW
        assert got.identity_evidence["review_candidate"] == "aluminios-ejemplo.com"
    # ...and it is surfaced for a human instead of being dropped
    assert any(q["company_id"] == cid for q in fw.review_queue(lead_source="t"))

def test_searched_companies_are_not_paid_for_twice(temp_db, monkeypatch):
    """not_found and needs_review both count as done, or a re-run bills Serper
    again for the same company."""
    from adwatch.identity import find_website as fw
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    for i, st in enumerate([fw.NOT_FOUND, fw.NEEDS_REVIEW, None]):
        s.add(Company(name=f"Firma {i}", country="ES", lead_source="t",
                      segment="Verarbeiter", identity_status=st))
    s.commit(); s.close()
    pend = fw.pending_ids("t")
    assert len(pend) == 1, "only the never-searched company may be queued"

def test_conflict_domains_are_never_google_fetched(temp_db):
    """A domain that FAILED identity verification must not be used to attribute a
    Google ad history — 188 of the 430 Spanish market-list sites came back
    'conflict', and fetching through them files someone else's ads under the
    company. Unverified (never checked) stays fetchable; disproven does not."""
    from adwatch.jobs import _google_fetchable_ids
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    ok = Company(name="Ok Co", country="ES", website_domain="ok.es",
                 identity_status="verified")
    unknown = Company(name="Unknown Co", country="ES", website_domain="unknown.es")
    bad = Company(name="Bad Co", country="ES", website_domain="portal.es",
                  identity_status="conflict")
    s.add_all([ok, unknown, bad]); s.commit()
    ids = [ok.id, unknown.id, bad.id]
    fetchable = _google_fetchable_ids(s, ids)
    assert ok.id in fetchable and unknown.id in fetchable
    assert bad.id not in fetchable
    s.close()

def test_triage_routes_but_never_writes_verified(temp_db, monkeypatch):
    """The design rule from the migration incident: only the deterministic gate
    may write 'verified'. Triage clears a diagnosed wrong_site domain (kept in
    evidence), queues likely_right for review with the clue, leaves too_thin as
    conflict — and no path produces 'verified'."""
    from adwatch.identity import triage
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    wrong = Company(name="Dealer A", country="ES", website_domain="technal.com",
                    identity_status="conflict", lead_source="t", segment="Handel")
    right = Company(name="Dealer B", country="ES", website_domain="dealerb.es",
                    identity_status="conflict", lead_source="t", segment="Handel")
    thin = Company(name="Dealer C", country="ES", website_domain="thin.es",
                   identity_status="conflict", lead_source="t", segment="Handel")
    s.add_all([wrong, right, thin]); s.commit()
    ids = {wrong.id: "wrong_site", right.id: "likely_right", thin.id: "too_thin"}
    s.close()

    monkeypatch.setattr(triage, "_evidence_for",
                        lambda c: {"reachable": True, "excerpt": "x" * 100})
    # confident on purpose: this test covers ROUTING, not the confidence gate
    monkeypatch.setattr(triage, "_judge_batch", lambda blocks: {
        cid: {"verdict": v, "confidence": 0.9, "what": "w", "clue": "c"}
        for cid, v in ids.items()})
    monkeypatch.setattr(triage.config, "ANTHROPIC_API_KEY", "test", raising=False)

    r = triage.run(lead_source="t")
    assert r["wrong_site"] == 1 and r["likely_right"] == 1 and r["too_thin"] == 1

    with temp_db.SessionLocal() as s:
        w, ri, th = (s.get(Company, cid) for cid in ids)
        assert w.website_domain is None                    # cleared for the finder
        assert w.identity_evidence["triage"]["domain_at_triage"] == "technal.com"
        assert ri.identity_status == "needs_review"
        assert ri.website_domain == "dealerb.es"           # kept, human decides
        assert th.identity_status == "conflict"
        for c in (w, ri, th):
            assert c.identity_status != "verified"

def test_triage_brand_overlap_is_deterministic_evidence():
    """If the researcher wrote 'Schüco + Drutex' and the site says Schüco, that is
    hard evidence independent of the LLM — a namesake in another province does not
    happen to carry the same profile systems."""
    from adwatch.identity.triage import _brand_overlap
    notes = "Schueco + Drutex, eigener Ausstellungsraum"
    # the researcher wrote 'Schueco', the Spanish site writes 'Schüco' — a naive
    # substring match found NO overlap and silently threw the evidence away
    assert _brand_overlap(notes, "Somos distribuidor Schüco oficial") == ["Schüco"]
    assert _brand_overlap(notes, "Schuco y Drutex") == ["Drutex", "Schüco"]
    assert _brand_overlap(notes, "ventanas de PVC baratas") == []
    assert _brand_overlap(None, "Schüco") == []

def test_triage_downgrades_unconfident_guesses(temp_db, monkeypatch):
    """A four-way label choice always returns something. Without a quotable clue
    or a brand match, low confidence must not reach the human queue."""
    from adwatch.identity import triage
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    c = Company(name="Vago SL", country="ES", website_domain="vago.es",
                identity_status="conflict", lead_source="t", segment="Handel")
    s.add(c); s.commit(); cid = c.id; s.close()

    monkeypatch.setattr(triage.config, "ANTHROPIC_API_KEY", "test", raising=False)
    monkeypatch.setattr(triage, "_evidence_for",
                        lambda x: {"reachable": True, "excerpt": "y" * 80,
                                   "brand_overlap": []})
    monkeypatch.setattr(triage, "_judge_batch", lambda blocks: {
        cid: {"verdict": "likely_right", "confidence": 0.3, "what": "", "clue": ""}})
    r = triage.run(lead_source="t")
    assert r["too_thin"] == 1 and r["likely_right"] == 0
    with temp_db.SessionLocal() as s:
        assert s.get(Company, cid).identity_status == "conflict"

def test_dashboard_only_loads_companies_with_an_ad_footprint(temp_db):
    """After the CRM import put 46k accounts in the database, /api/state built a
    metric row for every one of them — two queries each — and shipped 33 MB of
    JSON on every page load, 18 seconds of it inside latest_metrics(). Only 731
    companies had any ad data, and the dashboard filters on has_data everywhere
    anyway, so the rest were pure payload."""
    import datetime as dt
    from adwatch import services
    from adwatch.models import Company, CompanyPage, WeeklyCompanyMetric

    s = temp_db.SessionLocal()
    tracked = Company(name="Hat Metrik", segment="Handel")
    paged = Company(name="Hat Seite", segment="Handel")
    quiet = Company(name="Nie geholt", segment="Handel")
    s.add_all([tracked, paged, quiet]); s.commit()
    s.add(WeeklyCompanyMetric(company_id=tracked.id, week_start=dt.date(2026, 8, 3),
                              source="meta", total_active_ads=3))
    s.add(CompanyPage(company_id=paged.id, page_id="p1", active=True))
    # an INACTIVE page is not a footprint — it is a page we stopped following
    s.add(CompanyPage(company_id=quiet.id, page_id="p2", active=False))
    s.commit(); s.close()

    ids = services.tracked_company_ids()
    assert set(ids) == {tracked.id, paged.id}
    assert quiet.id not in ids

def test_review_queue_filters_by_any_market(temp_db, monkeypatch):
    """Two bugs in one screen. The route used SessionLocal without importing it,
    so BOTH the queue and the reject button raised NameError — the Prüfen tab
    was dead for every user, and no test touched the endpoint. And the only
    filter was a hard-coded "Nur Spanien-Marktanalyse" checkbox, which makes
    every other market unreachable the moment one exists."""
    from fastapi.testclient import TestClient
    from adwatch import web
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    s.add_all([
        Company(name="ES Eins", country="ES", segment="Verarbeiter",
                lead_source="marktanalyse_es_2026_08", identity_status="needs_review"),
        Company(name="DE Eins", country="DE", segment="Handel",
                lead_source=None, identity_status="needs_review"),
        Company(name="FR Eins", country="FR", segment="Verarbeiter",
                lead_source="marktanalyse_fr_2026", identity_status="needs_review"),
        Company(name="Entschieden", country="ES", segment="Verarbeiter",
                identity_status="verified"),
    ])
    s.commit(); s.close()
    monkeypatch.setattr(web, "SessionLocal", temp_db.SessionLocal)

    c = TestClient(web.app)
    r = c.get("/api/identity/review")
    assert r.status_code == 200, r.text          # the NameError regression
    body = r.json()
    assert {x["name"] for x in body["rows"]} == {"ES Eins", "DE Eins", "FR Eins"}
    # facets describe what is really in the queue, so the UI needs no hard-coded market
    assert body["facets"]["country"] == ["DE", "ES", "FR"]
    assert "marktanalyse_fr_2026" in body["facets"]["lead_source"]

    # any market is selectable, not just Spain
    assert {x["name"] for x in c.get("/api/identity/review?country=FR").json()["rows"]} == {"FR Eins"}
    assert {x["name"] for x in c.get("/api/identity/review?country=DE&country=ES").json()["rows"]} \
        == {"ES Eins", "DE Eins"}
    assert {x["name"] for x in c.get("/api/identity/review?segment=Handel").json()["rows"]} == {"DE Eins"}
    # narrowing the list must not shrink the choices still on offer
    assert c.get("/api/identity/review?country=FR").json()["facets"]["country"] == ["DE", "ES", "FR"]

def test_audit_names_the_columns_that_are_really_outcomes(temp_db):
    """order_value / invoiced_value / sap_order_numbers are filled on ~92% of won
    deals and ~0% of lost ones. They sit in the same table as the legitimate
    features, so the audit has to name them rather than leave a reader to assume
    somebody checked."""
    from adwatch import audit
    from adwatch.models import CrmOpportunity

    s = temp_db.SessionLocal()
    for i in range(40):
        won = i < 20
        s.add(CrmOpportunity(crm_id=f"o{i}",
                             state="gewonnen" if won else "verloren",
                             order_value=1000.0 if won else None,
                             estimated_value=1000.0,
                             lost_reason=None if won else "Zu teuer"))
    s.commit()
    s.close()

    rep = audit.outcome_leakage()
    assert "order_value" in rep["unusable"]
    assert "estimated_value" not in rep["unusable"], "gleich gefuellt - also brauchbar"

def test_a_shared_token_is_not_proof_unless_the_candidate_is_ours():
    """`domain_plus_name` means only that the domain shares a word with the
    company name. identity/find_website.PROVEN deliberately routes that to a
    human and ONBOARDING promises the same three hard signals — but
    enrich/service listed it as proof and wrote it straight into master data.

    Measured on the first 20 Spanish companies: "Montajes Portico Balear SL" ->
    portsdebalears.com and "+ PLUS" -> pressingplus.com, both wrong, both then
    enriched with a stranger's facts.

    The origin is the other half of the question: the company's own e-mail on
    that domain is corroboration a search result does not have."""
    from adwatch.enrich.service import _accepts

    assert not _accepts("serper", "domain_plus_name"), "eine geteilte Silbe ist kein Beweis"
    assert _accepts("email_domain", "domain_plus_name"), "die eigene Mail-Domain schon"
    assert _accepts("sap", "domain_plus_name")
    for hard in ("phone", "plz_street", "plz_name", "domain_in_name"):
        assert _accepts("serper", hard), f"{hard} ist ein harter Beweis"
    assert not _accepts("serper", None)
    assert not _accepts("email_domain", None), "ohne jedes Signal zaehlt auch die Herkunft nicht"

def test_a_queued_company_always_has_something_to_decide(temp_db):
    """Two rules had drifted apart: _review_worthy put a company in the queue
    when its name appeared on the page, while the candidate was picked only from
    entries carrying a match signal. Seven of nine Spanish review items therefore
    reached the Pruefen tab with an empty Kandidat column — a decision with
    nothing to decide. One predicate now does both jobs."""
    from adwatch.enrich import service

    tried = [
        {"domain": "zufall.example", "origin": "serper", "signals": {}},
        {"domain": "treffer.example", "origin": "serper",
         "signals": {"name_in_domain": True}},
    ]
    worthy = [t for t in tried if service._review_worthy(t)]
    assert [t["domain"] for t in worthy] == ["treffer.example"], \
        "nur der Kandidat mit Signal ist eine Entscheidung wert"

    # a candidate that matched nothing must not put the company in the queue
    assert not any(service._review_worthy(t) for t in
                   [{"domain": "zeitung.example", "origin": "serper", "signals": {}}])

def test_searched_and_found_nothing_is_not_the_same_as_never_looked(temp_db):
    """Measured on job 57 at 661/1103: 248 Spanish companies came back
    'no_website_found' with a full candidate trail — searched properly, nothing
    provable — and every one kept identity_status NULL, which is exactly what the
    column says for a company nobody has touched.

    That is not cosmetic. find_website.pending_ids treats NULL as pending and
    NOT_FOUND as "do not re-spend", so the next search run would have paid Serper
    a second time for 248 answers already on record — the same mistake as the
    same-week ad re-fetch, on a different invoice.

    The verdict may only be written when a search REALLY ran: an enrichment pass
    with allow_search=False knows nothing about the wider web and must leave the
    question open rather than close it wrongly."""
    from adwatch.enrich import service

    searched = {"domain": None, "source": None, "validated_by": None,
                "review_candidate": None, "searched": True,
                "candidates": [{"domain": "fremd.example", "origin": "serper",
                                "validated": False, "signals": {}}],
                "bundle": None, "status": "no_website_found"}
    assert searched["searched"] is True

    # the resolver reports the distinction itself, from its own argument
    import adwatch.enrich.service as svc
    comp = {"name": "Sin Web SL", "website_domain": None, "email": None,
            "city": "Madrid", "country": "ES"}
    no_search = svc._resolve_website(comp, allow_search=False)
    assert no_search["status"] == "no_website_found"
    assert no_search["searched"] is False, \
        "ohne Suche darf nichts als 'nicht gefunden' abgeschlossen werden"

    # and the status the finder writes is the one that stops the re-spend
    from adwatch.identity.find_website import NOT_FOUND
    assert NOT_FOUND == "not_found"

def test_health_reports_the_three_lifelines(temp_db):
    """Für den Betrieb als Dienst: EIN HTTP-Blick muss sagen, ob die App lebt.
    Der Endpunkt prüft die drei Lebensadern (DB, Sicherung, CRM-Sync-Alter) und
    antwortet mit dem STATUSCODE, nicht nur im JSON — ein stumpfer Uptime-Check
    ohne Parser muss alarmieren können. Ohne einzige Sicherung ist der Zustand
    'degraded' (503): eine Datenbank, deren Wert aus bezahlten Abrufen und
    menschlichen Urteilen besteht, läuft nie gesund ungesichert."""
    from fastapi.testclient import TestClient
    from adwatch import backup, web

    client = TestClient(web.app)
    r = client.get("/health")
    body = r.json()
    assert body["db"] == "ok"
    assert "job_running" in body
    if body["backup_last"] is None:
        assert r.status_code == 503 and body["status"] == "degraded", \
            "ohne Sicherung darf /health nicht 'ok' sagen"
    else:
        assert r.status_code in (200, 503)

    # nach einer Sicherung ist der Backup-Teil gesund
    backup.backup_now(tag="healthtest")
    r2 = client.get("/health")
    b2 = r2.json()
    if b2["backup_last"]:
        assert b2["backup_age_hours"] < 1

def test_pipeline_board_counts_the_chain_honestly(temp_db):
    """Das Board zeigt die Kette, die die App ohnehin erzwingt — und es muss
    dieselben Regeln sprechen wie der Rest des Codes: Private Endkunden sind
    nie Teil eines Zählers (scope), 'not_found' ist ein Endstand und keine
    Lücke, Käufer zählen in der Qualifizierung nicht als Ziele, und unter
    MIN_WINNERS_USABLE Material-Käufern heißt der Modus 'scorecard'."""
    import datetime as dt
    from adwatch.insights import pipeline
    from adwatch.models import Company, CompanyEnrichment, CrmOrderEvent

    s = temp_db.SessionLocal()
    # 1: verifiziert + Fakten + Material-Käufer (2.500 €)
    s.add(Company(name="Kaeufer SL", country="ES", segment="Handel",
                  website_domain="kaeufer.example", identity_status="verified",
                  solarlux_fit="hoch"))
    # 2: Interessent mit Passung hoch — der eigentliche Zieltyp
    s.add(Company(name="Ziel SL", country="ES", segment="Verarbeiter",
                  identity_status="needs_review", solarlux_fit="hoch"))
    # 3: gesucht, nichts gefunden — Endstand, keine Lücke
    s.add(Company(name="Ohne Web SL", country="ES", segment="Handel",
                  identity_status="not_found"))
    # 4: Privatkunde — darf in KEINEM Zähler auftauchen
    s.add(Company(name="Privat", country="ES", segment="Private Endkunden",
                  identity_status="verified", solarlux_fit="hoch"))
    s.commit()
    ids = {c.name: c.id for c in s.query(Company).all()}
    s.add(CompanyEnrichment(company_id=ids["Kaeufer SL"], status="enriched",
                            fields={"description_de": "x"}))
    s.add(CrmOrderEvent(company_id=ids["Kaeufer SL"],
                        order_date=dt.date(2025, 3, 1), amount=2500.0))
    s.commit()

    st = pipeline.market_status(s, "ES")
    s.close()

    assert st["bestand"]["total"] == 3, "Privatkunden zaehlen nirgends mit"
    assert st["identitaet"]["verified"] == 1
    assert st["identitaet"]["offen"] == 1
    assert st["identitaet"]["not_found"] == 1
    assert st["anreicherung"]["mit_fakten"] == 1
    assert st["anreicherung"]["ohne_website_final"] == 1, \
        "not_found ist Endstand, keine Luecke"
    # der Käufer hat Passung hoch, zählt aber NICHT als Ziel — er ist Referenz
    assert st["qualifizierung"]["betriebe_hoch"] == 1, \
        "nur der Interessent, nicht der Kaeufer"
    assert st["bestand"]["kaeufer"] == 1
    assert st["icp"]["material_kaeufer"] == 1
    assert st["icp"]["modus"] == "scorecard", "1 Kaeufer liegt unter dem Boden"

    # und das Board waehlt bei unbekanntem Land den groessten Markt
    b = pipeline.board("XX")
    assert b["selected"] == "ES"

def test_a_company_is_not_fetched_twice_in_one_week(temp_db):
    """Measured 2026-08-11 over every ad fetch on record: 614 runs for only 300
    distinct company+source pairs, so 51% of all Apify spend bought a row that
    had already been bought. One pair was fetched ten times. The worst of it was
    a single morning of seven overlapping Spanish runs ("Status-Reparatur",
    "Lock-Retry", "Lauf 3", "Lauf 4"), each restarting from the top.

    It stayed invisible because the DATA never duplicated: WeeklyCompanyMetric is
    keyed on (company, source, week_start), so a re-fetch overwrote the row.
    Only the invoice grew.

    The ad week is the unit of freshness, and a successful run inside it means
    paid-for. A FAILED run does not count — that one deserves a retry."""
    import datetime as dt
    from adwatch import jobs
    from adwatch.collect.pipeline import monday_of
    from adwatch.models import CollectionRun, Company, CompanyPage
    from sqlalchemy import select

    week = monday_of(dt.date.today())
    s = temp_db.SessionLocal()
    for i in range(4):
        s.add(Company(name=f"Haendler {i}", website_domain=f"h{i}.example"))
    s.commit()
    ids = list(s.scalars(select(Company.id).order_by(Company.id)))
    # all four have a Meta page, so all four are meta-fetchable
    for cid in ids:
        s.add(CompanyPage(company_id=cid, source="meta", page_id=f"p{cid}", active=True))
    # #0 fetched fine this week, #1 came back empty (also a real answer),
    # #2 errored, #3 was fetched but LAST week
    s.add(CollectionRun(company_id=ids[0], source="meta", week_start=week, status="ok"))
    s.add(CollectionRun(company_id=ids[1], source="meta", week_start=week,
                        status="no_active_ads"))
    s.add(CollectionRun(company_id=ids[2], source="meta", week_start=week, status="error"))
    s.add(CollectionRun(company_id=ids[3], source="meta",
                        week_start=week - dt.timedelta(days=7), status="ok"))
    s.commit()
    s.close()

    s = temp_db.SessionLocal()
    fresh = jobs._fetched_this_week(s, ids)
    assert (ids[0], "meta") in fresh, "erfolgreich abgerufen = bezahlt"
    assert (ids[1], "meta") in fresh, "'keine Anzeigen' ist auch eine Antwort"
    assert (ids[2], "meta") not in fresh, "ein Fehlversuch darf erneut laufen"
    assert (ids[3], "meta") not in fresh, "letzte Woche ist nicht diese Woche"

    units = jobs._plan_units(s, ids, ["meta"])
    assert sorted(u[0] for u in units) == sorted([ids[2], ids[3]])

    # the override still exists for a deliberate same-week refresh
    forced = jobs._plan_units(s, ids, ["meta"], refetch=True)
    assert len(forced) == 4
    s.close()

    # and the pre-flight number explains the difference instead of just shrinking
    est = jobs.estimate(ids, ["meta"])
    assert est["total_units"] == 2
    assert est["fresh_skipped"] == 2
    assert jobs.estimate(ids, ["meta"], refetch=True)["total_units"] == 4

def test_all_fetched_this_week_says_so_instead_of_nothing_to_fetch(temp_db):
    """"Nothing to fetch" has two very different causes: nobody is fetchable, or
    everybody was already bought this week. The second is good news and needs to
    read as such, or the next person just forces a refetch to make the error go
    away."""
    import datetime as dt
    import pytest
    from adwatch import jobs
    from adwatch.collect.pipeline import monday_of
    from adwatch.models import CollectionRun, Company, CompanyPage
    from sqlalchemy import select

    week = monday_of(dt.date.today())
    s = temp_db.SessionLocal()
    s.add(Company(name="Haendler", website_domain="h.example"))
    s.commit()
    cid = s.scalar(select(Company.id))
    s.add(CompanyPage(company_id=cid, source="meta", page_id="p1", active=True))
    s.add(CollectionRun(company_id=cid, source="meta", week_start=week, status="ok"))
    s.commit()
    s.close()

    with pytest.raises(ValueError) as e:
        jobs.create_job([cid], ["meta"])
    assert "diese Woche schon abgerufen" in str(e.value)

    # forcing it works and creates a real job
    job = jobs.create_job([cid], ["meta"], refetch=True)
    assert job["total"] == 1

def test_a_resumed_fetch_job_continues_at_the_right_company(temp_db):
    """The freshness guard turned _plan_units from a pure function of the job's
    stored inputs into one that also depends on what the job ITSELF has fetched.
    _run_body rebuilt the unit list from those inputs on every start, so a
    resumed job would rebuild a SHORTER list while `completed` still counted
    positions in the original — and the cursor would skip past companies that
    were never fetched at all.

    Four companies, two fetched, then interrupted: the resumed job must run
    exactly the other two, not positions 3 and 4 of a list that lost its head."""
    import datetime as dt
    from adwatch import jobs
    from adwatch.collect.pipeline import monday_of
    from adwatch.models import CollectionRun, Company, CompanyPage, FetchJob
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    for i in range(4):
        s.add(Company(name=f"Haendler {i}", website_domain=f"h{i}.example"))
    s.commit()
    ids = list(s.scalars(select(Company.id).order_by(Company.id)))
    for cid in ids:
        s.add(CompanyPage(company_id=cid, source="meta", page_id=f"p{cid}", active=True))
    s.commit()
    s.close()

    job = jobs.create_job(ids, ["meta"], label="resume test")
    assert job["total"] == 4
    stored = [tuple(u) for u in job["plan"]["units"]]
    assert stored == [(cid, "meta") for cid in ids], "der Plan ist der Vertrag"

    # simulate: the first two ran, then the app died
    week = monday_of(dt.date.today())
    s = temp_db.SessionLocal()
    for cid in ids[:2]:
        s.add(CollectionRun(company_id=cid, source="meta", week_start=week, status="ok"))
    row = s.get(FetchJob, job["id"])
    row.completed = 2
    row.status = "interrupted"
    s.commit()
    s.close()

    # the plan must NOT shrink now that two of its companies count as fresh
    s = temp_db.SessionLocal()
    replanned = jobs._plan_units(s, ids, ["meta"])
    s.close()
    assert len(replanned) == 2, "ohne Plan wuerde neu geplant nur noch 2 Einheiten ergeben"

    again = jobs.get_job(job["id"])
    units = [tuple(u) for u in again["plan"]["units"]]
    assert units == stored, "der gespeicherte Plan bleibt unveraendert"
    # cursor 2 into the ORIGINAL plan -> the remaining two are companies 3 and 4
    assert units[again["completed"]:] == [(ids[2], "meta"), (ids[3], "meta")]

def test_kundenklasse_schliesst_nichts_aus(temp_db):
    """sl_customer_class darf NIEMANDEN aus der Auswertung werfen.

    Am 2026-08-20 wurde "07 - SL Mitarbeiter" als "Konto einer Solarlux-Person"
    gelesen und ausgeschlossen. Der volle Abruf widerlegte das: 8.204 Konten
    tragen die Klasse, 7.302 davon im Haendler-Panel, zusammen 104,6 Mio EUR
    Angebotsvolumen -- darunter MADEROS Wintergaerten, LEEB Balkone, Willab
    Garden AB. Das sind Haendler. Die Klasse bedeutet die BETREUUNGSART, nicht
    die Person.

    Der Ausschluss haette fast die halbe Grundgesamtheit geloescht (6.207 ->
    1.858 allein im Kalt-Profil), inklusive der groessten Konten. Dieser Test
    haelt die Korrektur fest, damit die naheliegende Fehllesung nicht
    zurueckkehrt."""
    from sqlalchemy import select
    from adwatch import scope
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    s.add_all([
        Company(name="MADEROS Wintergaerten", segment="Verarbeiter", country="DE",
                sl_customer_class="07 - SL Mitarbeiter"),
        Company(name="Fachhandel", segment="Handel", country="DE",
                sl_customer_class="02 - Fachhandelsvertrieb"),
        Company(name="Privatperson", segment="Private Endkunden", country="DE"),
    ])
    s.commit()

    drin = {n for (n,) in s.execute(
        select(Company.name).where(scope.in_scope_clause()))}
    assert "MADEROS Wintergaerten" in drin, "Betreuungsart ist kein Ausschlussgrund"
    assert "Fachhandel" in drin
    assert "Privatperson" not in drin, "Private Endkunden bleiben ausgeschlossen"
    s.close()

def test_personensuche_ohne_flow_bricht_nichts():
    """Die Empfaengerpflege darf NIE an einer Zusatzfunktion haengen.

    Ist der Personen-Flow nicht eingerichtet (der Normalfall bei jeder frischen
    Installation), muss die Suche eine leere Liste liefern statt zu werfen --
    das Feld faellt dann auf freie Eingabe zurueck."""
    from adwatch import people
    assert people.suchen("Mueller") == []
    assert people.suchen("") == []
    assert people.suchen("a") == [], "unter zwei Zeichen wird gar nicht gefragt"

def test_personensuche_versteht_beide_antwortformen():
    """Der Flow liefert je nach Aufbau eine nackte Liste oder {value: [...]},
    und die Feldnamen unterscheiden sich je nach Connector-Version
    (mail vs. userPrincipalName, displayName vs. DisplayName). Wer eine Form
    voraussetzt, bekommt beim anderen Aufbau still null Zeilen -- derselbe
    Fehler, der beim Lead-Abruf schon einmal zuschlug."""
    from adwatch import people

    assert people._rows([{"mail": "a@b.de"}]) == [{"mail": "a@b.de"}]
    assert people._rows({"value": [{"mail": "a@b.de"}]}) == [{"mail": "a@b.de"}]
    assert people._rows({}) == []
    assert people._rows(None) == []

    # beide Schreibweisen ergeben denselben Datensatz
    a = people._norm({"displayName": "Iheb Marouani", "mail": "i.m@solarlux.com",
                      "jobTitle": "BD", "department": "Strategie"})
    b = people._norm({"DisplayName": "Iheb Marouani", "UserPrincipalName": "i.m@solarlux.com",
                      "JobTitle": "BD", "Department": "Strategie"})
    assert a == b
    assert a["email"] == "i.m@solarlux.com" and a["name"] == "Iheb Marouani"

    # ohne brauchbare Adresse ist eine Zeile als Empfaenger wertlos
    assert people._norm({"displayName": "Ohne Mail"}) is None
    assert people._norm({"displayName": "Kaputt", "mail": "keine-adresse"}) is None

def test_teams_link_nur_bei_echter_adresse():
    """Teams laesst sich nicht einbetten, aber ein Deep Link tut es auch --
    ohne jede Berechtigung. Nur muss die Adresse eine sein."""
    from adwatch import people
    link = people.teams_link("i.marouani@solarlux.com")
    assert link and link.startswith("https://teams.microsoft.com/l/chat/0/0?users=")
    assert "i.marouani%40solarlux.com" in link, "Adresse muss kodiert sein"
    assert people.teams_link("kein-at-zeichen") is None
    assert people.teams_link("") is None
    assert people.teams_link(None) is None

def test_gzip_nur_fuer_fremde_klienten():
    """gzip lohnt ueber eine Leitung und schadet auf dem eigenen Rechner:
    gemessen 1,38 s ohne, 2,11 s mit -- 6.205 KB gegen 1.699 KB. AdWatch
    bindet 127.0.0.1, also ist der Normalfall der, in dem gzip kostet."""
    from fastapi.testclient import TestClient

    from adwatch.web import _LOKAL, app

    # TestClient meldet sich als 'testclient', gilt also als fremd
    r = TestClient(app).get("/health", headers={"Accept-Encoding": "gzip"})
    assert r.status_code in (200, 503)

    for lokal in ("127.0.0.1", "::1", "localhost"):
        assert lokal in _LOKAL, f"{lokal} muss als lokal gelten"

def test_sse_wird_nie_komprimiert():
    """Ein Kompressor sammelt Bytes, bis es sich lohnt. Genau das darf ueber
    dem Fortschritts-Stream eines Imports nicht passieren."""
    from adwatch.web import _GzipNurFuerFremde

    gesehen = []

    async def roh(scope, receive, send):
        gesehen.append("roh")

    m = _GzipNurFuerFremde(roh)
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        m({"type": "http", "path": "/api/fetch/stream/abc",
           "client": ("10.0.0.5", 1234)}, None, None))
    assert gesehen == ["roh"], "SSE muss unkomprimiert durchgereicht werden"
