"""CRM, Kundenstamm und Auswertung: Import, Datenqualitaet, ICP, RFM, Profile.

Aufgeteilt aus test_core.py: 197 Tests in einer Datei von 6.000 Zeilen
liessen sich nicht mehr ueberblicken. Fixtures stehen in conftest.py.
"""
import datetime as dt   # noqa: F401

import pytest   # noqa: F401

from hilfen import _write_markt


def _ev(*items):
    """[(date, amount)] from (iso, amount) pairs."""
    import datetime as _dt
    return [(_dt.date.fromisoformat(d), a) for d, a in items]


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
    # Namespaced by country: DE, FR, ES and IT all use five digits, so a
    # country-blind zone equated Barcelona 08036 with German 0xxxx (Saxony).
    # 6.023 non-German companies were carrying a German zone, 1.700 Spanish.
    assert plz_zone("49134", "DE") == "DE 4x"
    assert plz_zone("08036", "ES") == "ES 0x"
    assert plz_zone("75008", "FR") == "FR 7x"
    assert plz_zone("08036", "ES") != plz_zone("08036", "DE")
    assert plz_zone("49134") == "DE 4x", "ohne Land bleibt DE die Annahme"
    assert plz_zone("123", "DE") is None
    assert plz_zone("1010", "AT") is None, "vierstellig — keine Zone, nicht geraten"

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

def test_architects_are_scored_on_projects_not_revenue(temp_db):
    """The business fact that broke the ICP: architects specify, they never buy.
    All 808 architect accounts converted at 0%, which made the headline lift look
    like signal when it was really 'architects aren't dealers'.

    The fix is a SECOND outcome measure: an architect's value is the project volume
    they specify, which lives on the opportunity and never on their account."""
    from adwatch import prescriptors
    from adwatch.models import Company, CrmOpportunity

    s = temp_db.SessionLocal()
    star = Company(name="Star Architekten", segment="Architekten", country="DE",
                   crm_id="AAAA1111-0000-0000-0000-000000000001", revenue_y0=0)
    quiet = Company(name="Stille Architekten", segment="Architekten", country="DE",
                    crm_id="BBBB2222-0000-0000-0000-000000000002", revenue_y0=0)
    dealer = Company(name="Haendler GmbH", segment="Handel", country="DE",
                     crm_id="CCCC3333-0000-0000-0000-000000000003", revenue_y0=50000)
    s.add_all([star, quiet, dealer]); s.commit()
    star_id, quiet_id = star.id, quiet.id
    s.add_all([
        # two won projects the architect specified, ordered by the dealer
        CrmOpportunity(crm_id="o1", state="gewonnen", order_value=120000.0,
                       architect_crm_id="aaaa1111-0000-0000-0000-000000000001",
                       parent_account_crm_id="cccc3333-0000-0000-0000-000000000003",
                       building_type="Bürogebäude", created_on=dt.datetime(2025, 3, 1)),
        CrmOpportunity(crm_id="o2", state="gewonnen", order_value=80000.0,
                       architect_crm_id="aaaa1111-0000-0000-0000-000000000001",
                       building_type="Villen", created_on=dt.datetime(2026, 1, 9)),
        CrmOpportunity(crm_id="o3", state="verloren", order_value=40000.0,
                       architect_crm_id="aaaa1111-0000-0000-0000-000000000001",
                       created_on=dt.datetime(2025, 6, 1)),
        # the CRM sometimes puts architect AND end customer on the same account —
        # that must count as ONE project for them, not two
        CrmOpportunity(crm_id="o4", state="offen", order_value=10000.0,
                       architect_crm_id="bbbb2222-0000-0000-0000-000000000002",
                       end_customer_crm_id="bbbb2222-0000-0000-0000-000000000002",
                       created_on=dt.datetime(2026, 2, 2)),
    ])
    s.commit(); s.close()

    star_inf = prescriptors.influence_for("AAAA1111-0000-0000-0000-000000000001")
    assert star_inf["projects"] == 3 and star_inf["won"] == 2 and star_inf["lost"] == 1
    assert star_inf["value_won"] == 200000.0
    assert star_inf["win_rate"] == round(2 / 3, 3)
    assert star_inf["roles"] == ["architect"]
    assert set(star_inf["building_types"]) == {"Bürogebäude", "Villen"}

    # the duplicate-role project counts once
    quiet_inf = prescriptors.influence_for("BBBB2222-0000-0000-0000-000000000002")
    assert quiet_inf["projects"] == 1
    assert sorted(quiet_inf["roles"]) == ["architect", "end_customer"]
    # nothing decided yet -> win_rate is None, NOT 0. An untested architect must
    # not rank below a real 10% performer.
    assert quiet_inf["win_rate"] is None

    # a company with no projects gets the empty shape, never a KeyError
    assert prescriptors.influence_for(None)["projects"] == 0
    assert prescriptors.influence_for("does-not-exist")["projects"] == 0

    # the ranking surfaces the architect with zero revenue ABOVE the paying
    # dealer, which a revenue-only list can never do
    ranked = prescriptors.prescriptor_targets()
    assert ranked[0]["company_id"] == star_id
    assert ranked[0]["revenue_y0"] in (0, 0.0, None)
    ids = [r["company_id"] for r in ranked]
    assert quiet_id in ids

    ov = prescriptors.overview()
    assert ov["opportunities"] == 4 and ov["usable"] is True
    assert ov["by_segment"]["Architekten"]["with_projects"] == 2

def test_crm_fetch_sends_select_as_a_string_and_reads_labels(temp_db, monkeypatch):
    """Two things verified against the live flow, pinned here.

    $select must be a COMMA-SEPARATED STRING — Power Automate passes it straight
    into the Dataverse query, and a JSON array silently returns no columns.

    And picklists arrive as an integer PLUS a FormattedValue label, so the app
    needs no option-set mapping. Confirmed live: 102 / "Architekten"."""
    from sqlalchemy import select
    from adwatch import crm_accounts, flows
    from adwatch.models import Company

    sent = {}

    def fake_post(role, payload, **kw):
        sent["role"] = role
        sent["payload"] = payload
        return {"value": [{
            "accountid": "11111111-1111-1111-1111-111111111111",
            "name": "Interessent GmbH",
            "modifiedon": "2026-08-05T12:00:00Z",
            "sl_customer_segment": 102,
            "sl_customer_segment@OData.Community.Display.V1.FormattedValue": "Architekten",
            "sl_customer_or_prospect": 102690000,
            "statecode": 0,
        }]}
    monkeypatch.setattr(flows, "post", fake_post)

    rows = crm_accounts.fetch_accounts("statecode eq 0", top=3)
    assert sent["role"] == "crm_query"
    assert isinstance(sent["payload"]["select"], str), "$select must be a string"
    assert "accountid" in sent["payload"]["select"].split(",")
    assert sent["payload"]["top"] == 3
    assert len(rows) == 1

    res = crm_accounts.upsert_accounts(rows)
    assert res["inserted"] == 1
    with temp_db.SessionLocal() as s:
        c = s.scalar(select(Company).where(Company.name == "Interessent GmbH"))
        assert c.segment == "Architekten"        # the LABEL, not 102

    # a scope must never run without a filter, or it would pull the whole org
    with pytest.raises(ValueError):
        crm_accounts.load_scope("")
    # the prospect scope is the one the ICP needs — keep it addressable by name
    assert "prospects" in crm_accounts.SCOPES
    assert "102690000" in crm_accounts.SCOPES["prospects"][1]

    # delta uses our newest modifiedon as the watermark
    out = crm_accounts.sync_delta()
    assert "modifiedon gt" in (out["filter"] or "")

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
            "segment": {"coverage": 1.0, "shares": {"Handel": 0.7, "Verarbeiter": 0.3},
                        "lifts": {"Handel": 1.8, "Verarbeiter": 0.6}},
            # the live case: a spread that LOOKS informative but rests on ~20 rows
            "size_bucket": {"coverage": 0.03,
                            "shares": {"20-49": 0.5, "10-19": 0.3, "50+": 0.2},
                            "lifts": {"20-49": 2.5, "10-19": 1.1, "50+": 0.7}},
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

def test_relevance_sorts_by_rank_not_alphabet(temp_db):
    """Solarlux-Relevanz is ordinal, and its labels sort alphabetically in exactly
    the wrong order — g(ering) < h(och) < m(ittel). A plain text sort would head
    the "best architects first" list with the worst-fitting offices, so the sort
    ranks the labels and leaves ungraded rows at the bottom either way."""
    from sqlalchemy import select
    from adwatch.customers import _apply_sort
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    s.add_all([
        Company(name="Villa-Buero", segment="Architekten", solarlux_relevance="hoch"),
        Company(name="Innenausbau", segment="Architekten", solarlux_relevance="gering"),
        Company(name="Hochbau", segment="Architekten", solarlux_relevance="mittel"),
        Company(name="Ungeprueft", segment="Architekten", solarlux_relevance=None),
    ])
    s.commit(); s.close()

    def order(direction):
        with temp_db.SessionLocal() as s2:
            stmt = _apply_sort(select(Company), "solarlux_relevance", direction)
            return [c.name for c in s2.scalars(stmt)]

    assert order("desc") == ["Villa-Buero", "Hochbau", "Innenausbau", "Ungeprueft"]
    assert order("asc") == ["Innenausbau", "Hochbau", "Villa-Buero", "Ungeprueft"]

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

