"""Database schema. Source-agnostic (a `source` column everywhere) so Google /
LinkedIn adapters can reuse the same tables later. Raw ads are kept per run so
metrics can be recomputed if the classifier or spend model changes."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Company(Base):
    """A tracked company IS a customer — there is no separate customer entity.
    Most rows (up to ~3000, from the Solarlux Excel export or a future direct
    DB feed — see customers.upsert_customers) carry only the master-data
    fields below and no ad-tracking data at all; a row becomes "tracked" the
    first time it's fetched (page_id/resolution_status get set then), not via
    any separate promotion step."""
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), unique=True)
    country: Mapped[str] = mapped_column(String(4), default="DE")
    source: Mapped[str] = mapped_column(String(20), default="meta")

    # ---- Master data (from the Excel import / SAP) — optional, since the
    # original hand-added companies predate this and have none of it. ----
    sap_number: Mapped[str | None] = mapped_column(String(40), nullable=True)   # SAP Nummer
    kv: Mapped[str | None] = mapped_column(String(120), nullable=True)          # KV (account owner)
    segment: Mapped[str | None] = mapped_column(String(120), nullable=True)     # Kundensegment
    sub_segment: Mapped[str | None] = mapped_column(String(120), nullable=True)  # Kundenuntersegment
    sales_channel: Mapped[str | None] = mapped_column(String(120), nullable=True)  # Vertriebsweg
    street: Mapped[str | None] = mapped_column(String(300), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email: Mapped[str | None] = mapped_column(String(300), nullable=True)
    fax: Mapped[str | None] = mapped_column(String(80), nullable=True)
    revenue_y0: Mapped[float | None] = mapped_column(Float, nullable=True)   # Umsatz aktuelles Jahr
    revenue_y1: Mapped[float | None] = mapped_column(Float, nullable=True)   # -1
    revenue_y2: Mapped[float | None] = mapped_column(Float, nullable=True)   # -2
    revenue_y3: Mapped[float | None] = mapped_column(Float, nullable=True)   # -3
    revenue_y4: Mapped[float | None] = mapped_column(Float, nullable=True)   # -4
    imported_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    website_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)  # e.g. 'solarlux.com' — used to resolve the Google Ads advertiser (no name search available there)

    # ---- Enrichment (see enrich/ + models.CompanyEnrichment) — the few fields
    # promoted onto Company because the Explorer filters/sorts and the PDF report
    # use them directly. The full extracted blob + per-field provenance lives in
    # CompanyEnrichment; these are a denormalised convenience copy. ----
    description: Mapped[str | None] = mapped_column(Text, nullable=True)          # 1-2 Sätze, from the company's own site
    products: Mapped[list | None] = mapped_column(JSON, nullable=True)            # ['Fenster','Wintergarten',...]
    founded_year: Mapped[int | None] = mapped_column(Integer, nullable=True)      # only when literally stated ('seit 1952')
    employee_hint: Mapped[str | None] = mapped_column(String(120), nullable=True)  # verbatim ('15 Mitarbeiter'), never an LLM estimate
    enrichment_status: Mapped[str] = mapped_column(String(20), default="none")
    # none = never enriched | enriched = data found & accepted | needs_review = a
    # candidate website failed deterministic validation (a human decides)
    # no_website_found = searched, nothing credible found | error

    # ---- Scoring (see insights/icp.py + insights/divergence.py) ----
    # customer_state: lifecycle derived from the imported Umsatz columns at
    # import time — active (buys now, bought before) | new (first revenue this
    # year) | lapsed (bought before, nothing this year) | never (no revenue on
    # record). Stored so the Explorer can filter/sort on it directly.
    customer_state: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # Own group / intercompany entity (Linara, NanaWall, Solarlux Vertriebsbüros,
    # subsidiaries). They appear as ordinary customers with large revenue, so
    # without this flag the ICP learns to love the group's own companies and
    # recommends them as acquisition targets. Never a winner, never a target.
    is_intercompany: Mapped[bool] = mapped_column(Boolean, default=False)
    fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)          # 0-100 vs the applied ICP
    opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0-100, Divergenz (needs ad data)
    target_score: Mapped[float | None] = mapped_column(Float, nullable=True)       # combined priority (see icp.apply)
    fit_breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # per-feature 'Warum' for the drawer
    scores_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    page_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    page_name: Mapped[str | None] = mapped_column(String(300), nullable=True)   # matched Facebook page name
    page_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    resolution_status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending = never fetched yet | confirmed = page_id linked (auto or manual) | ambiguous = best-guess, needs a human look
    # no_ads_found = a name search ran and returned zero ads (wrong name OR genuinely inactive)
    # locked = a human verified & locked this identity — the HIGHEST status: never
    #   overwritten by any automatic API resolution (the identity-check job skips it).
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{page_id,name,ad_count}] when ambiguous
    page_category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    page_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    page_likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    runs: Mapped[list["CollectionRun"]] = relationship(back_populates="company")
    metrics: Mapped[list["WeeklyCompanyMetric"]] = relationship(back_populates="company")
    pages: Mapped[list["CompanyPage"]] = relationship(back_populates="company")


class CompanyPage(Base):
    """A Facebook page that belongs to a company. One company can own several:
    its main page plus dedicated partner accounts (e.g. a 'Solarlux Partner'
    page running ads for them). `evidence` records WHY the link was made so a
    human can audit and undo it."""
    __tablename__ = "company_pages"
    __table_args__ = (UniqueConstraint("source", "page_id", name="uq_source_page"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    source: Mapped[str] = mapped_column(String(20), default="meta")
    page_id: Mapped[str] = mapped_column(String(120))
    page_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="main")      # main | partner
    status: Mapped[str] = mapped_column(String(20), default="auto")    # confirmed | auto | manual
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {method, url, utm, token, similarity}
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    linked_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    company: Mapped["Company"] = relationship(back_populates="pages")


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    source: Mapped[str] = mapped_column(String(20), default="meta")
    run_date: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    week_start: Mapped[dt.date] = mapped_column(Date)
    page_id: Mapped[str | None] = mapped_column(String(120), nullable=True)   # which page this run fetched
    page_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    page_role: Mapped[str | None] = mapped_column(String(20), nullable=True)  # main | partner | hub
    status: Mapped[str] = mapped_column(String(30), default="ok")  # ok | no_active_ads | error
    ads_scraped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="runs")
    ads: Mapped[list["Ad"]] = relationship(back_populates="run")


class Ad(Base):
    __tablename__ = "ads"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("collection_runs.id"))
    source: Mapped[str] = mapped_column(String(20), default="meta")

    external_ad_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ad_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cta: Mapped[str | None] = mapped_column(String(120), nullable=True)
    start_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    media_type: Mapped[str | None] = mapped_column(String(30), nullable=True)

    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)          # EU DSA reach if present
    real_spend: Mapped[float | None] = mapped_column(Float, nullable=True)      # only for regulated ads
    ad_library_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # view this ad on Meta
    landing_url: Mapped[str | None] = mapped_column(String(500), nullable=True)     # where its CTA points

    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    product: Mapped[str | None] = mapped_column(String(300), nullable=True)
    classifier: Mapped[str | None] = mapped_column(String(40), nullable=True)  # deterministic | llm
    classifier_raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # original Apify item, for debugging/remap

    run: Mapped["CollectionRun"] = relationship(back_populates="ads")


class WeeklyCompanyMetric(Base):
    __tablename__ = "weekly_company_metrics"
    __table_args__ = (UniqueConstraint("company_id", "source", "week_start", name="uq_company_week"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    source: Mapped[str] = mapped_column(String(20), default="meta")
    week_start: Mapped[dt.date] = mapped_column(Date)

    total_active_ads: Mapped[int] = mapped_column(Integer, default=0)
    ads_by_category: Mapped[dict] = mapped_column(JSON, default=dict)   # {category: count}
    products: Mapped[list] = mapped_column(JSON, default=list)          # distinct product strings
    new_ads: Mapped[int] = mapped_column(Integer, default=0)            # ads whose start_date is within the last 7 days
    score: Mapped[float | None] = mapped_column(Float, nullable=True)   # 0-100 activity score (see insights/score.py)

    estimated_spend_low: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_spend_high: Mapped[float] = mapped_column(Float, default=0.0)
    spend_method: Mapped[str | None] = mapped_column(String(30), nullable=True)  # reach | count | mixed
    spend_model_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    real_spend_regulated: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(30), default="ok")

    company: Mapped["Company"] = relationship(back_populates="metrics")


class ReportRecipient(Base):
    """Who the weekly report PDF can be emailed to — managed entirely in-app;
    Power Automate never decides this, it just sends to whatever address this
    app tells it to."""
    __tablename__ = "report_recipients"
    __table_args__ = (UniqueConstraint("email", name="uq_recipient_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class FetchJob(Base):
    """A scoped fetch run (e.g. 'fetch these 80 selected companies') that
    persists its own progress, unlike the original single in-memory fetch
    (adwatch/web.py's `_runs` queue), which dies with the process. Restarting
    the app mid-job doesn't lose the job — see jobs.py's startup reconciliation,
    which marks any row still 'running' as 'interrupted' so a human decides
    whether to resume, rather than silently continuing (and re-spending) on
    every restart."""
    __tablename__ = "fetch_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # queued | running | cancelling | done | failed | cancelled | interrupted
    kind: Mapped[str] = mapped_column(String(20), default="fetch")
    # fetch (ads) | identity (page resolution) | enrich (website + facts)
    # | pipeline (several of the above in the correct order, see jobs._run_pipeline)
    sources: Mapped[list] = mapped_column(JSON, default=list)          # ['meta','google']
    # For kind='pipeline': which steps to run and their options, e.g.
    # {"enrich":true,"identity":true,"ads":["meta","google"],"report":"full",
    #  "send_to":[3,7]}. Kept as data so the run is reproducible and auditable.
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    company_ids: Mapped[list] = mapped_column(JSON, default=list)      # the scoped set, fixed at creation
    label: Mapped[str | None] = mapped_column(String(300), nullable=True)  # e.g. filter description, for history

    total: Mapped[int] = mapped_column(Integer, default=0)       # total (company × source) units of work
    completed: Mapped[int] = mapped_column(Integer, default=0)   # units done so far — the resume cursor
    errors: Mapped[int] = mapped_column(Integer, default=0)
    ads_collected: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[list] = mapped_column(JSON, default=list)  # capped list of recent {ts, text} entries


class Setting(Base):
    """In-app overrides for the settings in config.SETTINGS_SPEC (API keys,
    endpoints, model, country). A row here takes precedence over the matching
    .env variable; an absent/blank row falls back to .env, then the default.
    Edited from the Settings tab — see config.__getattr__ for resolution."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class ScheduleConfig(Base):
    """Single-row table (id=1) holding when the app auto-fetches ads and
    auto-emails the weekly report. Edited from the dashboard's Settings panel;
    the in-process scheduler (see scheduler.py) re-reads it whenever it's saved."""
    __tablename__ = "schedule_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    fetch_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    fetch_day: Mapped[int] = mapped_column(Integer, default=6)   # 0=Mon .. 6=Sun (cron 'day_of_week')
    fetch_time: Mapped[str] = mapped_column(String(5), default="22:00")  # 'HH:MM'
    fetch_sources: Mapped[list] = mapped_column(JSON, default=lambda: ["meta"])  # meta | google, any combo
    send_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    send_day: Mapped[int] = mapped_column(Integer, default=0)    # Monday
    send_time: Mapped[str] = mapped_column(String(5), default="07:00")
    send_report: Mapped[str] = mapped_column(String(10), default="top5")  # top5 | full


class CompanyEnrichment(Base):
    """Everything enrichment learned about one company, plus WHY we believe it.

    `fields` is the extracted blob (description, products, founded_year,
    employee_hint, legal_form, service_area, mentions_solarlux,
    competitor_brands, ...). `provenance` maps each field name ->
    {source, confidence, evidence, fetched_at}, so any value can be audited and
    a wrong one traced back — the same auditability that CompanyPage.evidence
    gives identity. Enrichment NEVER overwrites SAP/human-entered data; it only
    fills blanks (see enrich/service.py).

    `website_source` records how the website itself was determined:
      email_domain = derived from the SAP contact email (free, Tier 0)
      serper       = found via a web search (Tier 1)
      sap          = came with the import — authoritative, never replaced
      manual       = a human set/approved it
    """
    __tablename__ = "company_enrichment"
    __table_args__ = (UniqueConstraint("company_id", name="uq_enrichment_company"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))

    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)

    website_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    website_candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)  # rejected/unvalidated options, for the review queue
    website_validated_by: Mapped[str | None] = mapped_column(String(40), nullable=True)  # phone | plz_street | name_tokens | none

    status: Mapped[str] = mapped_column(String(20), default="none")   # mirrors Company.enrichment_status
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(60), nullable=True)   # which model produced `fields`
    enriched_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class IcpProfile(Base):
    """A computed Ideal-Customer-Profile: WHICH companies defined it (the
    winners filter), what their feature distributions look like, and when it was
    applied to score the whole base. Kept as rows (not a single config) so a
    score on a company is always traceable to the exact profile that produced
    it — same auditability rule as enrichment provenance."""
    __tablename__ = "icp_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="ICP")
    winners_filter: Mapped[dict] = mapped_column(JSON, default=dict)   # the Explorer filter that selected the winners
    winners_count: Mapped[int] = mapped_column(Integer, default=0)
    features: Mapped[dict] = mapped_column(JSON, default=dict)         # per-feature value distributions of the winners
    weights: Mapped[dict] = mapped_column(JSON, default=dict)          # feature weights used when scoring
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class ReportDefinition(Base):
    """A saved, re-runnable (and optionally scheduled) report over a custom
    Companies-Explorer filter: a name + the exact filter blob + which recipients
    to email + an optional weekly cron. It turns 'filter → report → send' into a
    one-click action or a hands-off automatic one, so a BD user never has to
    re-filter and hand-mail a PDF for a recurring custom scope. Independent of
    ScheduleConfig, which still drives the single standard weekly report."""
    __tablename__ = "report_definitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    report_type: Mapped[str] = mapped_column(String(10), default="full")   # full | top5
    filters: Mapped[dict] = mapped_column(JSON, default=dict)               # a currentCustomerFilters() blob
    recipient_ids: Mapped[list] = mapped_column(JSON, default=list)         # ReportRecipient ids to email
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_day: Mapped[int] = mapped_column(Integer, default=0)           # 0=Mon .. 6=Sun
    schedule_time: Mapped[str] = mapped_column(String(5), default="07:00")  # 'HH:MM'
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(300), nullable=True)
