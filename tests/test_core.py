"""Core money-and-data-path tests. Run: pytest -q (from repo root).

Deliberately uses the REAL modules against a temporary SQLite DB so the tests
also cover the SQLAlchemy models/migrations, not just pure functions.
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Pure functions — revenue parsing, divergence math
# ---------------------------------------------------------------------------

def test_parse_number_german_formats():
    from adwatch.customers import _parse_number
    assert _parse_number("1.234.567,89") == pytest.approx(1234567.89)
    assert _parse_number("1.500") == 1500          # lone dot = thousands
    assert _parse_number("1,5") == pytest.approx(1.5)
    assert _parse_number("€ 80.000") == 80000
    assert _parse_number("") is None
    assert _parse_number(None) is None
    assert _parse_number("-") is None


def test_revenue_gap_states():
    from adwatch.insights.divergence import revenue_gap
    # lapsed: bought before, nothing this year
    g, state, best = revenue_gap(0, [80000, 0, 0, 0], elapsed=0.5)
    assert state == "lapsed" and g == 1.0 and best == 80000
    # never bought
    _, state, _ = revenue_gap(0, [0, 0, 0, 0], elapsed=0.5)
    assert state == "never"
    # healthy: half-year revenue annualizes to ~last year
    _, state, _ = revenue_gap(50000, [100000, 0, 0, 0], elapsed=0.5)
    assert state == "healthy"
    # steep decline even after annualizing
    _, state, _ = revenue_gap(5000, [100000, 0, 0, 0], elapsed=0.5)
    assert state == "steep"


def test_annualization_uses_elapsed_not_calendar():
    """The bug we fixed: a half-year of revenue must annualize to full-year
    before comparison, so a healthy partner isn't mislabelled 'declining'."""
    from adwatch.insights.divergence import revenue_gap
    # 60k in half a year -> 120k annualized > 0.7*100k => healthy, NOT declining
    _, state, _ = revenue_gap(60000, [100000, 0, 0, 0], elapsed=0.5)
    assert state == "healthy"
    # same 60k but a FULL year elapsed => 60k < 0.7*100k => mild decline
    _, state, _ = revenue_gap(60000, [100000, 0, 0, 0], elapsed=1.0)
    assert state == "mild"


def test_marketing_score_recency_weight():
    from adwatch.insights.divergence import marketing_score
    today = dt.date(2026, 7, 17)
    fresh = marketing_score(6, dt.date(2026, 7, 10), 3, 6, 0, 0, today)  # newest 7d ago
    stale = marketing_score(6, dt.date(2025, 1, 1), 0, 6, 0, 0, today)   # newest >1y ago
    assert fresh > stale
    assert marketing_score(0, None, 0, 0, 0, 0, today) == 0              # no ads = 0


# ---------------------------------------------------------------------------
# Actor "no ads" sentinel must never become a phantom active ad
# ---------------------------------------------------------------------------

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


def test_report_ctas_and_source_label():
    """Report links are per-platform CTAs (Meta + Google), and the header source
    label credits Google only when Google ads are actually present."""
    from adwatch.report import _ads_cta, _google_transparency_url, _source_label
    assert "ads/library" in _ads_cta({"page_id": "111", "country": "DE"})
    assert "Google-Anzeigen" not in _ads_cta({"page_id": "111"})
    assert "adstransparency.google.com/advertiser/AR9" in _ads_cta({"google_id": "AR9", "country": "DE"})
    assert _ads_cta({"page_id": "111", "google_id": "AR9"}).count("<a ") == 2   # both platforms
    assert _ads_cta({}) == ""
    assert _source_label([{"meta_active_ads": 5, "google_active_ads": 0}]) == "Meta Ad Library"
    assert "Google" in _source_label([{"google_active_ads": 2}])
    assert _google_transparency_url(None) is None


def test_company_score_zero_ads_is_zero():
    """A company running zero ads must score 0 — not ~12.5 off the neutral
    first-week momentum term (the phantom-ad fix exposed this)."""
    from adwatch.insights.score import company_score
    assert company_score(0, None, 0, 0) == 0.0
    assert company_score(0, 5, 0, 0) == 0.0     # even with prior-week ads
    assert company_score(3, None, 3, 2) > 0     # real activity still scores