def test_icp_scores_propensity_not_popularity(temp_db):
    """The heart, and the correction that made it work.

    Scoring used to reward the winners' most COMMON value. That ranks popularity,
    not propensity: measured live, Bauelementehandel is 36.5% of winners but
    converts at 1.03x, while Wintergartenbau is ~1% of winners and converts at
    1.62x — share ordered them backwards. fit_for now scores LIFT, so a value is
    rewarded for being over-represented among winners RELATIVE to the population.

    Fixture: Tischler is the common trade (60 of 90 companies) but converts
    poorly; Metallbau is rarer but converts well. Share-based scoring would rank
    Tischler top; lift must rank Metallbau top.
    """
    from adwatch import customers
    from adwatch.insights import icp
    from adwatch.models import Company
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    win_ids = []
    # 20 Metallbau winners out of 30 Metallbau companies  -> strongly over-represented
    for i in range(30):
        c = Company(name=f"Metall {i}", country="DE", segment="Verarbeiter",
                    sub_segment="Metallbau-Schlosser", sales_channel="Fachhandelsvertrieb",
                    postal_code="49134")
        s.add(c); s.flush()
        if i < 20:
            win_ids.append(c.id)
    # 10 Tischler winners out of 60 Tischler companies -> the COMMON trade, but
    # under-represented among winners
    for i in range(60):
        c = Company(name=f"Tisch {i}", country="DE", segment="Verarbeiter",
                    sub_segment="Tischler", sales_channel="Fachhandelsvertrieb",
                    postal_code="80331")
        s.add(c); s.flush()
        if i < 10:
            win_ids.append(c.id)
    blank = Company(name="K Leer", country="DE")
    s.add(blank); s.commit()
    bl_id = blank.id
    # a non-winner of each trade, to compare
    metall_id = s.scalars(select(Company).where(
        Company.name == "Metall 29")).one().id
    tisch_id = s.scalars(select(Company).where(
        Company.name == "Tisch 59")).one().id
    s.close()

    p = icp.build_profile({"ids": win_ids})
    assert p["winners_count"] == 30
    subs = p["features"]["sub_segment"]
    # Tischler is the LARGER share of winners' trade population but Metallbau is
    # the over-represented one — this is exactly the inversion that broke ranking
    assert dict(subs["shares"])["Metallbau-Schlosser"] == pytest.approx(20 / 30)
    assert subs["lifts"]["Metallbau-Schlosser"] > subs["lifts"]["Tischler"]
    assert subs["lifts"]["Metallbau-Schlosser"] > 1.0 > subs["lifts"]["Tischler"]

    res = icp.apply_profile({"ids": win_ids}, name="test")
    assert res["companies_scored"] >= 90

    s = temp_db.SessionLocal()
    me, ti, bl = (s.get(Company, metall_id), s.get(Company, tisch_id),
                  s.get(Company, bl_id))
    assert me.fit_score > 50 > ti.fit_score, (me.fit_score, ti.fit_score)
    assert bl.fit_score is None and bl.target_score is None   # nothing comparable -> unrated, not 0
    feats = {f["feature"] for f in me.fit_breakdown["features"]}
    assert "sales_channel" not in feats               # 100%-uniform -> excluded
    assert "sub_segment" in feats
    s.close()

def test_availability_leakage_is_detected_and_excluded(temp_db):
    """A feature known for winners far more often than for the population is
    measuring 'we already engaged this account', not fit. Live cases: products
    (13.4x), Betriebsgröße (13.7x), Firmenalter (11.9x), Anzeigen-Aktivität
    (9.6x) — all only exist for the enriched/monitored base, which WAS the old
    buyers-only export. Scoring on them yields a confident model that says
    'accounts we already sell to, buy from us'."""
    from adwatch.insights import icp
    from adwatch.models import Company
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    win_ids = []
    for i in range(40):
        # winners are enriched (products known)
        c = Company(name=f"Gewinner {i}", country="DE", segment="Handel",
                    sub_segment="Bauelementehandel", postal_code="49134",
                    products=["Fenster"])
        s.add(c); s.flush(); win_ids.append(c.id)
    for i in range(160):
        # the population is not enriched at all
        s.add(Company(name=f"Rest {i}", country="DE", segment="Handel",
                      sub_segment="Bauelementehandel", postal_code="49134"))
    s.commit(); s.close()

    p = icp.build_profile({"ids": win_ids})
    prod = p["features"]["products"]
    assert prod["coverage"] == 1.0
    assert prod["pop_coverage"] < 0.3
    assert prod["leaky"] is True, prod
    assert p["features"]["sub_segment"]["leaky"] is False

    # and the leaky feature must not contribute to any score
    fit, bd = icp.fit_for({"products": ["Fenster"], "sub_segment": "Bauelementehandel",
                           "segment": "Handel"}, p)
    assert "products" not in {b["feature"] for b in bd}

def test_cadence_is_measured_not_assumed():
    """A dealer ordering every 14 days that has been quiet 120 days is overdue;
    a Wohnungswirtschaft ordering yearly at 120 days is not. A fixed 12-month
    cutoff cannot tell these apart, which is the whole point of the module."""
    from adwatch.insights import rfm
    import datetime as dt
    today = dt.date(2026, 8, 5)

    fortnightly = _ev(("2025-06-01", 5000), ("2025-06-15", 5000),
                      ("2025-06-29", 5000), ("2026-04-01", 5000))
    r = rfm.classify(fortnightly, today)
    assert r["cadence_days"] == 14
    assert r["health"] in ("gefährdet", "verloren")
    assert r["overdue_factor"] > 3

    yearly = _ev(("2022-01-10", 90000), ("2023-01-20", 90000),
                 ("2024-02-01", 90000), ("2025-06-01", 90000))
    r2 = rfm.classify(yearly, today)
    assert r2["cadence_days"] >= 365
    assert r2["health"] == "aktiv", r2

def test_spare_parts_only_is_not_a_system_customer():
    """~25% of Belege are 0 EUR and the median is EUR 194. Without a materiality
    floor a gasket order makes a company look like a customer and poisons any ICP
    trained on 'buyers'."""
    from adwatch.insights import rfm
    import datetime as dt
    today = dt.date(2026, 8, 5)
    trivial = _ev(("2026-01-05", 0), ("2026-02-05", 120), ("2026-03-05", 80),
                  ("2026-04-05", 300))
    assert rfm.classify(trivial, today)["health"] == "einmalig"
    # the same monthly rhythm, but material and still current, is a live customer
    real = _ev(("2026-04-05", 9000), ("2026-05-05", 9000), ("2026-06-05", 9000),
               ("2026-07-05", 9000))
    assert rfm.classify(real, today)["health"] == "aktiv"

def test_no_events_is_never_not_lost():
    from adwatch.insights import rfm
    r = rfm.classify([])
    assert r["health"] == "nie" and r["value"] == 0.0
    # and it must not produce a win-back rank — there is nothing to win back
    assert rfm.winback_score(r, 0.0) == 0.0

def test_winback_ad_signal_is_a_multiplier_and_value_is_log_scaled():
    """50% of revenue sits with 66 companies, so a linear value term would make
    the list nothing but whales; and an advertising lapsed customer must outrank
    an equally-valuable silent one."""
    from adwatch.insights import rfm
    import datetime as dt
    evs = _ev(("2024-01-05", 60000), ("2024-03-05", 60000),
              ("2024-05-05", 60000), ("2024-07-05", 60000))
    cls = rfm.classify(evs, dt.date(2026, 8, 5))
    quiet = rfm.winback_score(cls, cls["value"])
    ads = rfm.winback_score(cls, cls["value"], advertising=True)
    assert ads > quiet > 0
    # log scaling: 100x the revenue must not give anything like 100x the score
    big = rfm.winback_score(cls, cls["value"] * 100)
    assert big < quiet * 2

