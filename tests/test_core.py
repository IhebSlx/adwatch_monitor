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
    """Point the app at a throwaway SQLite file for this test."""
    db = tmp_path / "t.db"
    monkeypatch.setenv("ADWATCH_DB_URL", f"sqlite:///{db}")
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