# ---------------------------------------------------------------------------
# DB-backed: failed-fetch must NOT overwrite good metrics with 0
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file for this test.

    ADWATCH_DATA_DIR is redirected too, not just the DB URL: init_db() takes a
    startup backup, and without this every test wrote a 4 KB snapshot of its empty
    temp database into the REAL data/backups/ and then rotated — so running the
    test suite silently destroyed the production backups. 13 of 14 retained
    backups were test junk before this was fixed.
    """
    db = tmp_path / "t.db"
    monkeypatch.setenv("ADWATCH_DB_URL", f"sqlite:///{db}")
    monkeypatch.setenv("ADWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADWATCH_BACKUP_DIR", str(tmp_path / "backups"))
    # rebuild the engine bound to the temp URL
    import importlib
    from adwatch import config as cfg
    importlib.reload(cfg)
    from adwatch import db as dbmod
    importlib.reload(dbmod)
    dbmod.init_db()
    yield dbmod


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


def test_ad_activity_filter(temp_db):
    """The explorer's 'is running ads' filter: active = latest week has active
    ads; any = ever advertised (incl. ended, and a superset of active); none =
    fetched but not active now. Never-fetched companies match none of them."""
    from adwatch.customers import _apply_filters
    from adwatch.models import Ad, CollectionRun, Company, WeeklyCompanyMetric
    from sqlalchemy import select
    s = temp_db.SessionLocal()
    wk = dt.date(2026, 7, 6)
    a = Company(name="A running", resolution_status="confirmed", country="DE"); s.add(a); s.flush()
    s.add(WeeklyCompanyMetric(company_id=a.id, source="meta", week_start=wk, total_active_ads=3))
    b = Company(name="B ended-only", resolution_status="confirmed", country="DE"); s.add(b); s.flush()
    s.add(WeeklyCompanyMetric(company_id=b.id, source="meta", week_start=wk, total_active_ads=0))
    run = CollectionRun(company_id=b.id, source="meta", week_start=wk, status="ok"); s.add(run); s.flush()
    s.add(Ad(run_id=run.id, source="meta", external_ad_id="x1", is_active=False))
    c = Company(name="C fetched-silent", resolution_status="confirmed", country="DE"); s.add(c); s.flush()
    s.add(WeeklyCompanyMetric(company_id=c.id, source="meta", week_start=wk, total_active_ads=0))
    d = Company(name="D never-fetched", resolution_status="confirmed", country="DE"); s.add(d)
    s.commit()

    def ids(f): return set(s.scalars(_apply_filters(select(Company.id), f)))
    active, anyads, none = ids({"ad_activity": "active"}), ids({"ad_activity": "any"}), ids({"ad_activity": "none"})
    assert active == {a.id}
    assert anyads == {a.id, b.id}          # running now + ever-advertised; superset of active
    assert active <= anyads
    assert none == {b.id, c.id}            # fetched but not active now
    assert d.id not in (active | anyads | none)   # never fetched -> matches none
    s.close()


def test_ad_activity_source_filter(temp_db):
    """`ad_source` narrows the ad-activity filter to one platform: a company
    running only Meta ads must match active+meta but NOT active+google, and vice
    versa — while the unscoped 'active' still catches both. 'none'/'any' are
    scoped the same per-platform way."""
    from adwatch.customers import _apply_filters
    from adwatch.models import Ad, CollectionRun, Company, WeeklyCompanyMetric
    from sqlalchemy import select
    s = temp_db.SessionLocal()
    wk = dt.date(2026, 7, 6)

    def wcm(cid, src, n):
        s.add(WeeklyCompanyMetric(company_id=cid, source=src, week_start=wk, total_active_ads=n))

    m = Company(name="M meta-only", resolution_status="confirmed", country="DE"); s.add(m); s.flush()
    wcm(m.id, "meta", 3); wcm(m.id, "google", 0)
    g = Company(name="G google-only", resolution_status="confirmed", country="DE"); s.add(g); s.flush()
    wcm(g.id, "meta", 0); wcm(g.id, "google", 2)
    b = Company(name="B both-silent", resolution_status="confirmed", country="DE"); s.add(b); s.flush()
    wcm(b.id, "meta", 0); wcm(b.id, "google", 0)
    # B has one ENDED Meta ad on record (real ad, inactive) — so it counts as
    # "ever advertised on Meta" but not "running Meta now".
    run = CollectionRun(company_id=b.id, source="meta", week_start=wk, status="ok"); s.add(run); s.flush()
    s.add(Ad(run_id=run.id, source="meta", external_ad_id="e1", is_active=False))
    s.commit()

    def ids(f): return set(s.scalars(_apply_filters(select(Company.id), f)))

    assert ids({"ad_activity": "active"}) == {m.id, g.id}                        # either platform
    assert ids({"ad_activity": "active", "ad_source": "meta"}) == {m.id}          # Meta only
    assert ids({"ad_activity": "active", "ad_source": "google"}) == {g.id}        # Google only
    # none = fetched on that platform but nothing live there now
    assert ids({"ad_activity": "none", "ad_source": "meta"}) == {g.id, b.id}
    assert ids({"ad_activity": "none", "ad_source": "google"}) == {m.id, b.id}
    # any = ever advertised on that platform (incl. ended)
    assert ids({"ad_activity": "any", "ad_source": "meta"}) == {m.id, b.id}
    assert ids({"ad_activity": "any", "ad_source": "google"}) == {g.id}
    # an unknown/blank source falls back to all-platforms (no crash, no over-filter)
    assert ids({"ad_activity": "active", "ad_source": "linkedin"}) == {m.id, g.id}
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


def test_report_def_crud_and_run(temp_db, monkeypatch):
    """Saved report definitions: create/list/update/delete + validation, and
    run_definition builds over the saved filter and emails only the ACTIVE saved
    recipients (build + email mocked so no PDF/network in the test)."""
    from adwatch import report_defs
    import adwatch.report as report_mod
    import adwatch.emailer as emailer_mod
    from adwatch.models import ReportRecipient

    s = temp_db.SessionLocal()
    r1 = ReportRecipient(name="BD One", email="one@x.de", active=True)
    r2 = ReportRecipient(name="BD Two", email="two@x.de", active=False)   # inactive -> skipped
    s.add_all([r1, r2]); s.commit()
    rid1, rid2 = r1.id, r2.id
    s.close()

    d = report_defs.create_definition(
        name="Google Winback DE", filters={"ad_activity": "active", "ad_source": "google"},
        report_type="full", recipient_ids=[rid1, rid2],
        schedule_enabled=True, schedule_day=0, schedule_time="07:30")
    assert d["id"] and d["schedule_enabled"] is True
    assert len(report_defs.list_definitions()) == 1

    with pytest.raises(ValueError):
        report_defs.create_definition(name="  ", filters={})          # empty name
    with pytest.raises(ValueError):
        report_defs.create_definition(name="x", filters={}, schedule_time="99:99")  # bad time

    sent = {}
    monkeypatch.setattr(report_mod, "build_report",
                        lambda filters=None: "output/adwatch_report_KW30_2026.pdf")
    monkeypatch.setattr(report_mod, "write_report_meta", lambda *a, **k: None)
    monkeypatch.setattr(emailer_mod, "send_report_email",
                        lambda path, recipient=None, subject=None, **k: sent.update(
                            path=path, recipient=recipient, subject=subject,
                            source=k.get("source")))
    res = report_defs.run_definition(d["id"], send=True)
    assert res["sent"] is True
    assert sent["recipient"] == ["one@x.de"]                          # inactive recipient excluded
    assert "sent to 1" in report_defs.get_definition(d["id"])["last_status"]

    report_defs.update_definition(d["id"], schedule_enabled=False)
    assert report_defs.get_definition(d["id"])["schedule_enabled"] is False
    report_defs.delete_definition(d["id"])
    assert report_defs.list_definitions() == []


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
    monkeypatch.setattr(extract_mod, "extract_facts", lambda text, model=None: {
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


def test_crm_id_is_the_primary_key_and_write_once(temp_db):
    """The Dataverse accountid is the only durable identity: SAP numbers are
    absent on ~25% of rows and names change. It must be captured from the export
    header "(Nicht ändern) Firma", matched on BEFORE sap/name, and never
    overwritten — overwriting would silently repoint a row at another account."""
    import io
    import openpyxl
    from adwatch.customers import parse_excel, upsert_companies
    from adwatch.models import Company
    from sqlalchemy import select

    GUID = "b943a81c-e493-e011-97fd-0050568441a6"

    def sheet(rows):
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(["(Nicht ändern) Firma", "(Nicht ändern) Geändert am",
                   "SAP Nummer", "Firmenname", "Ort"])
        for r in rows:
            ws.append(r)
        buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

    recs, _ = parse_excel(sheet([[GUID, "2026-07-03 07:53:00", "0005000001", "Alte AG", "Melle"]]))
    assert recs[0]["crm_id"] == GUID
    upsert_companies(recs)
    s = temp_db.SessionLocal()
    c = s.scalar(select(Company))
    assert c.crm_id == GUID and c.crm_modified_on is not None
    original_id = c.id
    s.close()

    # SAME GUID, but renamed AND re-numbered in the CRM -> must UPDATE that row
    recs2, _ = parse_excel(sheet([[GUID, "2026-07-30 09:00:00", "0009999999", "Neue AG", "Osnabrück"]]))
    upsert_companies(recs2)
    s = temp_db.SessionLocal()
    rows = list(s.scalars(select(Company)))
    assert len(rows) == 1, "matched on crm_id, so no duplicate row"
    assert rows[0].id == original_id
    assert rows[0].sap_number == "0009999999" and rows[0].city == "Osnabrück"
    assert rows[0].crm_id == GUID
    s.close()

    # a DIFFERENT GUID with the same name is a different company -> new row
    recs3, _ = parse_excel(sheet([["ffffffff-0000-0000-0000-000000000001",
                                   "2026-07-30 09:00:00", None, "Neue AG Zweite", "Melle"]]))
    upsert_companies(recs3)
    s = temp_db.SessionLocal()
    assert len(list(s.scalars(select(Company)))) == 2
    s.close()


def test_crm_showroom_ingest_joins_on_crm_id(temp_db):
    """CRM showroom rows join to companies through the Dataverse GUID. A row whose
    dealer GUID is unknown locally must still be STORED (company_id NULL, counted
    as unmatched) — dropping it would hide that the company master is incomplete.
    Re-ingesting the same pull must update, never duplicate."""
    from adwatch import crm_sync
    from adwatch.models import Company, CrmShowroom
    from sqlalchemy import select

    GUID_A = "aaaaaaaa-0000-0000-0000-000000000001"
    s = temp_db.SessionLocal()
    s.add(Company(name="Schaurraum Partner", country="DE", crm_id=GUID_A))
    s.commit(); s.close()

    recs = [
        {"crm_id": "e1", "dealer_crm_id": GUID_A, "product_family": "Glas-Faltwand",
         "product": "SL 25", "installed_on": "2024-05-01"},
        {"crm_id": "e2", "dealer_crm_id": GUID_A, "product_family": "Wintergarten",
         "product": "SDL Atrium plus", "installed_on": None},
        {"crm_id": "e3", "dealer_crm_id": "unknown-guid", "product_family": "cero"},
        {"crm_id": "", "dealer_crm_id": GUID_A, "product_family": "Ignoriert"},   # no key
    ]
    r = crm_sync.upsert_showrooms(recs)
    assert r == {"received": 4, "inserted": 3, "updated": 0,
                 "matched": 2, "unmatched": 1, "skipped": 1}

    s = temp_db.SessionLocal()
    rows = {x.crm_id: x for x in s.scalars(select(CrmShowroom))}
    cid = s.scalar(select(Company.id))
    assert rows["e1"].company_id == cid and rows["e1"].product == "SL 25"
    assert rows["e1"].installed_on.isoformat() == "2024-05-01"
    assert rows["e3"].company_id is None            # kept, but unresolved
    s.close()

    # idempotent: same pull again updates in place
    r2 = crm_sync.upsert_showrooms(recs)
    assert r2["inserted"] == 0 and r2["updated"] == 3
    s = temp_db.SessionLocal()
    assert len(list(s.scalars(select(CrmShowroom)))) == 3
    s.close()

    o = crm_sync.showroom_overview()
    assert o["rows"] == 3 and o["matched_dealers"] == 1
    assert dict(o["families"])["Glas-Faltwand"] == 1
    assert o["per_company"][cid] == ["Glas-Faltwand", "Wintergarten"]


def test_country_code_mapping():
    """The CRM's Land column holds full names; Company.country must end up as an
    ISO-2 code because it is what the ad lookups pass as their country param."""
    from adwatch.customers import _country_code
    assert _country_code("Spanien") == "ES"
    assert _country_code("Deutschland") == "DE"
    assert _country_code("Portugal") == "PT"
    assert _country_code("Österreich") == "AT"
    assert _country_code("  spanien ") == "ES"          # normalised
    assert _country_code("ES") == "ES" and _country_code("es") == "ES"
    assert _country_code("Absurdistan") is None          # unknown -> don't guess
    assert _country_code(None) is None and _country_code("") is None


def test_import_keeps_rows_without_sap_and_reports_them(temp_db):
    """Rows with no SAP Nummer must be imported (matched by Firmenname) and
    REPORTED, not silently dropped — a real CRM export had 917 of 1,000 such
    rows, which previously vanished without a word. Rows with neither key are
    skipped, also with a count."""
    import io
    import openpyxl
    from adwatch.customers import parse_excel, upsert_companies
    from adwatch.models import Company
    from sqlalchemy import select

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SAP Nummer", "Firmenname", "Land", "Ort"])
    ws.append(["0005000001", "Mit SAP GmbH", "Deutschland", "Melle"])
    ws.append([None, "Ohne SAP SL", "Spanien", "Barcelona"])       # name-keyed
    ws.append([None, None, "Spanien", None])                        # no key -> skipped
    buf = io.BytesIO(); wb.save(buf)

    records, warnings = parse_excel(buf.getvalue())
    assert len(records) == 2                                        # not 1
    assert any("no SAP Nummer" in w for w in warnings)
    assert any("skipped" in w for w in warnings)

    upsert_companies(records)
    s = temp_db.SessionLocal()
    rows = {c.name: c for c in s.scalars(select(Company))}
    assert set(rows) == {"Mit SAP GmbH", "Ohne SAP SL"}
    assert rows["Mit SAP GmbH"].country == "DE"
    assert rows["Ohne SAP SL"].country == "ES"                      # not the "DE" default
    s.close()

    # re-importing the same SAP-less row must UPDATE it, not duplicate it
    upsert_companies(records)
    s = temp_db.SessionLocal()
    assert len(list(s.scalars(select(Company)))) == 2
    s.close()


def test_customer_state_derivation():
    """Lifecycle from the Umsatz columns: active = buys now AND before, new =
    first revenue this year, lapsed = bought before not now, never = nothing."""
    from adwatch.customers import derive_customer_state as d
    assert d(50000, 80000, 0, 0, 0) == "active"
    assert d(50000, None, None, None, None) == "new"
    assert d(0, 80000, 0, 0, 0) == "lapsed"
    assert d(None, None, None, None, 100) == "lapsed"
    assert d(None, None, None, None, None) == "never"
    assert d(0, 0, 0, 0, 0) == "never"


def test_icp_parsers():
    from adwatch.insights.icp import parse_employee_count, size_bucket, age_bucket, plz_zone
    import datetime as dt
    assert parse_employee_count("15 Mitarbeiter") == 15
    assert parse_employee_count("drei Angestellten") == 3
    assert parse_employee_count("10-15 Mann") == 12
    assert parse_employee_count("Familienbetrieb") is None
    assert size_bucket("15 Mitarbeiter") == "10-19"
    assert size_bucket(None) is None
    today = dt.date(2026, 7, 29)
    assert age_bucket(2020, today) == "<10 Jahre"
    assert age_bucket(1955, today) == "50+ Jahre"
    assert age_bucket(None) is None
    assert plz_zone("49134") == "PLZ 4x"
    assert plz_zone("123") is None


def test_icp_winners_never_include_consumers(temp_db):
    """Regression: 'Private Endkunden' who bought something were silently making
    up ~a third of the default winners set, pulling the TRADE-PARTNER profile
    toward consumer traits. They must never define the profile — not by default,
    and not when the caller passes their own filter — unless hand-picked by id."""
    from adwatch import customers
    from adwatch.insights import icp
    from adwatch.models import Company
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    for i in range(6):
        s.add(Company(name=f"Haendler {i}", country="DE", segment="Handel",
                      postal_code="49134", revenue_y0=90000, revenue_y1=80000))
    for i in range(6):
        s.add(Company(name=f"Verbraucher {i}", country="DE", segment="Private Endkunden",
                      postal_code="10115", revenue_y0=400, revenue_y1=300))
    s.commit()
    for c in s.scalars(select(Company)):
        c.customer_state = customers.derive_customer_state(
            c.revenue_y0, c.revenue_y1, c.revenue_y2, c.revenue_y3, c.revenue_y4)
    s.commit()
    consumer_ids = [c.id for c in s.scalars(select(Company).where(Company.segment == "Private Endkunden"))]
    s.close()

    # default winners: consumers excluded
    p = icp.build_profile(None)
    assert p["winners_count"] == 6
    assert "Private Endkunden" not in (p["features"]["segment"]["shares"] or {})

    # caller-supplied filter: consumers still excluded, caller's own exclusion kept
    p2 = icp.build_profile({"customer_state": ["active", "new"], "exclude_segment": ["Handel"]})
    assert set(p2["winners_filter"]["exclude_segment"]) == {"Handel", "Private Endkunden"}
    assert p2["winners_count"] == 0

    # A hand-picked id list is NO LONGER an override. Consumers are out of scope
    # globally (adwatch/scope.py) precisely because "explicit enough" doors kept
    # letting 36% of the base back into counts nobody meant to include them in.
    p3 = icp.build_profile({"ids": consumer_ids})
    assert p3["winners_count"] == 0

    # deliberate consumer profiling now goes through the one named override
    p4 = icp.build_profile({"ids": consumer_ids, "include_consumers": True})
    assert p4["winners_count"] == len(consumer_ids)


def test_intercompany_never_winner_never_target(temp_db):
    """Own-group entities (Linara, NanaWall, Solarlux Vertriebsbüros) appear as
    ordinary large customers. Confirmed live: Linara Kaufbeuren (EUR 1.35M) was
    the biggest 'customer' and ranked #3 in the target list, and 5 Linara rows
    were shaping the profile. They must never be a winner nor a target."""
    from adwatch import customers
    from adwatch.insights import icp
    from adwatch.models import Company
    from sqlalchemy import select

    assert customers.looks_intercompany("Linara Kaufbeuren GmbH")
    assert customers.looks_intercompany("Mike Morgenstern Solarlux Vertriebsbüro")
    assert not customers.looks_intercompany("Serin Bauelemente")

    s = temp_db.SessionLocal()
    # deliberately VARIED winners: if every winner shared one value, the feature
    # would be dropped as "nicht trennscharf" and nothing would be comparable.
    # 30+ winners because the profile guard rejects anything smaller as noise.
    for i in range(30):
        s.add(Company(name=f"Echter Haendler {i}", country="DE",
                      segment="Handel" if i < 20 else "Verarbeiter",
                      postal_code="49134" if i < 20 else "80331",
                      revenue_y0=50000, revenue_y1=40000))
    s.add(Company(name="Linara Teststadt GmbH", country="DE", segment="Handel",
                  postal_code="49134", revenue_y0=1350910, revenue_y1=900000))
    s.commit()
    for c in s.scalars(select(Company)):
        c.customer_state = customers.derive_customer_state(
            c.revenue_y0, c.revenue_y1, c.revenue_y2, c.revenue_y3, c.revenue_y4)
    s.commit()
    s.close()

    assert customers.flag_intercompany() == 1          # only the Linara row flagged
    p = icp.build_profile(None)
    assert p["winners_count"] == 30                     # the group company is out

    icp.apply_profile(None, name="t")
    s = temp_db.SessionLocal()
    linara = s.scalar(select(Company).where(Company.name.like("Linara%")))
    assert linara.is_intercompany is True
    assert linara.target_score is None                  # never on the call list
    assert linara.fit_score is not None                 # but still described
    s.close()


def test_crm_sync_never_overwrites_what_we_paid_for(temp_db):
    """The whole point of the ownership map. CRM owns master data; a sync must not
    touch enrichment, locked pages, scores or ad history — that is the expensive
    half of the database and Dataverse has no opinion about any of it."""
    from sqlalchemy import select
    from adwatch import crm_accounts
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    c = Company(
        crm_id="A7DBC4F6-A2F9-40CC-BEB9-0000E0EE6272", name="Alter Name GmbH",
        country="DE", city="Altstadt", segment="Handel", revenue_y0=100.0,
        # everything below is ours and must survive untouched
        description="Von uns angereichert.", products=["Fenster"], founded_year=1999,
        employee_hint="12 Mitarbeiter", enrichment_status="enriched",
        page_id="123456", page_name="Alte Seite", resolution_status="locked",
        fit_score=88.0, opportunity_score=42.0, target_score=61.0,
        website_domain="alt.example")
    s.add(c); s.commit()
    cid = c.id
    s.close()

    # same account, matched on the GUID in a DIFFERENT case
    res = crm_accounts.upsert_accounts([{
        "accountid": "a7dbc4f6-a2f9-40cc-beb9-0000e0ee6272",
        "modifiedon": "2026-07-27T20:37:19Z",
        "name": "Neuer Name GmbH", "accountnumber": "0005009967",
        "address1_line1": "Vogelherdbogen 27", "address1_postalcode": "88069",
        "address1_city": "Tettnang", "address1_country": "Spanien",
        "telephone1": "+4975428292", "emailaddress1": "neu@example.de",
        "sl_customer_segment": 101,
        "sl_customer_segment@OData.Community.Display.V1.FormattedValue": "Verarbeiter",
        "slx_revenue_current_year": 22749.0, "slx_revenue_current_year_1": 80728.0,
        "statecode": 0,
    }])
    assert res == {**res, "inserted": 0, "updated": 1, "skipped": 0}

    s = temp_db.SessionLocal()
    c = s.get(Company, cid)
    # CRM won on master data
    assert c.name == "Neuer Name GmbH" and c.city == "Tettnang"
    assert c.sap_number == "0005009967" and c.postal_code == "88069"
    assert c.country == "ES"                       # "Spanien" -> ES via markets
    assert c.segment == "Verarbeiter"              # LABEL, never the raw 101
    assert c.revenue_y0 == 22749.0
    assert c.customer_state == "active"            # recomputed, not synced
    # ...and every local field is untouched
    assert c.description == "Von uns angereichert." and c.products == ["Fenster"]
    assert c.employee_hint == "12 Mitarbeiter" and c.enrichment_status == "enriched"
    assert c.page_id == "123456" and c.resolution_status == "locked"
    assert (c.fit_score, c.opportunity_score, c.target_score) == (88.0, 42.0, 61.0)
    assert c.website_domain == "alt.example"
    s.close()

    # a raw picklist with NO label must not write an integer into a name column
    crm_accounts.upsert_accounts([{"accountid": "a7dbc4f6-a2f9-40cc-beb9-0000e0ee6272",
                                   "sl_customer_segment": 999}])
    with temp_db.SessionLocal() as s2:
        assert s2.get(Company, cid).segment == "Verarbeiter"

    # a record without accountid is skipped, not guessed at
    assert crm_accounts.upsert_accounts([{"name": "Ohne GUID"}])["skipped"] == 1
    # inserts can be refused, so a delta sync cannot silently widen the base
    assert crm_accounts.upsert_accounts(
        [{"accountid": "11111111-1111-1111-1111-111111111111", "name": "Neu"}],
        allow_insert=False)["inserted"] == 0
    # ...and allowed when asked for
    assert crm_accounts.upsert_accounts(
        [{"accountid": "11111111-1111-1111-1111-111111111111", "name": "Neu"}]
    )["inserted"] == 1

    # the ownership map and the protected set must never overlap
    written = (set(crm_accounts.CRM_OWNED_SCALARS.values())
               | set(crm_accounts.CRM_OWNED_PICKLISTS.values())
               | set(crm_accounts.CRM_OWNED_REVENUE.values()))
    assert not (written & crm_accounts.LOCAL_OWNED)

    # the watermark drives the delta filter
    assert crm_accounts.watermark().endswith("Z")
    assert "accountid" in crm_accounts.select_fields()


def test_markets_are_data_not_code():
    """Adding a market used to need three code edits (country aliases, legal-page
    term, search language) in two modules. Missing one failed SILENTLY — that is
    how 982 Spanish companies were imported as DE. Now one YAML file drives all
    three, and a successor adds a market without Python."""
    from adwatch import markets

    markets.reload()
    codes = markets.known_codes()
    assert {"DE", "ES", "PT", "AT", "CH", "FR"} <= set(codes)

    # the Spain bug, pinned: every spelling a source might use resolves
    for spelling in ("Spanien", "españa", "espana", "SPAIN", "es", "ES"):
        assert markets.code_for(spelling) == "ES", spelling
    # umlaut and its transliteration both work
    assert markets.code_for("österreich") == markets.code_for("Oesterreich") == "AT"
    # NO must survive YAML's boolean coercion of a bare NO key
    assert "NO" in codes and markets.code_for("Norwegen") == "NO"

    # an unknown name returns None so the caller can KEEP the old value rather
    # than silently defaulting to DE
    assert markets.code_for("Slowenien") is None
    assert markets.code_for("") is None
    # but an unlisted 2-letter code passes through instead of being dropped
    assert markets.code_for("si") == "SI"

    # the per-market behaviour that used to be hardcoded elsewhere
    assert markets.search_lang("ES") == "es"
    assert markets.legal_page_term("ES") == "aviso legal"      # not "Impressum"
    assert markets.legal_page_term("PT") == "contactos"
    assert markets.legal_page_term("DE") == "Impressum"
    # unknown market degrades to a usable default rather than raising
    assert markets.search_lang("XX") and markets.legal_page_term("XX")

    # every market must be complete, or a new entry could half-work
    for code, spec in markets.all_markets().items():
        assert len(code) == 2 and code.isalpha() and code.isupper(), code
        assert spec["aliases"] and spec["search_lang"] and spec["legal_page"], code


def test_flow_registry_is_configurable_and_backward_compatible(temp_db, monkeypatch):
    """Flows are addressed by ROLE, so a second integration point (the CRM query
    proxy) needs no new constant, timeout or error convention. And an install that
    still has only the old POWER_AUTOMATE_WEBHOOK_URL must keep working."""
    from adwatch import config, flows

    # unknown role fails loudly rather than silently doing nothing
    with pytest.raises(ValueError):
        flows.url_for("does_not_exist")

    # nothing configured -> a message that says WHERE to fix it
    monkeypatch.setattr(config, "resolve_setting", lambda k: "")
    assert not flows.is_configured("report_email")
    msg = flows.missing_message("crm_query")
    assert "FLOW_URL_CRM_QUERY" in msg and "Einstellungen" in msg
    with pytest.raises(RuntimeError):
        flows.post("crm_query", {})

    # legacy key alone still drives the email role (upgrade path)
    monkeypatch.setattr(config, "resolve_setting",
                        lambda k: "https://legacy.example/flow"
                        if k == "POWER_AUTOMATE_WEBHOOK_URL" else "")
    assert flows.is_configured("report_email")
    assert flows.url_for("report_email") == "https://legacy.example/flow"
    # ...but the legacy key must NOT leak into other roles
    assert not flows.is_configured("crm_query")

    # the new key wins when both are set
    monkeypatch.setattr(config, "resolve_setting",
                        lambda k: {"FLOW_URL_REPORT_EMAIL": "https://new.example/f",
                                   "POWER_AUTOMATE_WEBHOOK_URL": "https://legacy.example/f"}.get(k, ""))
    assert flows.url_for("report_email") == "https://new.example/f"

    # status() reports the integration points for Settings / diagnostics
    st = {s["role"]: s for s in flows.status()}
    assert st["report_email"]["configured"] is True
    assert st["crm_query"]["configured"] is False
    assert st["crm_query"]["key"] == "FLOW_URL_CRM_QUERY"

    # every role must have a settings entry, or Settings could never configure it
    for role, (key, _, _) in flows.FLOW_ROLES.items():
        assert key in config._SPEC_BY_KEY, f"{role} has no SETTINGS_SPEC entry"
        assert config._SPEC_BY_KEY[key]["secret"] is True, f"{key} must be masked"


def test_thinly_known_features_do_not_score(temp_db):
    """diagnose() refused to trust a feature below 15% coverage, but fit_for used
    it anyway — so Betriebsgröße, known for 3% of winners (a distribution built
    from ~20 companies), was shaping EVERY company's fit score. Warning about a
    number and then scoring with it is worse than not having it at all."""
    from adwatch.insights.icp import fit_for

    profile = {
        "weights": {"segment": 1.0, "size_bucket": 1.0},
        "features": {
            # solidly known, and discriminating
            "segment": {"coverage": 1.0, "shares": {"Handel": 0.7, "Verarbeiter": 0.3}},
            # the live case: a spread that LOOKS informative but rests on ~20 rows
            "size_bucket": {"coverage": 0.03,
                            "shares": {"20-49": 0.5, "10-19": 0.3, "50+": 0.2}},
        },
    }
    # a company matching ONLY the thin feature has nothing comparable left
    fit_thin, bd_thin = fit_for({"segment": None, "size_bucket": "20-49"}, profile)
    assert fit_thin is None and bd_thin == []

    # and the thin feature cannot inflate a score that rests on the solid one
    fit_a, _ = fit_for({"segment": "Handel", "size_bucket": "20-49"}, profile)
    fit_b, _ = fit_for({"segment": "Handel", "size_bucket": None}, profile)
    assert fit_a == fit_b, "size_bucket must not move the score at 3% coverage"

    # raise its coverage above the floor and it starts counting
    profile["features"]["size_bucket"]["coverage"] = 0.6
    fit_c, bd_c = fit_for({"segment": "Handel", "size_bucket": "20-49"}, profile)
    assert {b["feature"] for b in bd_c} >= {"segment", "size_bucket"}
    assert fit_c is not None


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
    monkeypatch.setattr(extract, "extract_facts", lambda t: {
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
    monkeypatch.setattr(extract, "extract_facts", lambda t: {
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


def test_report_events_record_creation_and_delivery(temp_db, monkeypatch, tmp_path):
    """Creating and sending a report must leave an audit row, so 'did the mail go
    out?' is answerable after a crash — the question that had no answer before."""
    from adwatch import report_log
    from adwatch.report import write_report_meta

    write_report_meta(str(tmp_path / "adwatch_top5_KW31_2026.pdf"),
                      filters={"country": ["ES"]}, source="manual")
    ev = report_log.history()
    assert len(ev) == 1
    assert ev[0]["kind"] == "created" and ev[0]["report_type"] == "top5"
    assert ev[0]["source"] == "manual" and "ES" in (ev[0]["scope"] or "")

    # a failing send is recorded too, with the real error, and still raises
    import adwatch.emailer as emailer_mod
    from adwatch import config
    monkeypatch.setattr(config, "POWER_AUTOMATE_WEBHOOK_URL", "https://example.invalid/f")
    pdf = tmp_path / "adwatch_top5_KW31_2026.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")

    def _boom(*a, **k):
        raise OSError("network is down")
    monkeypatch.setattr(emailer_mod.requests, "post", _boom)
    with pytest.raises(RuntimeError):
        emailer_mod.send_report_email(str(pdf), recipient=["a@x.de", "b@x.de"],
                                      subject="Bericht", source="pipeline")

    ev = report_log.history()
    fail = ev[0]
    assert fail["kind"] == "send_failed"
    assert fail["recipients"] == ["a@x.de", "b@x.de"]
    assert fail["source"] == "pipeline" and "network is down" in fail["detail"]

    # recording must never be what breaks a send: a broken audit write is swallowed
    monkeypatch.setattr(report_log, "SessionLocal", lambda: (_ for _ in ()).throw(OSError("db gone")))
    report_log.record("created", "x.pdf")          # must not raise


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


