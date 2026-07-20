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