def test_crm_import_refuses_a_truncated_download(tmp_path):
    """A partial download must not be mistaken for the full population — it would
    look like thousands of accounts had vanished."""
    import json, pytest
    from adwatch import crm_import
    p = tmp_path / "part.json"
    p.write_text(json.dumps({"cols": ["crm_id", "name"], "rows": [["a", "X"]]}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="partial"):
        crm_import.load_export(p)

def test_crm_import_never_writes_local_owned_columns():
    """Enrichment, scores and linked ad identities survive a full re-import."""
    from adwatch.crm_accounts import LOCAL_OWNED
    from adwatch.crm_import import WRITES
    assert not (WRITES & LOCAL_OWNED), sorted(WRITES & LOCAL_OWNED)

def test_bulk_imported_companies_are_not_monitored(temp_db):
    """46,000 CRM accounts must feed the ICP without flooding the ad pipeline."""
    import json
    from sqlalchemy import select
    from adwatch import crm_import
    from adwatch.models import Company
    rows = [[f"guid-{i}", f"Firma {i}", "", 101, 101000, 102690001, 102690000,
             "Deutschland", "49", "Ort", "", None, "2024-01-01",
             0, 0, "", "", 0, 0, 0, 0, None, 0, 0, 0] for i in range(600)]
    cols = ["crm_id", "name", "accountnumber", "segment", "sub_segment",
            "sales_channel", "kunde_interessent", "country", "postal_code",
            "city", "website", "employees", "created_on", "beleg_count",
            "beleg_sum", "beleg_first", "beleg_last", "rev_2023", "rev_2024",
            "rev_2025", "rev_2026", "avg_discount", "arch_projects",
            "arch_won", "arch_won_value"]
    p = temp_db.config.DATA_DIR if hasattr(temp_db, "config") else None
    import tempfile, pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "e.json"
    f.write_text(json.dumps({"cols": cols, "rows": rows}), encoding="utf-8")
    stats = crm_import.import_accounts(f)
    assert stats["inserted"] == 600
    with temp_db.SessionLocal() as s:
        assert s.scalars(select(Company).where(Company.monitored.is_(False))).all()
        c = s.scalars(select(Company).where(Company.crm_id == "guid-7")).one()
        assert c.segment == "Verarbeiter" and c.sub_segment == "Fensterbau"
        assert c.monitored is False

def test_market_list_repairs_semicolon_shifted_names(tmp_path):
    """The Spanish legal-form comma arrived as a semicolon, shifting 46 of 534 real
    rows. Unrepaired, 'S.L.' becomes the company TYPE and every later column lands
    in the wrong field."""
    from adwatch import market_list as ml
    out = ml.parse(_write_markt(tmp_path))
    names = {r["name"]: r for r in out["records"]}
    assert "CARPYVENT, S.L." in names, sorted(names)
    assert names["CARPYVENT, S.L."]["import_type"] == "potenzialkunde"
    assert names["CARPYVENT, S.L."]["city"] == "Alicante"
    assert names["CARPYVENT, S.L."]["postal_code"] == "03001"
    assert out["stats"]["unparsable"] == 0

def test_market_list_separates_competitors_from_conquest_targets(tmp_path):
    """'wettbewerber' means two opposite things. A manufacturer's OWN location is
    never a target; a firm that merely INSTALLS a rival's systems is the best
    target in the file — and must stay recognisable as having arrived tagged
    'wettbewerber'."""
    from adwatch import market_list as ml
    recs = {r["name"]: r for r in ml.parse(_write_markt(tmp_path))["records"]}

    schueco = recs["Schueco Showroom Madrid"]
    assert schueco["is_competitor"] is True
    assert schueco["carries_competitor_brand"] is False

    premial = recs["Premial"]
    assert premial["is_competitor"] is False, "installs Schueco != is Schueco"
    assert premial["carries_competitor_brand"] is True
    assert premial["import_type"] == "wettbewerber", "origin must stay auditable"
    assert premial["segment"] == "Verarbeiter"

def test_market_list_dedupes_and_keeps_the_discarded_type(tmp_path):
    from adwatch import market_list as ml
    out = ml.parse(_write_markt(tmp_path))
    assert out["stats"]["duplicates_removed"] == 1
    premial = next(r for r in out["records"] if r["name"] == "Premial")
    # the same firm was entered twice under different Typ values — the one we
    # dropped is remembered rather than silently lost
    assert premial["also_imported_as"] == ["potenzialkunde"]

def test_market_list_matches_existing_customer_by_kdnr_not_name(temp_db, tmp_path):
    """Names do not join: 'IBZ Cristal' is 'IBZ Cortinas De Cristal SL'. Only 7 of
    534 rows matched by name, 30 matched on the Kd-Nr buried in free text."""
    from sqlalchemy import select
    from adwatch import market_list as ml
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    s.add(Company(name="IBZ Cortinas De Cristal SL", country="ES",
                  sap_number="0005164611", segment="Verarbeiter"))
    s.commit(); s.close()

    stats = ml.import_list(_write_markt(tmp_path), lead_source="test_es")
    assert stats["matched_by"]["customer_number"] == 1
    with temp_db.SessionLocal() as s:
        c = s.scalars(select(Company).where(
            Company.name == "IBZ Cortinas De Cristal SL")).one()
        # CRM master data untouched; the research is appended to notes
        assert c.segment == "Verarbeiter"
        assert "Kd-Nr. 5164611" in (c.notes or "")
        assert "test_es" in (c.notes or "")
        # and it must NOT have been inserted a second time
        assert not s.scalars(select(Company).where(
            Company.name == "IBZ Cristal")).all()

def test_dossier_separates_roles_and_synthesises_profile(temp_db):
    """The FBI file: every VC in every ROLE the company plays, never mixed —
    an architect's 'lost' VC is not a lost sale — plus a Kurzprofil whose every
    clause traces to a column (deterministic, no LLM per view)."""
    import datetime as _dt
    from adwatch import dossier
    from adwatch.models import Company, CrmOpportunity, CrmOrderEvent

    s = temp_db.SessionLocal()
    c = Company(name="Muster Bau GmbH", country="DE", city="Osnabrück",
                crm_id="GUID-1", segment="Handel", sub_segment="Bauelementehandel",
                positioning="premium", own_fabrication=True,
                quote_sum=100000, conversion_rate=0.25)
    s.add(c); s.flush()
    s.add(CrmOrderEvent(company_id=c.id, order_date=_dt.date(2024, 3, 1),
                        amount=50000, beleg_count=2))
    # as buyer: one won VC with invoiced value and SAP trace
    s.add(CrmOpportunity(crm_id="1", number="1", parent_account_crm_id="guid-1",
                         state="gewonnen", order_value=40000, invoiced_value=38000,
                         sap_order_numbers=["4711"], type_of_use="Wohnen",
                         origin="vom Händler", project_id="p1",
                         opportunity_guid="v1",
                         created_on=_dt.datetime(2024, 1, 1)))
    # as architect: a lost VC — must land in the ARCHITECT block, not the buyer's
    s.add(CrmOpportunity(crm_id="2", number="2", architect_crm_id="guid-1",
                         state="verloren", lost_reason="Zu teuer",
                         project_id="p2", opportunity_guid="v2",
                         created_on=_dt.datetime(2024, 2, 1)))
    s.commit(); cid = c.id; s.close()

    d = dossier.build(cid)
    assert d["rollen"]["kaeufer"]["won"] == 1
    assert d["rollen"]["kaeufer"]["invoiced_value"] == 38000
    assert d["rollen"]["kaeufer"]["recent"][0]["sap_orders"] == ["4711"]
    assert d["rollen"]["architekt"]["lost"] == 1
    assert "kaeufer" in d["rollen"] and d["rollen"]["architekt"]["vcs"] == 1
    assert len(d["projekte"]) == 2
    kp = d["kurzprofil"]
    assert "Bauelementehandel" in kp and "Osnabrück" in kp
    assert "eigene Fertigung" in kp and "Konversion 25%" in kp

def test_dossier_project_outcome_uses_the_one_win_rule(temp_db):
    """A project with one win and one sibling loss is a WON project in the
    dossier's Objekte list, per the Objektvertrieb rule."""
    import datetime as _dt
    from adwatch import dossier
    from adwatch.models import Company, CrmOpportunity

    s = temp_db.SessionLocal()
    c = Company(name="GU Beispiel", country="DE", crm_id="GUID-9",
                segment="Baudienstleister")
    s.add(c); s.flush()
    s.add(CrmOpportunity(crm_id="10", number="10", parent_account_crm_id="guid-9",
                         state="verloren", lost_reason="Zugehörige VC gewonnen",
                         project_id="prj", opportunity_guid="prj",
                         project_name="Objekt Musterstraße",
                         created_on=_dt.datetime(2024, 5, 1)))
    s.add(CrmOpportunity(crm_id="11", number="11",
                         parent_account_crm_id="someone-else",
                         state="gewonnen", order_value=90000,
                         project_id="prj", opportunity_guid="v11",
                         created_on=_dt.datetime(2024, 5, 2)))
    s.commit(); cid = c.id; s.close()

    d = dossier.build(cid)
    prj = next(p for p in d["projekte"] if p["project_id"] == "prj")
    assert prj["status"] == "gewonnen", "one win makes the project won"
    assert prj["members"] == 2

def test_objekt_detail_assembles_the_whole_project(temp_db, monkeypatch):
    """An Objekt has no record of its own — it is a GROUP of Verkaufschancen
    sharing sl_primary_opportunityid, so the drawer has to assemble it. Two
    things this must get right, both of which it got wrong first:

    * the group key falls back to the opportunity guid when a VC has no project
      id, so matching only project_id 404s on every single-VC project;
    * a firm that is Käufer, Architekt AND Endkunde on one deal is on ONE deal.
      Counting role occurrences reported 9 VCs on a 4-VC project.
    """
    from adwatch.insights import projekte
    from adwatch.models import Company, CrmOpportunity
    import datetime as _dt

    s = temp_db.SessionLocal()
    buyer = Company(name="Metallbau A", segment="Verarbeiter", crm_id="aaa", city="Wien")
    allrole = Company(name="Generalunternehmer B", segment="Baudienstleister", crm_id="bbb")
    s.add_all([buyer, allrole]); s.commit()
    s.add_all([
        CrmOpportunity(crm_id="v1", opportunity_guid="g1", project_id="P1",
                       project_name="Muthgasse 109", state="verloren",
                       lost_reason="Zugehörige VC gewonnen",
                       parent_account_crm_id="aaa", city="Wien", postal_code="1190",
                       street="Muthgasse 109", created_on=_dt.datetime(2024, 7, 15)),
        # one firm in all three roles on a single deal
        CrmOpportunity(crm_id="v2", opportunity_guid="g2", project_id="P1",
                       project_name="Muthgasse 109", state="verloren",
                       lost_reason="Zu teuer", parent_account_crm_id="bbb",
                       architect_crm_id="bbb", end_customer_crm_id="bbb",
                       created_on=_dt.datetime(2024, 9, 4)),
        # a project of ONE with no project_id — keyed by its own guid
        CrmOpportunity(crm_id="v3", opportunity_guid="g3", project_id=None,
                       project_name="Einzelobjekt", state="gewonnen",
                       order_value=1000.0, parent_account_crm_id="aaa"),
    ])
    s.commit(); s.close()
    monkeypatch.setattr(projekte, "SessionLocal", temp_db.SessionLocal)

    d = projekte.detail("P1")
    assert d["members"] == 2 and d["status"] == "gewonnen"
    # won through a sibling outside the window: say so, or "gewonnen · 0 gewonnene
    # VCs · kein Wert" reads like a bug
    assert d["won_members"] == 0 and d["won_via"]
    assert d["address"] == "Muthgasse 109 1190 Wien"
    byname = {f["name"]: f for f in d["firms"]}
    assert byname["Generalunternehmer B"]["roles"] == ["architekt", "endkunde", "kaeufer"]
    assert byname["Generalunternehmer B"]["vcs"] == 1          # one deal, not three
    assert [t["state"] for t in d["timeline"]] == ["verloren", "verloren"]  # oldest first
    assert "Zu teuer" in d["lost_reasons"]
    assert "Zugehörige VC gewonnen" not in d["lost_reasons"]

    # a single-VC project is reachable by its guid
    solo = projekte.detail("g3")
    assert solo is not None and solo["members"] == 1 and solo["won_members"] == 1
    assert projekte.detail("gibtsnicht") is None

def test_objekte_filter_by_number_of_verkaufschancen(temp_db, monkeypatch):
    """How many VCs hang on an Objekt is the strongest project-level signal we
    have (39,0 % against 19,3 %), so it has to be filterable in both directions.

    Two things that must not slip: a closed range excludes above as well as
    below, and the bucket table stays over ALL Objekte no matter what the table
    is filtered to — otherwise the reference row mirrors the filter and the
    comparison it exists to make disappears.
    """
    from adwatch.insights import projekte
    from adwatch.models import CrmOpportunity

    s = temp_db.SessionLocal()
    rows = []
    # P1: 1 VC, lost. P2: 2 VCs, won. P3: 3 VCs, lost.
    plan = [("P1", 1, "verloren"), ("P2", 2, "gewonnen"), ("P3", 3, "verloren")]
    for pid, n, state in plan:
        for i in range(n):
            rows.append(CrmOpportunity(
                crm_id=f"{pid}-{i}", opportunity_guid=f"{pid}-{i}", project_id=pid,
                project_name=pid, state=(state if i == 0 else "verloren"),
                order_value=(100.0 if state == "gewonnen" and i == 0 else None)))
    s.add_all(rows); s.commit(); s.close()
    monkeypatch.setattr(projekte, "SessionLocal", temp_db.SessionLocal)
    projekte.invalidate_cache()

    assert {r["name"] for r in projekte.list_projects()["rows"]} == {"P1", "P2", "P3"}
    assert {r["name"] for r in projekte.list_projects(min_members=2)["rows"]} == {"P2", "P3"}
    # closed range: excludes the 3-VC project ABOVE it, not just the 1 below
    exact2 = projekte.list_projects(min_members=2, max_members=2)
    assert {r["name"] for r in exact2["rows"]} == {"P2"} and exact2["total"] == 1
    assert {r["name"] for r in projekte.list_projects(max_members=1)["rows"]} == {"P1"}

    o = projekte.overview(min_members=2, max_members=2)
    assert o["projects"] == 1                      # the filtered population
    b = {x["label"]: x for x in o["member_buckets"]}
    assert [b["1 VC"]["projects"], b["2 VCs"]["projects"], b["3–4 VCs"]["projects"]] == [1, 1, 1]
    assert b["2 VCs"]["win_rate"] == 1.0 and b["1 VC"]["win_rate"] == 0.0
    assert sum(x["projects"] for x in o["member_buckets"]) == o["all_projects"] == 3

def test_dossier_carries_the_product_profile(temp_db, monkeypatch):
    """Everything pulled today landed in the database and none of it reached the
    drawer. The product profile is the whole answer to "which product for whom",
    so it has to travel with the dossier, and its euros must stay labelled as
    QUOTED — they span won and lost deals and are not revenue."""
    from adwatch import dossier
    from adwatch.models import Company, CrmCompanyProduct
    import datetime as _dt

    s = temp_db.SessionLocal()
    c = Company(name="Testbau", segment="Handel")
    s.add(c); s.commit()
    s.add_all([
        CrmCompanyProduct(company_id=c.id, family="cero", positions=9,
                          value=161845.0, first_seen=_dt.date(2021, 4, 4),
                          last_seen=_dt.date(2026, 3, 2)),
        # euros without positions: the value comes through the opportunity link,
        # the positions through the account link — they do not have to agree
        CrmCompanyProduct(company_id=c.id, family="Horizontale-Schiebewand",
                          positions=0, value=41000.0),
    ])
    s.commit(); cid = c.id; s.close()
    monkeypatch.setattr(dossier, "SessionLocal", temp_db.SessionLocal)

    d = dossier.build(cid)
    p = d["produkte"]
    assert [f["family"] for f in p["families"]] == ["cero", "Horizontale-Schiebewand"]
    assert p["value_quoted"] == 202845.0
    assert p["positions"] == 9
    assert p["first"] == "2021-04-04" and p["last"] == "2026-03-02"
    # a company with no product rows must not grow an empty block
    s2 = temp_db.SessionLocal()
    other = Company(name="Ohne Produkte", segment="Handel")
    s2.add(other); s2.commit(); oid = other.id; s2.close()
    assert dossier.build(oid)["produkte"] is None

def test_unproven_website_keeps_no_facts(temp_db, monkeypatch):
    """Facts and the identity verdict are written in one run, so they agree —
    until a verdict is REVISED. D3 Outdoor Girona kept a full profile (products,
    Corradi as an installed brand) read off a site the checker had already ruled
    was not theirs. A description with no website at all sat on 15 more rows."""
    from adwatch import dataquality as dq
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    s.add_all([
        Company(name="Demoted", segment="Handel", identity_status="conflict",
                website_domain="fremd.de", description="von der falschen Seite",
                products=["Fenster"], competitor_brands=["Corradi"],
                enrichment_status="enriched"),
        Company(name="Ohne Website", segment="Handel", identity_status="not_found",
                description="woher auch immer", enrichment_status="enriched"),
        Company(name="Sauber", segment="Handel", identity_status="verified",
                website_domain="echt.de", description="belegt",
                products=["Wintergarten"], enrichment_status="enriched"),
    ])
    s.commit(); s.close()
    monkeypatch.setattr(dq, "SessionLocal", temp_db.SessionLocal)

    assert dq.clear_unbacked_enrichment(apply=False)["rows"] == 2   # dry run changes nothing
    dq.clear_unbacked_enrichment(apply=True)

    with temp_db.SessionLocal() as s2:
        rows = {c.name: c for c in s2.query(Company).all()}
        assert rows["Demoted"].description is None
        assert rows["Demoted"].competitor_brands is None
        assert rows["Demoted"].enrichment_status == "none"
        # the domain and the verdict STAY — they are the evidence the check ran
        assert rows["Demoted"].website_domain == "fremd.de"
        assert rows["Demoted"].identity_status == "conflict"
        # a verified row is untouched
        assert rows["Sauber"].description == "belegt"
    # idempotent
    assert dq.clear_unbacked_enrichment(apply=False)["rows"] == 0

def test_own_group_is_excluded_even_when_the_filter_names_ids(temp_db):
    """The intercompany guard used to be skipped whenever the winners filter
    carried `ids` — which is precisely the DEFAULT path, because
    material_buyer_ids() returns ids. Measured on the real base: 7 of 8 flagged
    own-group companies were in the default winners set. The existing
    intercompany test missed it because that DB has no Belege and therefore fell
    back to the id-less customer_state filter.

    An id list is a choice of POPULATION. It is never consent to train the
    profile on our own subsidiaries."""
    import datetime as dt
    from adwatch import customers
    from adwatch.insights import icp
    from adwatch.models import Company, CrmOrderEvent
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    for i in range(30):
        s.add(Company(name=f"Haendler {i}", country="DE",
                      segment="Handel" if i < 20 else "Verarbeiter",
                      postal_code="49134" if i < 20 else "80331"))
    s.add(Company(name="Linara Teststadt GmbH", country="DE", segment="Handel",
                  postal_code="49134"))
    s.commit()
    # every one of them a MATERIAL buyer, so material_buyer_ids() picks the
    # id-carrying path — the one that used to bypass the guard
    for c in s.scalars(select(Company)):
        s.add(CrmOrderEvent(company_id=c.id, order_date=dt.date(2025, 3, 1),
                            amount=50_000.0))
    s.commit()
    s.close()

    assert customers.flag_intercompany() == 1
    ids = icp.material_buyer_ids()
    assert len(ids) == 31, "alle 31 sind materielle Kaeufer - sonst testet das hier nichts"

    p = icp.build_profile(None)                    # the default path, uses ids
    assert p["winners_count"] == 30, "die eigene Gesellschaft sitzt im Gewinner-Set"
    assert p["winners_filter"].get("ids"), "der Default muss weiterhin ueber ids laufen"

    # and explicitly, too: naming the ids by hand must not smuggle them back in
    p2 = icp.build_profile({"ids": ids})
    assert p2["winners_count"] == 30

def test_out_of_scope_rows_never_carry_a_ranking(temp_db):
    """Private Endkunden had a winback_score on 1.449 rows and a fit_score on all
    1.665, because rfm.recompute() iterated select(Company) with no scope filter
    and nothing ever cleared scores written before the scope rule existed.
    overdue_customers() filtered them out of the VIEW, which is exactly what kept
    it invisible — any direct read of the column still got consumers.

    `health` is the deliberate exception: it is a fact about the row, not a
    position in a call list."""
    import datetime as dt
    from adwatch import dataquality as dq
    from adwatch.insights import rfm
    from adwatch.models import Company, CrmOrderEvent
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    for r in (Company(name="Echter Haendler", segment="Handel"),
              Company(name="Herr Privat", segment="Private Endkunden"),
              Company(name="Schueco Niederlassung", segment="Handel", is_competitor=True),
              Company(name="Linara Teststadt GmbH", segment="Handel", is_intercompany=True)):
        s.add(r)
    s.commit()
    for c in s.scalars(select(Company)):
        for yr in (2019, 2020, 2021, 2022):        # a cadence, then silence
            s.add(CrmOrderEvent(company_id=c.id, order_date=dt.date(yr, 3, 1),
                                amount=90_000.0))
        c.fit_score = 55.0
        c.target_score = 55.0
    s.commit()
    s.close()

    rfm.recompute(today=dt.date(2026, 8, 10))
    dq.clear_out_of_scope_scores(apply=True)

    s = temp_db.SessionLocal()
    by = {c.name: c for c in s.scalars(select(Company))}
    assert by["Echter Haendler"].winback_score > 0
    for name in ("Herr Privat", "Schueco Niederlassung", "Linara Teststadt GmbH"):
        assert by[name].winback_score is None, f"{name} steht auf einer Rueckgewinnungsliste"
        assert by[name].target_score is None, f"{name} steht auf der Zielliste"
        assert by[name].health is not None, f"{name} hat seine Historie verloren"
    # out of the business entirely -> nothing descriptive either; own group -> kept
    assert by["Herr Privat"].fit_score is None
    assert by["Linara Teststadt GmbH"].fit_score == 55.0
    s.close()

    assert dq.clear_out_of_scope_scores(apply=True)["rows"] == 0   # idempotent

def test_account_import_reflags_own_group(temp_db):
    """flag_intercompany() promised in its own docstring to run "on every import"
    and was called by nothing outside a test. Fourteen own-group companies were
    therefore unflagged — Nana Wall Systems Inc. (EUR 39,7 Mio) and Solarlux
    Nederland B.V. (EUR 21,9 Mio, 98% of all Dutch revenue) among them."""
    from adwatch import crm_accounts
    from adwatch.models import Company
    from sqlalchemy import select

    res = crm_accounts.upsert_accounts([
        {"accountid": "aaaaaaaa-0000-0000-0000-000000000001",
         "name": "Solarlux Nederland B.V."},
        {"accountid": "aaaaaaaa-0000-0000-0000-000000000002",
         "name": "Nana Wall Systems Inc."},
        {"accountid": "aaaaaaaa-0000-0000-0000-000000000003",
         "name": "Serin Bauelemente"},
    ])
    assert res["intercompany_reflagged"] == 2

    s = temp_db.SessionLocal()
    flags = {c.name: c.is_intercompany for c in s.scalars(select(Company))}
    s.close()
    assert flags["Solarlux Nederland B.V."] is True
    assert flags["Nana Wall Systems Inc."] is True
    assert flags["Serin Bauelemente"] is False

def test_executing_architect_is_not_always_an_architect():
    """`architect_crm_id` mirrors slx_executingarchitect_accountid — the
    AUSFUEHRENDER Architekt. A dealer that plans in-house enters itself, and on
    the real base that is 4.447 of 7.331 filled values (60,7%). Reading the field
    as "an architecture practice is involved" overstates it 2,5-fold: third-party
    architects appear on 2.884 of 57.776 Verkaufschancen (5,0%), not 12,7%."""
    from adwatch.insights.projekte import specifying_architect
    from adwatch.models import CrmOpportunity

    dealer, buero = "GUID-HAENDLER", "GUID-BUERO"
    assert specifying_architect(CrmOpportunity(
        crm_id="1", parent_account_crm_id=dealer, architect_crm_id=dealer)) is None
    assert specifying_architect(CrmOpportunity(
        crm_id="2", parent_account_crm_id=dealer, architect_crm_id=buero)) == buero
    assert specifying_architect(CrmOpportunity(
        crm_id="3", parent_account_crm_id=dealer, architect_crm_id=None)) is None
    # different casing must not create a phantom architect
    assert specifying_architect(CrmOpportunity(
        crm_id="4", parent_account_crm_id=dealer.lower(),
        architect_crm_id=dealer.upper())) is None

def test_diagnose_compares_winners_against_a_real_baseline(temp_db):
    """diagnose() built its baseline by stripping `customer_state` from the
    winners filter. That was correct while the default winners set WAS
    customer_state — but it moved to {"ids": material_buyer_ids()} when the
    Belege became the source, so nothing got stripped, population == winners,
    every separation was 0.000 and the verdict was always "Kein Merkmal trennt
    die Gewinner von der Grundgesamtheit". Measured on the real base:
    diagnose(None) reported 3.781 winners against 3.781 population.

    A market slice (country/segment) must therefore compare the BUYERS in the
    slice against the slice, not the slice against itself."""
    import datetime as dt
    from adwatch.insights import icp
    from adwatch.models import Company, CrmOrderEvent
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    for i in range(60):
        s.add(Company(name=f"Haendler {i}", country="DE", segment="Handel",
                      sales_channel="Fachhandelsvertrieb",
                      # the buyers sit in one PLZ zone, the rest in another, so
                      # there IS something to find if the baseline is right
                      postal_code="49134" if i < 30 else "80331"))
    s.commit()
    for i, c in enumerate(s.scalars(select(Company).order_by(Company.id))):
        if i < 30:
            s.add(CrmOrderEvent(company_id=c.id, order_date=dt.date(2024, 3, 1),
                                amount=50_000.0))
    s.commit()
    s.close()

    d = icp.diagnose({"country": ["DE"], "segment": ["Handel"]})
    assert d["population"] == 60
    assert d["winners"] == 30, "die Kaeufer im Filter, nicht der ganze Filter"
    assert d["population"] > d["winners"], "Grundgesamtheit darf nicht die Gewinnermenge sein"
    plz = next(f for f in d["features"] if f["feature"] == "plz_zone")
    assert plz["separation"] > 0.4, f"PLZ trennt hier perfekt, gemessen {plz['separation']}"

def test_crm_product_families_are_time_gated(temp_db):
    """`crm_company_products` is the best-covered feature available (23.431
    companies against 1.207 for the website-derived list) — and it is written
    BY buying. Measured on the real base: between a cut two years back and today
    the family list grew by +0,74 entries for buyers and +0,22 for non-buyers.

    Scored without a cut it looks like a strong predictor; that is hindsight.
    crm_product_map(as_of=...) must therefore return only families first seen on
    or before the cut, and must drop rows with no first_seen at all rather than
    assume they were known."""
    import datetime as dt
    from adwatch.insights import icp
    from adwatch.models import Company, CrmCompanyProduct
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add(Company(name="Haendler", country="DE", segment="Handel"))
    s.commit()
    cid = s.scalar(select(Company.id))
    s.add(CrmCompanyProduct(company_id=cid, family="Glas-Faltwand",
                            first_seen=dt.date(2023, 1, 1)))
    s.add(CrmCompanyProduct(company_id=cid, family="cero",
                            first_seen=dt.date(2026, 1, 1)))
    s.add(CrmCompanyProduct(company_id=cid, family="Wintergarten",
                            first_seen=None))
    s.commit()
    s.close()

    assert icp.crm_product_map()[cid] == ["Glas-Faltwand", "Wintergarten", "cero"]
    assert icp.crm_product_map(as_of=dt.date(2024, 1, 1))[cid] == ["Glas-Faltwand"]
    assert icp.crm_product_map(as_of=dt.date(2022, 1, 1)) == {}

def test_a_feature_known_mostly_for_winners_is_dropped(temp_db):
    """The availability guard, at the threshold that `crm_products` forced.

    Its family list is known for 92,4% of buyers and 50,4% of the base — ratio
    1,83. At the old threshold of 2,5 it passed, and the whole-base backtest
    jumped to lift 2,71 / ranks=True while Handel+Verarbeiter ALONE fell to
    0,75, i.e. worse than random. The feature was re-learning "this account has
    been worked", which separates architects from dealers and nothing else."""
    from adwatch.insights import icp
    assert icp._LEAK_RATIO <= 1.85, (
        "der Schwellenwert muss crm_products auf der Rohbasis fangen")

def test_list_features_cover_both_product_columns():
    """Two product columns with different provenance and 20x different coverage.
    Both are multi-valued, so every place that special-cases a list has to know
    about both — the count, the distribution, the lift and the score."""
    from adwatch.insights import icp
    assert set(icp._LIST_FEATURES) == {"products", "crm_products"}
    for f in icp._LIST_FEATURES:
        assert f in icp.DEFAULT_WEIGHTS
        assert f in icp._FEATURE_LABEL_DE

def test_company_verkaufschancen_are_complete_and_deduped(temp_db):
    """"Everything that has to do with this firm" cannot be a sample. The dossier
    ships the ten newest per role for its summary; this endpoint is the rest.

    Two rules it has to keep. A Verkaufschance counted once even when the company
    plays several roles on it (1.850 firms are Käufer AND Architekt AND Endkunde),
    and a `total` that is the real count — one company carries 1.266."""
    import datetime as dt
    from adwatch import dossier
    from adwatch.models import Company, CrmOpportunity
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add(Company(name="Haendler", crm_id="GUID-A"))
    s.add(Company(name="Andere", crm_id="GUID-B"))
    s.commit()
    cid = s.scalar(select(Company.id).where(Company.name == "Haendler"))
    for i in range(25):
        s.add(CrmOpportunity(crm_id=f"o{i}", opportunity_guid=f"g{i}",
                             number=f"NR{i:03d}", parent_account_crm_id="guid-a",
                             state="gewonnen" if i < 5 else "verloren",
                             created_on=dt.datetime(2024, 1, 1) + dt.timedelta(days=i)))
    # same deal, three roles at once -> ONE Verkaufschance, not three
    s.add(CrmOpportunity(crm_id="multi", opportunity_guid="gmulti", number="NR999",
                         parent_account_crm_id="guid-a", architect_crm_id="guid-a",
                         end_customer_crm_id="guid-a", state="offen",
                         created_on=dt.datetime(2026, 1, 1)))
    # a deal that belongs to somebody else
    s.add(CrmOpportunity(crm_id="fremd", opportunity_guid="gfremd",
                         parent_account_crm_id="guid-b", state="offen",
                         created_on=dt.datetime(2026, 2, 1)))
    s.commit()
    s.close()

    r = dossier.verkaufschancen(cid, limit=10)
    assert r["total"] == 26, "25 plus die Mehrrollen-VC, die Fremde nicht"
    assert r["returned"] == 10
    assert r["by_role"] == {"kaeufer": 26, "architekt": 1, "endkunde": 1}
    assert r["rows"][0]["number"] == "NR999", "neueste zuerst"
    assert sorted(r["rows"][0]["roles"]) == ["architekt", "endkunde", "kaeufer"]

    # paging reaches the end and never repeats a row
    seen = []
    for off in (0, 10, 20):
        seen += [v["number"] for v in dossier.verkaufschancen(cid, limit=10, offset=off)["rows"]]
    assert len(seen) == 26 and len(set(seen)) == 26

    # role filter narrows both the rows and the counts
    only = dossier.verkaufschancen(cid, role="architekt")
    assert only["total"] == 1 and only["by_role"] == {"architekt": 1}

def test_dossier_objekte_come_from_all_vcs_not_the_last_ten(temp_db):
    """The Objekte list was derived from `blk["recent"]`, which is capped at ten.
    A company on 1.250 buildings therefore showed only those touched by its ten
    newest Verkaufschancen, and nothing said so."""
    import datetime as dt
    from adwatch import dossier
    from adwatch.models import Company, CrmOpportunity
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add(Company(name="Haendler", crm_id="GUID-A"))
    s.commit()
    cid = s.scalar(select(Company.id))
    for i in range(30):
        s.add(CrmOpportunity(crm_id=f"o{i}", opportunity_guid=f"g{i}",
                             project_id=f"p{i}", project_name=f"Bauvorhaben {i}",
                             parent_account_crm_id="guid-a", state="verloren",
                             lost_reason="Zu teuer",
                             created_on=dt.datetime(2024, 1, 1) + dt.timedelta(days=i)))
    s.commit()
    s.close()

    d = dossier.build(cid)
    assert d["projekte_total"] == 30, "alle Objekte zaehlen, nicht nur die der letzten 10 VCs"
    assert len(d["projekte"]) <= 20            # die Liste selbst bleibt gedeckelt
    assert d["rollen"]["kaeufer"]["vcs"] == 30
    assert len(d["rollen"]["kaeufer"]["recent"]) == 10

def test_the_backfill_closes_only_what_was_really_searched(temp_db):
    """The repair for the rows already written without a verdict. A candidate
    trail is the evidence that a search ran; without one the row may have come
    from an allow_search=False pass, which knows nothing about the wider web and
    must not be allowed to close the question."""
    from adwatch import dataquality
    from adwatch.models import Company, CompanyEnrichment
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add(Company(name="Gesucht SL", country="ES"))
    s.add(Company(name="Nie gesucht SL", country="ES"))
    s.add(Company(name="Schon geprueft SL", country="ES",
                  identity_status="verified", identity_matched_by="phone"))
    s.commit()
    ids = list(s.scalars(select(Company.id).order_by(Company.id)))
    s.add(CompanyEnrichment(company_id=ids[0], status="no_website_found",
                            website_candidates=[{"domain": "fremd.example",
                                                 "origin": "serper",
                                                 "validated": False}]))
    s.add(CompanyEnrichment(company_id=ids[1], status="no_website_found",
                            website_candidates=None))
    s.add(CompanyEnrichment(company_id=ids[2], status="no_website_found",
                            website_candidates=[{"domain": "x.example"}]))
    s.commit()
    s.close()

    dry = dataquality.close_searched_not_found(apply=False)
    assert dry["rows"] == 1, "nur die wirklich gesuchte Firma"

    dataquality.close_searched_not_found(apply=True)
    s = temp_db.SessionLocal()
    got = {c.name: c.identity_status for c in s.scalars(select(Company))}
    assert got["Gesucht SL"] == "not_found"
    assert got["Nie gesucht SL"] is None, "ohne Suchspur bleibt die Frage offen"
    assert got["Schon geprueft SL"] == "verified", "ein Urteil wird nie ueberschrieben"
    ev = s.scalar(select(Company).where(Company.name == "Gesucht SL")).identity_evidence
    assert ev["searched"] is True and ev["accepted"] is None
    s.close()

    # idempotent: a second run finds nothing left to do
    assert dataquality.close_searched_not_found(apply=False)["rows"] == 0

def test_the_cold_icp_refuses_the_two_poisoned_features(temp_db):
    """Gemessen 2026-08-13 an der deutschen Händlerbasis, beide Male als STARKE
    Prädiktoren aufgetaucht und beide Male Artefakt:

    * Vertriebsweg 'Direktvertrieb': n=55, Kaufrate 54,5% gegen 13,5% Basis —
      das beschreibt unsere Beziehung zur Firma, nicht die Firma.
    * Untersegment leer: n=209, Kaufrate 42,6% — Import-Herkunft. Und es ist die
      gefährliche Richtung: eine im Internet neu gefundene Firma hat EBENFALLS
      kein Untersegment und bekäme aus einem Grund, der nicht überträgt, eine
      hohe Punktzahl. Genau das würde die Zwillingssuche vergiften.

    Die Merkmalsfunktion darf beides nicht kennen."""
    from adwatch.insights import profiles
    from adwatch.models import Company

    c = Company(name="Musterfenster GmbH", segment="Handel",
                sub_segment="Fensterbau", country="DE", postal_code="49074",
                sales_channel="Direktvertrieb", website_domain="x.example")
    f = profiles._features_cold(c)
    assert not any("Direktvertrieb" in x or "vertriebsweg" in x.lower() for x in f), \
        "der Vertriebsweg beschreibt die Beziehung, nicht die Firma"
    assert "branche:Fensterbau" in f and "region:DE49" in f

    # ohne Untersegment darf KEIN Branchenmerkmal entstehen — auch kein 'leer'
    c2 = Company(name="Ohne Untersegment", segment="Handel", country="DE",
                 postal_code="49074")
    f2 = profiles._features_cold(c2)
    assert not any(x.startswith("branche:") for x in f2), \
        "'kein Untersegment' ist Herkunft, kein Merkmal"

def test_a_warranty_credit_is_not_a_purchase(temp_db):
    """14.049 der 91.992 Bestellereignisse stehen auf 0 EUR (Garantie, Muster,
    Ersatz), und 486 Firmen haben AUSSCHLIESSLICH solche — die galten als Kunden.
    Wer eine Garantiegutschrift als Erfolg zählt, trainiert das Modell darauf,
    Reklamationen vorherzusagen. Gemessen: 95 von 1.369 'Käufern' waren keine,
    und die Bereinigung bringt +0,012 AUC (0,617 -> 0,629).

    Die Zielgröße verlangt daher mindestens EIN Ereignis mit Betrag > 0."""
    import datetime as dt
    from adwatch.insights import profiles
    from adwatch.models import Company, CrmOrderEvent
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add(Company(name="Echter Kaeufer", segment="Handel", sub_segment="Fensterbau",
                  country="DE", postal_code="49074"))
    s.add(Company(name="Nur Garantie", segment="Handel", sub_segment="Fensterbau",
                  country="DE", postal_code="49074"))
    s.commit()
    ids = {c.name: c.id for c in s.query(Company).all()}
    s.add(CrmOrderEvent(company_id=ids["Echter Kaeufer"],
                        order_date=dt.date(2025, 6, 1), amount=4000.0))
    s.add(CrmOrderEvent(company_id=ids["Nur Garantie"],
                        order_date=dt.date(2025, 6, 1), amount=0.0))
    s.commit()
    s.close()

    comps, pre, post, _vc = profiles._load(dt.date(2025, 1, 1), "DE")
    assert post[ids["Echter Kaeufer"]]["paid"] == 1
    assert post[ids["Nur Garantie"]]["n"] == 1, "die Bewegung existiert"
    assert post[ids["Nur Garantie"]]["paid"] == 0, "aber sie ist kein Kauf"

def test_the_at_risk_list_is_worth_calling(temp_db):
    """Die Kunden-Fortsetzung sortiert aufsteigend — die riskantesten zuerst.
    Ohne Wertgrenze besteht die Spitze aus Firmen mit EINER 40-Euro-Bestellung
    vor drei Jahren: mathematisch korrekt, betriebswirtschaftlich wertlos.
    Dieselbe 2.000-Euro-Schwelle wie im Bericht trennt Rettbares von Rauschen."""
    from adwatch.insights import profiles
    assert profiles.MATERIAL_EUR == 2000

def test_ipp_scores_lift_not_popularity(temp_db):
    """Die Lehre aus dem Firmen-ICP, auf Projekte übertragen: fit_for belohnte
    die HÄUFIGSTE Ausprägung der Gewinner (Bauelementehandel: 36,5% der Gewinner,
    Lift 1,03) und rankte damit exakt falsch herum. Das IPP muss Lift zahlen,
    nicht Popularität — und unter MIN_SUPPORT Gewinnern ist eine Ausprägung
    Anekdote und taucht gar nicht erst auf, egal wie perfekt ihre Quote ist."""
    from adwatch.insights import ipp, projekte

    # 100 entschiedene Projekte, Basisrate 20%: 'haeufig' tragen fast alle
    # (Gewinner wie Verlierer), 'selten-gut' nur 12 — davon 9 Gewinner.
    rows = []
    for i in range(100):
        won = i < 20
        feats = {"haeufig"}
        if (i < 9) or (77 <= i < 80):          # 9 Gewinner + 3 Verlierer
            feats = {"haeufig", "selten-gut"}
        if i == 0:
            feats |= {"perfekt-aber-3x"}       # 100%-Quote, aber nur 1 Gewinner
        rows.append(("k%d" % i, feats, projekte.WON if won else projekte.LOST,
                     2024, None))

    w = ipp._fit(rows)
    assert "perfekt-aber-3x" not in w, "unter dem Boden zählt keine perfekte Quote"
    assert "selten-gut" not in w, "9 Gewinner sind unter MIN_SUPPORT=10 — Anekdote"
    assert abs(w["haeufig"]["lift"] - 1.0) < 0.05, \
        "was jeder hat, sagt nichts — Lift ~1, nicht 'stark weil haeufig'"

    # jetzt mit genug Support: 15 Gewinner von 20 Trägern -> Lift deutlich > 1,
    # aber Laplace hält ihn UNTER der rohen Quote (75% / 20% = 3,75)
    rows2 = []
    for i in range(200):
        won = i < 40
        feats = {"basis"}
        if (i < 15) or (190 <= i < 195):
            feats = {"basis", "gut"}
        rows2.append(("j%d" % i, feats, projekte.WON if won else projekte.LOST,
                      2024, None))
    w2 = ipp._fit(rows2)
    raw = (15 / 20) / 0.2
    assert 1.5 < w2["gut"]["lift"] < raw, \
        "Laplace muss die kleine Stichprobe daempfen, nicht ausloeschen"

    # der Score eines Projekts mit dem guten Merkmal schlaegt eines ohne
    assert ipp._score({"basis", "gut"}, w2) > ipp._score({"basis"}, w2)

def test_discovery_matches_known_companies_two_ways(temp_db):
    """Der Abgleich entscheidet über das Ergebnis des ganzen Versuchs. Nur über
    die Domain zu prüfen würde 'neu' systematisch überschätzen: von 10.998
    deutschen Händlern haben bloß 5.463 (49 %) überhaupt eine Domain hinterlegt.
    Deshalb zusätzlich Name+Ort — und deshalb wird das Ergebnis als SPANNE
    berichtet, nicht als eine Zahl."""
    from adwatch import discover
    from adwatch.models import Company
    from sqlalchemy import select

    s = temp_db.SessionLocal()
    s.add(Company(name="Mustermann Fensterbau GmbH", city="Osnabrück",
                  country="DE", website_domain="mustermann-fenster.de",
                  segment="Verarbeiter"))
    s.add(Company(name="Sonnenschein Glaserei", city="Münster", country="DE",
                  segment="Verarbeiter"))          # KEINE Domain hinterlegt
    s.commit()
    ids = {c.name: c.id for c in s.query(Company).all()}
    s.close()

    by_domain, by_city = discover._known_index()

    # Weg 1: harte Domain
    cid, how = discover._match_known(
        {"domain": "mustermann-fenster.de", "title": "Irgendein Titel"},
        by_domain, by_city)
    assert cid == ids["Mustermann Fensterbau GmbH"] and how == "domain"

    # Weg 2: Name + Ort, für die Hälfte des Bestands ohne Domain
    cid2, how2 = discover._match_known(
        {"domain": "sonnenschein-glas.de",
         "title": "Sonnenschein Glaserei — Ihr Glaser in Münster"},
        by_domain, by_city)
    assert cid2 == ids["Sonnenschein Glaserei"] and how2 == "name_ort"

    # eine wirklich fremde Firma bleibt neu
    cid3, _ = discover._match_known(
        {"domain": "voellig-fremd.de", "title": "Völlig Fremd GmbH, Kiel"},
        by_domain, by_city)
    assert cid3 is None

    # Rechtsform und Umlaute dürfen den Vergleich nicht sprengen
    assert discover._norm_name("Müller & Söhne GmbH") == discover._norm_name("Mueller & Soehne")

def test_email_coverage_findet_luecken_und_teilmonate(temp_db):
    """`coverage()` muss die zwei gemessenen Ausfallarten des E-Mail-Abrufs
    finden — und zwar BEIDE.

    Der Erstabruf am 2026-08-18 verlor 7 von 41 Monaten am Flow-Timeout und lief
    trotzdem sauber durch: die Schleife fängt Fehler ab, damit ein schlechter
    Monat keinen Vier-Stunden-Lauf killt. Das Ergebnis SAH vollständig aus,
    während rund 65.000 Mails fehlten.

    Der zweite Fall ist der tückischere: 2026-05 stand mit 2.834 statt ~9.500
    Zeilen in der Datenbank, Rest eines Testlaufs. Über Anwesenheit allein ist
    das NICHT zu finden — der Monat ist da, nur eben zu einem Drittel.
    """
    import datetime as dt
    from adwatch import crm_emails
    from adwatch.models import CrmEmail

    def add(s, monat: str, n: int):
        for i in range(n):
            s.add(CrmEmail(activity_id=f"{monat}-{i}",
                           created_on=dt.datetime.fromisoformat(f"{monat}-05T09:00:00")))

    s = temp_db.SessionLocal()
    add(s, "2024-01", 100)
    add(s, "2024-02", 100)
    # 2024-03 fehlt komplett — Flow-Timeout
    add(s, "2024-04", 100)
    add(s, "2024-05", 5)        # Teilabruf: da, aber weit unter dem Median
    s.commit(); s.close()

    cov = crm_emails.coverage(dt.date(2024, 1, 1), dt.date(2024, 6, 1))

    assert cov["monate_erwartet"] == 5
    assert cov["fehlend"] == ["2024-03"], "der komplett fehlende Monat"
    assert cov["duenn"] == ["2024-05"], "der Teilmonat, den Anwesenheit übersieht"
    assert cov["median_pro_monat"] == 100
    assert cov["vollstaendig"] is False

    # Ein lückenloser Zeitraum darf nicht fälschlich Alarm schlagen.
    ok = crm_emails.coverage(dt.date(2024, 1, 1), dt.date(2024, 3, 1))
    assert ok["fehlend"] == [] and ok["duenn"] == []
    assert ok["vollstaendig"] is True

    # Der laufende Monat ist ZU RECHT unvollständig und darf nie als "dünn"
    # gemeldet werden — sonst ist die Prüfung jeden Tag rot.
    s = temp_db.SessionLocal()
    heute = dt.date.today()
    add(s, f"{heute:%Y-%m}", 1)
    s.commit(); s.close()
    lauf = crm_emails.coverage(heute.replace(day=1),
                               (heute.replace(day=1) + dt.timedelta(days=32)).replace(day=1))
    assert lauf["duenn"] == [], "der laufende Monat wird ausgenommen"

def test_lead_antwortform_und_aufloesung(temp_db):
    """Zwei Stellen, an denen der Lead-Abruf still falsch laufen wuerde.

    1. FORM. Der Flow liefert fuer `leads` ein nacktes Array, fuer `accounts`
       dagegen {value: [...]}. Wer sich auf eine Form verlaesst, bekommt beim
       anderen Entity null Zeilen -- und zwar ohne Fehler, was der schlimmste
       Fall ist: der Abruf meldet Erfolg und laedt nichts.

    2. AUFLOESUNG. Ein Lead wird NUR ueber die im CRM gesetzte Mutterfirma auf
       eine Firma gezogen, nie ueber Namensaehnlichkeit. "Fenster Meier" und
       "Meier Fenster- und Tuerenbau GmbH" koennen dieselbe Firma sein oder
       nicht -- das entscheidet kein Stringvergleich, und eine falsche
       Verknuepfung vergiftet jede spaetere Auswertung.
    """
    from adwatch import crm_leads
    from adwatch.models import Company

    assert crm_leads._rows([{"leadid": "a"}]) == [{"leadid": "a"}]
    assert crm_leads._rows({"value": [{"leadid": "b"}]}) == [{"leadid": "b"}]
    assert crm_leads._rows({}) == []
    assert crm_leads._rows(None) == []

    s = temp_db.SessionLocal()
    c = Company(name="Meier Fenster- und Tuerenbau GmbH", crm_id="GUID-1",
                resolution_status="confirmed", country="DE")
    s.add(c); s.commit()
    resolve = crm_leads._company_resolver(s)

    assert resolve("GUID-1") == c.id, "gesetzte Mutterfirma wird aufgeloest"
    assert resolve("GUID-UNBEKANNT") is None
    assert resolve(None) is None, "ohne Mutterfirma bleibt die Frage offen"
    s.close()

def test_lead_holt_keine_personendaten():
    """Die Feldliste ist eine Zusage, keine Bequemlichkeit.

    firstname, lastname, emailaddress1 und telephone1 stehen in Dataverse und
    waeren einen Tastendruck entfernt. Sie duerfen nicht in SELECT stehen --
    gespeichert wird ausschliesslich, was die FIRMA beschreibt."""
    from adwatch import crm_leads
    for feld in ("firstname", "lastname", "emailaddress", "telephone",
                 "mobilephone", "fullname"):
        assert feld not in crm_leads.SELECT, f"{feld} ist eine Personendatei"
    assert "companyname" in crm_leads.SELECT

def test_profil_bevoelkerung_kennt_die_zukunft_nicht(temp_db):
    """Eine Firma, die erst NACH dem Stichtag im CRM angelegt wurde, darf nicht
    in der Bevoelkerung stehen -- am Stichtag kannten wir sie nicht.

    Das war ein echter Fehler: frisch angelegte Konten fragen mit 33,1 % an,
    alte mit 22,1 %, weil eine Firma oft ANGELEGT wird, WEIL sie angefragt hat.
    Damit sagte das Anlagedatum die Anfrage voraus. Garten- und Landschaftsbau
    stand mit Lift 4,45 an der Spitze der Kalt-Liste -- 51 seiner 57 Konten
    stammten aus 2024+. Nach der Korrektur faellt das Gewerk unter die
    Traegergrenze und verschwindet."""
    import datetime as dt
    from adwatch.insights import profiles
    from adwatch.models import Company

    s = temp_db.SessionLocal()
    s.add_all([
        Company(name="Alt", segment="Handel", country="DE",
                crm_created_on=dt.datetime(2020, 5, 1)),
        Company(name="Neu", segment="Handel", country="DE",
                crm_created_on=dt.datetime(2025, 6, 1)),
        Company(name="Ohne Datum", segment="Handel", country="DE"),
    ])
    s.commit(); s.close()

    comps, _pre, _post, _vc = profiles._load(dt.date(2025, 1, 1))
    namen = {c.name for c in comps}
    assert "Alt" in namen
    assert "Ohne Datum" in namen, "ohne Anlagedatum laesst sich nichts ausschliessen"
    assert "Neu" not in namen, "am Stichtag gab es diese Firma bei uns noch nicht"

def test_projektwert_ist_die_primaere_vc_nicht_die_summe(temp_db):
    """Der Wert eines Objekts ist der Wert seiner PRIMAEREN Verkaufschance.

    Vorher wurden alle Geschwister addiert. An einem Gebaeude bekommen aber
    mehrere Haendler und Generalunternehmer dasselbe Gewerk angeboten --
    gewinnen kann nur einer. Karlsruhe, Rheinstrasse 91 stand deshalb mit
    14,7 Mio EUR in der Liste: derselbe Betrag von 2.293.202 lag dort viermal,
    1.277.564 dreimal.

    Gemessen an 581 GEWONNENEN Objekten, wo der tatsaechliche Auftragswert
    bekannt ist -- Verhaeltnis Formel zu Auftrag:
        Summe    Median 2,41x (28 % brauchbar)
        Maximum  Median 1,21x (82 %)
        primaere Median 1,01x (85 %)   <- praktisch unverzerrt
    """
    from adwatch.insights.projekte import _projekt_schaetzwert
    from adwatch.models import CrmOpportunity

    prim = CrmOpportunity(crm_id="1", opportunity_guid="P", project_id="P",
                          estimated_value=2_293_202)
    geschwister = [prim] + [
        CrmOpportunity(crm_id=str(i), opportunity_guid=f"G{i}", project_id="P",
                       estimated_value=v)
        for i, v in enumerate([2_293_202, 2_293_202, 1_277_564, 1_277_564], start=2)]

    wert = _projekt_schaetzwert(prim, geschwister)
    assert wert == 2_293_202, "der Wert der primaeren VC, nicht die Summe"
    assert wert != sum(float(m.estimated_value) for m in geschwister)

    # Rueckfall: traegt die primaere VC keinen Wert, gilt der groesste --
    # von den verbleibenden Regeln liegt er am wenigsten daneben.
    ohne = CrmOpportunity(crm_id="1", opportunity_guid="P", project_id="P",
                          estimated_value=None)
    assert _projekt_schaetzwert(ohne, [ohne] + geschwister[1:]) == 2_293_202

    # gar keine Werte -> None, nicht 0 (0 EUR und "unbekannt" sind verschieden)
    leer = CrmOpportunity(crm_id="1", opportunity_guid="P", project_id="P")
    assert _projekt_schaetzwert(leer, [leer]) is None

def test_beziehungsstufe_gewonnenes_objekt_schlaegt_lead(temp_db, monkeypatch):
    """Architekten kaufen nichts -- die Beziehung steht verteilt im CRM. Die
    hoechste erreichte Stufe zaehlt, und eine gewonnene Verkaufschance schlaegt
    alles darunter."""
    from sqlalchemy import select

    from adwatch.insights import beziehung
    from adwatch.models import Company, CrmLead, CrmOpportunity

    monkeypatch.setattr(beziehung, "SessionLocal", temp_db.SessionLocal)
    s = temp_db.SessionLocal()
    a = Company(name="Gewinner", segment="Architekten", crm_id="guid-a")
    b = Company(name="Nur Lead", segment="Architekten", crm_id="guid-b")
    c = Company(name="Unbekannt", segment="Architekten", crm_id="guid-c")
    s.add_all([a, b, c]); s.commit()
    s.add_all([
        CrmOpportunity(crm_id="vc1", architect_crm_id="guid-a", state="gewonnen"),
        CrmLead(lead_id="l1", company_id=a.id),      # auch Lead -- darf nicht gewinnen
        CrmLead(lead_id="l2", company_id=b.id),
    ])
    s.commit(); s.close()

    r = beziehung.berechnen(nur_architekten=True, apply=True)
    s = temp_db.SessionLocal()
    stufen = {c.name: c.relation_level for c in s.scalars(select(Company))}
    s.close()
    assert stufen["Gewinner"] == 5, "gewonnene VC ist die hoechste Stufe"
    assert stufen["Nur Lead"] == 1
    assert stufen["Unbekannt"] == 0
    assert r["warm"] == 1

def test_projektseiten_werden_auch_im_singular_erkannt():
    """Der gemeinsame Link-Katalog kennt nur die Mehrzahl (`projekte`,
    `projects`). Einzelne Projekte liegen aber fast immer im Singular --
    endersweissbangert.de fuehrt die Uebersicht unter /projekte und die
    Projekte unter /project/... . Ohne diese Regel wurde KEINE Detailseite
    gelesen, also genau die Seiten, auf denen der Ort steht."""
    from adwatch.enrich import laenderlauf as LL

    uebersicht = "https://www.endersweissbangert.de/projekte"
    assert LL._ist_projektseite(
        "https://www.endersweissbangert.de/project/kiju-am-sportplatz", uebersicht)
    assert LL._ist_projektseite(
        "https://www.praglowski.de/projekt/kita/?portfolioCats=18", uebersicht)
    # Assets und der Ruecksprung auf die Uebersicht zaehlen nicht
    assert not LL._ist_projektseite(uebersicht, uebersicht)
    assert not LL._ist_projektseite(
        "https://www.endersweissbangert.de/wp-content/uploads/favicon.png", uebersicht)
    assert not LL._ist_projektseite(
        "https://www.endersweissbangert.de/xmlrpc.php", uebersicht)

def test_projektpfad_erkennt_das_wort_auch_mitten_im_abschnitt():
    """reinshaus.com legt seine 25 Projekte unter /portfolioreader-1784/... ab.
    Eine Regel, die das Wort als eigenen Wegabschnitt verlangt, findet dort
    KEINE einzige Seite. Entscheidend ist stattdessen, ob nach dem Abschnitt
    mit dem Projektwort noch einer folgt -- das trennt die Uebersicht von der
    Einzelseite."""
    from adwatch.enrich import laenderlauf as LL

    assert LL._ist_projekt_pfad("https://x.de/portfolioreader-1784/neubau-lager")
    assert LL._ist_projekt_pfad("https://x.de/projekte/haus-am-see")
    assert LL._ist_projekt_pfad("https://x.de/project/kiju")
    # Uebersichtsseiten sind KEINE Einzelprojekte
    assert not LL._ist_projekt_pfad("https://x.de/projekte")
    assert not LL._ist_projekt_pfad("https://x.de/portfolioreader-1784")
    assert not LL._ist_projekt_pfad("https://x.de/kontakt")

def test_gesundheit_ist_filterbar_und_customer_state_bleibt_luecke(temp_db, monkeypatch):
    """Gemessen 2026-09-08 im Audit: `customer_state` fuehrt 2.219 Firmen MIT
    echten SAP-Belegen als 'never', weil es aus den Umsatz-Schnappschussspalten
    kommt, die nur auf 3.623 von 48.543 Firmen gefuellt sind.

    `health` kommt aus den 91.992 Belegen und ist stimmig ('nie' hat 0 Belege,
    'aktiv' im Schnitt 42,9) -- war aber bis dahin NUR Anzeigespalte. Wer nach
    Kunden filtern will, braucht die gepruefte Spalte."""
    from adwatch import customers
    from adwatch.models import Company

    monkeypatch.setattr(customers, "SessionLocal", temp_db.SessionLocal)
    s = temp_db.SessionLocal()
    s.add_all([
        # genau der Fall, der den Audit ausgeloest hat: echte Belege, aber
        # customer_state sagt 'never', weil der Schnappschuss leer ist
        Company(name="Kauft wirklich", segment="Handel", health="aktiv",
                customer_state="never", beleg_count=42),
        Company(name="Gefaehrdet", segment="Handel", health="gefährdet",
                customer_state="never", beleg_count=4),
        Company(name="Nie", segment="Handel", health="nie",
                customer_state="never", beleg_count=0),
    ])
    s.commit(); s.close()

    assert customers.query_companies({"health": ["aktiv"]}, page_size=1)["total"] == 1
    assert customers.query_companies({"health": ["nie"]}, page_size=1)["total"] == 1
    # customer_state waere hier fuer ALLE DREI 'never' -- der Filter darauf
    # kann den zahlenden Kunden nicht von der leeren Zeile trennen
    assert customers.query_companies({"customer_state": ["never"]},
                                     page_size=1)["total"] == 3

def test_projekte_werden_entdoppelt():
    """Dasselbe Projekt unter mehreren Adressen z\u00e4hlt einmal.

    ab-grimm.de f\u00fchrt jedes Projekt unter `/080_port-andratx/index.htm` UND
    unter `/080_port-andratx/080_port-andratx.htm`. Beim ANTEIL faellt das
    nicht auf, weil Z\u00e4hler und Nenner mitwachsen \u2014 bei der absoluten Zahl
    schon, und die geht in die Bewertung ein.
    """
    from adwatch.enrich.tiefenlauf import _projekte_entdoppeln

    roh = [
        {"url": "https://x.de/projekte/080_port-andratx/index.htm",
         "titel": "Ausbau Ferienhaus", "orte_es": ["andratx"], "orte_andere": {},
         "hat_ort": True},
        {"url": "https://x.de/projekte/080_port-andratx/080_port-andratx.htm",
         "titel": "Ausbau Ferienhaus", "orte_es": ["andratx"], "orte_andere": {},
         "hat_ort": True},
        {"url": "https://x.de/projekte/081_anderes/index.htm",
         "titel": "Anderes Haus", "orte_es": [], "orte_andere": {"DE": ["essen"]},
         "hat_ort": True},
    ]
    aus = _projekte_entdoppeln(roh)
    assert len(aus) == 2
    assert sum(1 for p in aus if p["orte_es"]) == 1

def test_ort_im_seitenfuss_ist_keine_projektadresse():
    """Ein Ort auf FAST JEDER Projektseite geh\u00f6rt zur Vorlage, nicht zum Projekt.

    Gemessen 2026-09-09, und der Fehler war gross: bfl-architekten.de meldete
    169 spanische Projekte \u2014 darunter \u201eDachausbau Berlin-K\u00f6penick" und
    \u201eGrundschule Berlin-Spandau". Im Seitenfu\u00df steht:

        B\u00fcro Valencia (ES)  E-46018 Valencia  T. +34 636508235

    big.dk dasselbe: \u201eRonda de Sant Pere, 56 Bajos, 08010 Barcelona" im Fu\u00df,
    268 von 268 Projekten angeblich in Spanien \u2014 und \u201eRonda" ist dort ein
    STRASSENNAME, gelesen als andalusische Kleinstadt.

    `drop_chrome` entfernt Navigationsmen\u00fcs, aber keine Fu\u00dfzeilen. Statt
    Fu\u00dfzeilen zu erkennen z\u00e4hlt die Regel nach: was auf 60 % aller
    Projektseiten steht, ist Vorlage. Titeltreffer bleiben ausgenommen.
    """
    from adwatch.enrich.tiefenlauf import _chrome_orte_entfernen

    # 10 Berliner Projekte, alle mit „valencia" aus dem Fuss
    projekte = [{"url": f"https://x.de/p{i}", "titel": f"Dachausbau Berlin {i}",
                 "orte_es": ["valencia"], "orte_andere": {"DE": ["berlin"]},
                 "gruende": {"valencia": "Stadt (30 PLZ)"}, "hat_ort": True}
                for i in range(10)]
    aus, chrome = _chrome_orte_entfernen(projekte)
    assert chrome == {"valencia": 10}
    assert all(not p["orte_es"] for p in aus)
    assert all(p["hat_ort"] for p in aus)          # der deutsche Ort bleibt

    # Gegenprobe: ein Buero mit wenigen echten Spanien-Projekten behaelt sie
    echte = [{"url": f"https://y.de/p{i}", "titel": t_, "orte_es": o,
              "orte_andere": {}, "gruende": {x: "im Projekttitel" for x in o},
              "hat_ort": True}
             for i, (t_, o) in enumerate([
                 ("Ausbau Ferienhaus Mallorca", ["mallorca"]),
                 ("Umbau Cala Llamp", ["mallorca"]),
                 ("Weinstube Aschaffenburg", []), ("Wohnhaus Goldbach", []),
                 ("Scheune Obernau", []), ("Haus Hoesbach", []),
                 ("Praxis Alzenau", []), ("Buero Kahl", [])])]
    aus2, chrome2 = _chrome_orte_entfernen(echte)
    assert chrome2 == {}
    assert sum(1 for p in aus2 if p["orte_es"]) == 2

    # Und der Grenzfall: ein Ort auf allen Seiten, aber jedes Mal im TITEL —
    # ein Buero, das wirklich nur auf Mallorca baut, verliert seine Orte nicht.
    nur_mallorca = [{"url": f"https://z.de/p{i}", "titel": f"Finca {i} Mallorca",
                     "orte_es": ["mallorca"], "orte_andere": {},
                     "gruende": {"mallorca": "im Projekttitel"}, "hat_ort": True}
                    for i in range(9)]
    aus3, chrome3 = _chrome_orte_entfernen(nur_mallorca)
    assert chrome3 == {"mallorca": 9}
    assert all(p["orte_es"] == ["mallorca"] for p in aus3)

def test_archivseiten_sind_keine_projekte():
    """Jahres- und Kategorienarchive bestehen den Pfadtest, sind aber Kataloge.

    cruzyortiz.com fuehrt /project-year/1999-en und
    /project-category/museums-galleries-en. Beide enthalten "project", beide
    listen ein Dutzend Projekte mit ein Dutzend Orten. Fuenf von vierzehn
    Stichproben kamen von solchen Seiten.
    """
    from adwatch.enrich.tiefenlauf import _art, _ist_archivseite

    assert _ist_archivseite("https://x.com/en/project-year/1999-en")
    assert _ist_archivseite("https://x.com/en/project-category/museums-en")
    assert _ist_archivseite("https://x.com/projekte/tag/wohnbau")
    assert _ist_archivseite("https://x.com/p/1", "1999 archivos")
    assert not _ist_archivseite("https://x.com/projekte/haus-am-see")

    assert _art("https://x.com/en/project-category/museums") == "sonstige"
    assert _art("https://x.com/projekte/haus-am-see") == "projekt"

def test_portale_sind_keine_bueros():
    """Fremde Projekte auf Portalen und in Zeitschriften zaehlen nicht.

    Zwei echte Faelle aus dem Bestand, beide unter den ersten acht Treffern:
    aju.at leitet auf archilovers.com um -- im CRM steht die eigene Domain,
    gelesen wurden 23 Projekte fremder Architekten. Und architektur-aktuell.at
    ist eine Zeitschrift; ihre "Projekte" sind Artikel ueber das Reina Sofia
    und das Bernabeu.

    Geprueft wird der Host der PROJEKT-URL, nicht die CRM-Domain -- sonst
    faellt der Umleitungsfall durch. Die uebrigen abweichenden Hosts im
    Bestand sind echte Umbenennungen und muessen drin bleiben.
    """
    from tools.spanien_excel import _ist_portal

    assert _ist_portal("https://www.archilovers.com/projects/61473/x.html")
    assert _ist_portal("archilovers.com")
    assert _ist_portal("https://www.architektur-aktuell.at/projekte/reina-sofia")

    assert not _ist_portal("https://esteva.eu/proyectos/casa")   # Umbenennung
    assert not _ist_portal("https://oma.com/projects/x")         # Umbenennung
    assert not _ist_portal("aju.at")                             # eigene Domain
    # Kein Treffer ueber eine blosse Zeichenkette: die Domain muss enden.
    assert not _ist_portal("https://archdaily.com.mx/projects/x")
    assert not _ist_portal("https://meine-baunetz.de/x")