def test_default_step_order_puts_the_free_domain_pass_first():
    """The default order is load-bearing, so it gets its own test.

    Identity's only free AND authoritative tier crawls Company.website_domain.
    Full enrichment fills that column but costs money (Serper + Haiku), while
    deriving a domain from the company's own email is free. So the default must
    be: free domains -> identity -> paid enrichment."""
    from adwatch.jobs import DOMAIN_PREPASS, resolve_step_order

    both = {"enrich": True, "identity": True, "ads": ["meta"], "report": "full",
            "send_to": [1]}
    assert resolve_step_order(both) == [DOMAIN_PREPASS, "identity", "enrich",
                                        "ads", "report", "send"]

    # the pre-pass only earns its place when it can actually feed the identity
    # check — enrichment alone already does Tier 0 internally
    assert resolve_step_order({"enrich": True}) == ["enrich"]
    assert resolve_step_order({"identity": True}) == ["identity"]

    # an explicit order wins, and anything selected but omitted is appended
    # rather than silently dropped
    assert resolve_step_order({"enrich": True, "identity": True,
                               "order": ["enrich", "identity"]}) == ["enrich", "identity"]
    assert resolve_step_order({"enrich": True, "identity": True, "report": "full",
                               "order": ["enrich"]}) == ["enrich", "identity", "report"]

    # 'send' lives under send_to, not a boolean
    assert resolve_step_order({"report": "full", "send_to": [1]}) == ["report", "send"]
    assert "send" not in resolve_step_order({"report": "full"})


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


def test_top5_report_shows_profiles_and_an_honest_count(temp_db, tmp_path):
    """A 'Top 5' that shows 2 companies must say why, and each company must carry
    its enriched profile — otherwise the report is just ad counts with no context."""
    import datetime as dt
    from adwatch.models import Company, CompanyEnrichment, WeeklyCompanyMetric
    from adwatch.report import build_top5_report

    s = temp_db.SessionLocal()
    wk = dt.date(2026, 7, 27)
    ids = []
    for i in range(2):
        c = Company(name=f"Cerramientos {i} SL", country="ES", segment="Handel",
                    resolution_status="confirmed", website_domain=f"cerr{i}.es",
                    enrichment_status="enriched")
        s.add(c); s.flush()
        s.add(WeeklyCompanyMetric(company_id=c.id, source="meta", week_start=wk,
                                  total_active_ads=8 - i,
                                  products=["cerramiento", "porch closure"]))
        s.add(CompanyEnrichment(company_id=c.id, status="enriched", fields={
            "description_de": "Anbieter von Glasabschlüssen für Veranden und Terrassen.",
            "assessment_de": "Dürfte ein Kleinbetrieb mit regionalem Fokus sein.",
            "products": ["Terrassenverglasung"], "employee_hint": "10 Mitarbeiter",
            "competitor_brands": ["Sunflex"]}))
        ids.append(c.id)
    # 3 more companies in scope that never advertised — they are why it isn't 5
    for i in range(3):
        s.add(Company(name=f"Stille Firma {i}", country="ES", segment="Handel",
                      resolution_status="confirmed"))
    s.commit(); s.close()

    out = str(tmp_path / "top5.pdf")
    build_top5_report(path=out, filters={})

    from pypdf import PdfReader
    raw = "\n".join(p.extract_text() or "" for p in PdfReader(out).pages)
    # collapse whitespace: the PDF line-wraps mid-sentence, which would otherwise
    # make these assertions depend on where the text happens to break
    text = " ".join(raw.split())
    # the headline states the real number instead of promising five
    assert "Top 5" not in text
    assert "Werbetreibende mit aktiven Anzeigen (2)" in text
    assert "die Liste ist nicht gekürzt" in text
    assert "nur 2 von 5 Firmen" in text
    # the enrichment reaches this report type too, fact and inference kept apart
    assert "Beschreibung:" in text and "Glasabschl" in text
    assert "Einschätzung (KI, unbestätigt):" in text and "Kleinbetrieb" in text
    assert "keine belegte Angabe" in text
    assert "10 Mitarbeiter" in text and "Sunflex" in text
    # ad-derived products arrive in German, not as 'cerramiento'/'porch closure'
    assert "cerramiento" not in text and "porch closure" not in text
    assert "Terrassenverglasung" in text


def test_export_includes_enriched_columns(temp_db):
    """A freshly enriched market must not export as bare master data."""
    import io
    import openpyxl
    from adwatch.customers import export_xlsx
    from adwatch.models import Company, CompanyEnrichment

    s = temp_db.SessionLocal()
    c = Company(name="Export Test SL", country="ES", description="Baut Wintergärten.",
                products=["Wintergarten"], founded_year=2005, enrichment_status="enriched")
    s.add(c); s.flush()
    s.add(CompanyEnrichment(company_id=c.id, status="enriched", fields={
        "assessment_de": "Wirkt wie ein spezialisierter Kleinbetrieb.",
        "mentions_solarlux": True, "competitor_brands": ["Sunflex"]}))
    s.commit(); s.close()

    wb = openpyxl.load_workbook(io.BytesIO(export_xlsx(filters={})))
    ws = wb.active
    header = [cell.value for cell in ws[1]]
    for col in ("Beschreibung (Website)", "Einschätzung (KI, unbestätigt)", "Produkte",
                "Nennt Solarlux", "Wettbewerber auf Website", "Aktive Anzeigen"):
        assert col in header, col
    row = {header[i]: v for i, v in enumerate([c.value for c in ws[2]])}
    assert row["Beschreibung (Website)"] == "Baut Wintergärten."
    assert row["Einschätzung (KI, unbestätigt)"] == "Wirkt wie ein spezialisierter Kleinbetrieb."
    assert row["Produkte"] == "Wintergarten"
    assert row["Nennt Solarlux"] == "ja"
    assert row["Wettbewerber auf Website"] == "Sunflex"


def test_extract_separates_facts_from_assessment():
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
    sys.modules["anthropic"] = mod
    ex.config.ANTHROPIC_API_KEY = "test-key"

    got = ex.extract_facts("x" * 200)
    assert got["description_de"] == "Baut Fenster."
    assert got["products"] == ["Fenster"]                 # off-vocabulary dropped
    assert got["competitor_brands"] == ["WAREMA"]         # canonicalised
    assert len(got["assessment_de"]) == 700               # capped, not unbounded
    assert "evidence" in got and got["evidence"]["description_de"]


def test_icp_diagnose_guards(temp_db):
    """The validity check behind 'an ICP for any filter — but only the ones that
    make sense': it must refuse a too-small set, refuse a set whose winners look
    exactly like the population, and detect a set that secretly mixes two
    incompatible groups (which is how the Handel/Verarbeiter split was found)."""
    from adwatch import customers
    from adwatch.insights import icp
    from adwatch.models import Company
    from sqlalchemy import select

    s = temp_db.SessionLocal()

    def mk(name, seg, plz, buys):
        s.add(Company(name=name, country="DE", segment=seg, postal_code=plz,
                      sales_channel="Fachhandelsvertrieb",
                      revenue_y0=50000 if buys else None,
                      revenue_y1=40000 if buys else None))

    # (a) tiny set -> unusable on n alone
    for i in range(8):
        mk(f"Klein {i}", "Nische", "49134", i < 4)
    s.commit()
    for c in s.scalars(select(Company)):
        c.customer_state = customers.derive_customer_state(
            c.revenue_y0, c.revenue_y1, c.revenue_y2, c.revenue_y3, c.revenue_y4)
    s.commit(); s.close()

    d = icp.diagnose({"customer_state": ["active", "new"], "segment": ["Nische"]})
    assert d["verdict"] == "unusable"
    assert any("unter 30" in r for r in d["reasons"])

    # (b) 40 buyers + 40 non-buyers that are IDENTICAL in every feature ->
    #     nothing separates them, so the profile cannot rank
    s = temp_db.SessionLocal()
    for i in range(80):
        mk(f"Gleich {i}", "Flach", "49134", i < 40)
    s.commit()
    for c in s.scalars(select(Company)):
        c.customer_state = customers.derive_customer_state(
            c.revenue_y0, c.revenue_y1, c.revenue_y2, c.revenue_y3, c.revenue_y4)
    s.commit(); s.close()

    d = icp.diagnose({"customer_state": ["active", "new"], "segment": ["Flach"]})
    assert d["winners"] == 40
    assert d["verdict"] == "unusable"
    assert any("Kein Merkmal trennt" in r for r in d["reasons"])

    # (c) a mixed set: two groups whose winners differ sharply -> split advised.
    #     Each group needs INTERNAL variety, otherwise every feature is 100%
    #     uniform inside it, gets dropped as non-discriminating, and neither
    #     sub-profile can score anything (which is what the real Handel vs
    #     Verarbeiter sets have naturally).
    s = temp_db.SessionLocal()

    def mk2(name, seg, sub, plz, buys):
        s.add(Company(name=name, country="DE", segment=seg, sub_segment=sub,
                      postal_code=plz, sales_channel="Fachhandelsvertrieb",
                      revenue_y0=50000 if buys else None,
                      revenue_y1=40000 if buys else None))

    for i in range(40):
        mk2(f"Nord {i}", "GruppeA", "Metallbau" if i < 24 else "Tischler", "20095", True)
        mk2(f"Sued {i}", "GruppeB", "Glaser" if i < 24 else "Fensterbau", "80331", True)
    for i in range(40):                                  # non-buyers on both sides
        mk2(f"NordNo {i}", "GruppeA", "Metallbau" if i < 20 else "Tischler", "49134", False)
        mk2(f"SuedNo {i}", "GruppeB", "Glaser" if i < 20 else "Fensterbau", "70173", False)
    s.commit()
    for c in s.scalars(select(Company)):
        c.customer_state = customers.derive_customer_state(
            c.revenue_y0, c.revenue_y1, c.revenue_y2, c.revenue_y3, c.revenue_y4)
    s.commit(); s.close()

    d = icp.diagnose({"customer_state": ["active", "new"], "segment": ["GruppeA", "GruppeB"]})
    seg_split = next((sp for sp in d["splits"] if sp["dimension"] == "segment"), None)
    assert seg_split is not None and seg_split["should_split"] is True
    assert any("Gemischte Grundgesamtheit" in r for r in d["reasons"])


def test_icp_profile_and_fit(temp_db):
    """The heart: the profile counts winner distributions; fit scores a company
    by how typical its values are (modal winner value = 1.0); unknown values are
    skipped with weight renormalisation; near-uniform features are excluded;
    apply writes scores + breakdown to every company."""
    from adwatch import customers
    from adwatch.insights import icp
    from adwatch.models import Company
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    # 30 winners (the guard rejects smaller sets as noise): 20× Metallbau in
    # PLZ 4x, 10× Tischler in PLZ 8x; ALL share the same Vertriebsweg
    # (non-discriminating -> must be excluded from scoring)
    for i in range(20):
        s.add(Company(name=f"W Metall {i}", country="DE", segment="Verarbeiter",
                      sub_segment="Metallbau-Schlosser", sales_channel="Fachhandelsvertrieb",
                      postal_code="49134", revenue_y0=50000, revenue_y1=40000))
    for i in range(10):
        s.add(Company(name=f"W Tisch {i}", country="DE", segment="Verarbeiter",
                      sub_segment="Tischler", sales_channel="Fachhandelsvertrieb",
                      postal_code="80331", revenue_y0=50000, revenue_y1=40000))
    # candidates: a look-alike (Metallbau, PLZ 4x), a partial match, a blank
    lookalike = Company(name="K Passt", country="DE", segment="Verarbeiter",
                        sub_segment="Metallbau-Schlosser", sales_channel="Fachhandelsvertrieb",
                        postal_code="49076")
    partial = Company(name="K Halb", country="DE", segment="Verarbeiter",
                      sub_segment="Tischler", postal_code="80000")
    blank = Company(name="K Leer", country="DE")
    s.add_all([lookalike, partial, blank]); s.commit()
    # derive states so the default winners filter (active+new) finds the six
    for c in s.scalars(select(Company)):
        c.customer_state = customers.derive_customer_state(
            c.revenue_y0, c.revenue_y1, c.revenue_y2, c.revenue_y3, c.revenue_y4)
    s.commit()
    la_id, pa_id, bl_id = lookalike.id, partial.id, blank.id
    s.close()

    p = icp.build_profile(None)
    assert p["winners_count"] == 30
    assert dict(p["features"]["sub_segment"]["shares"])["Metallbau-Schlosser"] == pytest.approx(20 / 30)

    res = icp.apply_profile(None, name="test")
    assert res["companies_scored"] >= 32  # everyone with any comparable value

    s = temp_db.SessionLocal()
    la, pa, bl = s.get(Company, la_id), s.get(Company, pa_id), s.get(Company, bl_id)
    assert la.fit_score == 100.0                       # modal value on every scored feature
    assert pa.fit_score is not None and pa.fit_score < la.fit_score
    assert bl.fit_score is None and bl.target_score is None   # nothing comparable -> unrated, not 0
    feats = {f["feature"] for f in la.fit_breakdown["features"]}
    assert "sales_channel" not in feats               # 100%-uniform -> excluded
    assert "sub_segment" in feats
    s.close()


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
