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
    # The Dataverse `accountid` GUID — the only truly durable identity. SAP
    # numbers are missing on ~25% of rows (they appear at first order) and names
    # change and collide; the GUID never does. Present in every CRM export as
    # the "(Nicht ändern) Firma" column, so it can be backfilled offline.
    # Matching order everywhere: crm_id -> sap_number -> exact name.
    crm_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    crm_modified_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)  # CRM `modifiedon` = delta watermark
    sap_number: Mapped[str | None] = mapped_column(String(40), nullable=True)   # SAP Nummer
    kv: Mapped[str | None] = mapped_column(String(120), nullable=True)          # KV (account owner)
    segment: Mapped[str | None] = mapped_column(String(120), nullable=True)     # Kundensegment
    sub_segment: Mapped[str | None] = mapped_column(String(120), nullable=True)  # Kundenuntersegment
    sales_channel: Mapped[str | None] = mapped_column(String(120), nullable=True)  # Vertriebsweg
    street: Mapped[str | None] = mapped_column(String(300), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Karte: Koordinaten mit Präzisionsgrad. 'plz' = PLZ-Zentroid (GeoNames,
    # offline, kostenlos — Stadt-genau), 'street' = Nominatim-Nachschärfung,
    # 'manual' = von Hand gesetzt. Ein Pin trägt seine Präzision sichtbar —
    # ungefähr muss ungefähr AUSSEHEN (dieselbe Regel wie belegt/KI-Chip).
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    geocode_precision: Mapped[str | None] = mapped_column(String(12), nullable=True)
    geocoded_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
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

    # ---- Real-world identity of the website ----
    # A domain in `website_domain` is only ever a CLAIM until something confirms
    # it belongs to THIS company. The distinction is not cosmetic: this column
    # drives the enrichment crawler and the Google advertiser lookup, so an
    # unverified domain silently produces a description, a product list and an ad
    # history for the WRONG company — and nothing downstream can tell.
    #
    # That is exactly what happened when 22,696 CRM-typed URLs were bulk-filled
    # into this column alongside 1,426 gate-validated ones with no way to
    # distinguish them. Hence provenance is now mandatory.
    #
    #   website_source: 'crm' (typed by a colleague in Dataverse — good evidence,
    #     NOT proof) | 'serper' (a search guess) | 'email' (derived from the mail
    #     domain) | 'manual' (a human here) | 'impressum' (found on the site)
    website_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #   identity_status: 'unverified' = a claim nobody checked | 'verified' = the
    #     site carries this company's own master data | 'conflict' = the site was
    #     read and does NOT match (a portal, a parent brand, a namesake) |
    #     'unreachable' = could not be fetched, so still unknown
    identity_status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    #   which signal proved it — phone | plz_street | plz_name | domain_plus_name
    #   (see enrich/validate.validate_site; phone is the strongest)
    identity_matched_by: Mapped[str | None] = mapped_column(String(24), nullable=True)
    identity_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    identity_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # ---- Enrichment (see enrich/ + models.CompanyEnrichment) — the few fields
    # promoted onto Company because the Explorer filters/sorts and the PDF report
    # use them directly. The full extracted blob + per-field provenance lives in
    # CompanyEnrichment; these are a denormalised convenience copy. ----
    description: Mapped[str | None] = mapped_column(Text, nullable=True)          # 1-2 Sätze, from the company's own site
    products: Mapped[list | None] = mapped_column(JSON, nullable=True)            # ['Fenster','Wintergarten',...]
    founded_year: Mapped[int | None] = mapped_column(Integer, nullable=True)      # only when literally stated ('seit 1952')
    employee_hint: Mapped[str | None] = mapped_column(String(120), nullable=True)  # verbatim ('15 Mitarbeiter'), never an LLM estimate
    # ---- Enrichment, part 2: fields the extractor already produced but nobody
    # could see. `CompanyEnrichment.fields` held legal_form, service_area,
    # competitor_brands, mentions_solarlux and the assessment as JSON, so no
    # filter, export, report or ICP feature could reach them — we were paying to
    # extract data and then hiding it. Mirrored here like description/products.
    legal_form: Mapped[str | None] = mapped_column(String(60), nullable=True)
    service_area: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Rival systems this firm installs (Schüco, Cortizo, Technal …). The conquest
    # signal: a fabricator already building premium façades with a competitor's
    # profiles is a proven capable buyer, and this names which brand to displace.
    competitor_brands: Mapped[list | None] = mapped_column(JSON, nullable=True)
    mentions_solarlux: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    assessment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ---- Qualification attributes, all nullable because "not stated" must stay
    # distinguishable from "no". Chosen to match the columns the Spain market
    # research already used by hand (Eigene Fertigung, Zertifizierungen,
    # Projektfokus), so machine and human research are directly comparable.
    certifications: Mapped[list | None] = mapped_column(JSON, nullable=True)
    own_fabrication: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_showroom: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    project_focus: Mapped[list | None] = mapped_column(JSON, nullable=True)
    positioning: Mapped[str | None] = mapped_column(String(20), nullable=True)  # premium|mittel|budget
    # ---- Self-declared machine-readable facts (enrich/site_facts.py) — no LLM.
    # facebook_url is the strongest Meta identity anchor available: a page linked
    # FROM the verified website provably belongs to this company, unlike a name
    # search. Feeds identity resolution rather than replacing it.
    # ---- Architekten-spezifisch (enrich profile 'architekt') ----
    # An architect's value is not "do they buy" but "do they specify projects
    # where Solarlux fits, and do they decide". A prestigious office doing
    # interiors or infrastructure is irrelevant however large it is.
    #   solarlux_relevance  hoch|mittel|gering — does the portfolio involve large
    #                       glazing, façades, folding/sliding systems at all
    #   decision_role       'vergibt Aufträge' vs 'empfiehlt' — the Spain/Germany
    #                       asymmetry made concrete per office
    solarlux_relevance: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    office_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    decision_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reference_scale: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enrich_profile: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # ---- Verarbeiter/Handel-spezifisch (enrich profile 'betrieb') ----
    # The mirror of solarlux_relevance, asked of a company that SELLS rather than
    # specifies: does it already build large glazing (hoch), could it add the
    # category (mittel), or is it a different trade altogether (gering)?
    #   partner_of  brands the site claims an EXPLICIT dealership with
    #               ("distribuidor oficial de CORTIZO") — a contractual tie, far
    #               stronger than merely naming a brand, and the clearest
    #               statement of who supplies them today
    #   installs    does the company mount on site, or only sell over the counter
    solarlux_fit: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    partner_of: Mapped[list | None] = mapped_column(JSON, nullable=True)
    installs: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    facebook_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    instagram_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    site_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
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
    # A COMPETITOR's own location (a Schüco showroom, a CORTIZO branch, a Reynaers
    # Niederlassung). Kept in the database on purpose — knowing where Schüco is
    # strong in Spain, and which prospects sit in its catchment, is real market
    # intelligence — but never a target. Excluded by scope.apply(), the same
    # mechanism that keeps Private Endkunden out of every number.
    #
    # Deliberately NOT set for firms that merely INSTALL a competitor's systems.
    # Those are the opposite of a competitor: proven premium fabricators already
    # spending with a rival, i.e. the best conquest targets in the list. They keep
    # `import_type = 'wettbewerber'` so their origin stays visible.
    is_competitor: Mapped[bool] = mapped_column(Boolean, default=False)
    # Where a NON-CRM row came from, e.g. 'marktanalyse_es_2026_08'. NULL means
    # CRM/Excel master data. This is what separates a colleague's scraped list
    # from the official base — permanently and with one filter.
    lead_source: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    # The classification the SOURCE file gave this row, verbatim and unmodified
    # ('potenzialkunde' | 'architekt' | 'wettbewerber' | 'bestandskunde' | ...).
    # Kept separate from `segment` because routing changes the segment while the
    # provenance must stay auditable: a firm imported as 'wettbewerber' but routed
    # to a prospect has to remain recognisable as having come in that way.
    import_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)          # 0-100 vs the applied ICP
    opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0-100, Divergenz (needs ad data)
    target_score: Mapped[float | None] = mapped_column(Float, nullable=True)       # combined priority (see icp.apply)
    fit_breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # per-feature 'Warum' for the drawer
    scores_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # ---- Belege (ax_sap_order) — the AUTHORITATIVE revenue source ----
    # Measured 2026-08: `slx_revenue_current_year` is filled on only 2.9% of CRM
    # accounts, so the revenue_y0..y4 snapshot above is near-empty for anyone
    # outside the original Excel export. The SAP Belege are transaction-level,
    # 99.9% of them carry a customer link, and they go back to 2019. Everything
    # that ranks or scores should prefer these. The snapshot columns are kept
    # untouched rather than overwritten, so nothing that already relied on them
    # silently changes meaning — `effective_revenue()` in insights/rfm.py picks.
    beleg_count: Mapped[int] = mapped_column(Integer, default=0)          # Belege in the loaded window
    beleg_sum: Mapped[float] = mapped_column(Float, default=0.0)          # EUR, same window
    beleg_first: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    beleg_last: Mapped[dt.date | None] = mapped_column(Date, nullable=True)  # -> Recency, the churn signal
    beleg_by_year: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"2023": 12345.0, ...}
    avg_discount: Mapped[float | None] = mapped_column(Float, nullable=True)  # Ø Grundrabatt = partner tier
    # Prescriptor influence, from opportunity.slx_executingarchitect_accountid.
    # 12.7% of opportunities fill that field, but it is the AUSFÜHRENDER Architekt
    # — whoever planned the job — and on 60.7% of those the value is the BUYER
    # itself (a dealer that plans in-house). A third-party architect appears on
    # 2,884 of 57,776 opportunities = 5.0%. These columns are imported verbatim
    # from the CRM export and inherit that conflation, so they count dealers as
    # their own architects; use insights/projekte.specifying_architect() for any
    # figure that has to mean "an architecture practice".
    # A 0 here means "not recorded" at least as often as it means "influences
    # nothing" — never rank a company DOWN on this, only up.
    arch_projects: Mapped[int] = mapped_column(Integer, default=0)
    arch_won: Mapped[int] = mapped_column(Integer, default=0)
    arch_won_value: Mapped[float] = mapped_column(Float, default=0.0)
    # Lifecycle from Beleg recency (see insights/rfm.classify) — distinct from
    # customer_state, which is derived from the sparse snapshot columns.
    health: Mapped[str | None] = mapped_column(String(16), nullable=True)
    winback_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    crm_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # ---- Angebote (ax_sap_quote) and what became of them ----
    # 43,922 quotes since 2023 worth EUR 1,448M against EUR 392M of orders — a
    # 27.1% value conversion, i.e. EUR 1,056M quoted and never ordered. This is
    # the single largest measured gap in the business.
    #
    # READ conversion_rate ONLY for Handel/Verarbeiter. For Architekten,
    # Baudienstleister and Wohnungswirtschaft the quoted party is NOT the ordering
    # party — Solarlux quotes the planner or object owner and the dealer places the
    # order — so their near-0% is an attribution artefact, not a lost deal.
    quote_count: Mapped[int] = mapped_column(Integer, default=0)
    quote_sum: Mapped[float] = mapped_column(Float, default=0.0)
    conversion_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # beleg_sum / quote_sum

    # Whether the ad-tracking pipeline should consider this company. The CRM
    # population is ~46,000 active business accounts; ad monitoring costs money
    # per company and is only wanted on a chosen subset. Bulk CRM imports land
    # with monitored=False so they feed the ICP and the analysis without
    # flooding the identity check, the fetch queue or the Explorer's ad views.
    # Every row that existed before the bulk import stays monitored=True.
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)

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
    # `active` = may this address be mailed at all (a soft delete).
    # `preselected` = is the send box ticked for them by default. Kept apart on
    # purpose: unticking someone for one send must NOT quietly stop the weekly
    # saved-report definitions from reaching them, which is what overloading
    # `active` would do.
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # server_default as well as default: db.py seeds the configured default
    # recipient with raw SQL, which never sees the ORM-side default.
    preselected: Mapped[bool] = mapped_column(Boolean, default=True,
                                              server_default="1")
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


class CrmShowroom(Base):
    """One product family (and concrete system) a dealer exhibits in their
    showroom — mirrored from the CRM table `sl_dealer_exposition` (1,739 rows,
    207 dealers, actively maintained).

    Why this matters more than it looks: the per-deal product table
    (`sl_opportunityproduct`) exists but is permission-denied, and the SAP order
    mirror is header-level only — so this is the best readable evidence of which
    product families a partner actually commits to. It is the basis of the
    cross-sell matrix: a dealer exhibiting Glas-Faltwand but NOT Wintergarten,
    who otherwise resembles Wintergarten buyers, is a concrete, named
    cross-sell target — revenue from a partner you already have.

    Joined to Company via `dealer_crm_id` -> `Company.crm_id` (the Dataverse
    accountid), which is exactly why the durable key had to come first."""
    __tablename__ = "crm_showrooms"
    __table_args__ = (UniqueConstraint("crm_id", name="uq_showroom_crm_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    crm_id: Mapped[str] = mapped_column(String(40))          # sl_dealer_expositionid
    dealer_crm_id: Mapped[str | None] = mapped_column(String(40), nullable=True)   # accountid
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    product_family: Mapped[str | None] = mapped_column(String(160), nullable=True)
    product: Mapped[str | None] = mapped_column(String(160), nullable=True)
    installed_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class CrmOpportunity(Base):
    """A Solarlux Verkaufschance (project), mirrored lean from Dataverse
    `opportunity`.

    Exists because of a business fact that broke the original ICP: **architects
    never buy.** All 808 architect accounts converted at 0%, which made the ICP's
    headline lift look like signal when it was really just "architects aren't
    dealers". The fix is not to exclude them — it is to score them on the right
    outcome. An architect's value to Solarlux is the PROJECT VOLUME THEY SPECIFY,
    and that lives here, not on their account.

    Three different companies can hang off one project, which is the whole point:
      parent_account_crm_id  = Auftraggeber (who orders — usually a dealer)
      architect_crm_id       = the specifying architecture practice
      end_customer_crm_id    = Bauherr / end customer
    Any of them can be the "company" a profile is about; the opportunity is the
    shared fact. NB the CRM sometimes has the architect and the end customer as
    the SAME account — do not assume they differ.

    Deliberately lean: only the fields with verified fill (`ax_order_value` 1,626
    rows, `slx_buildingtype` 4,860, architect 1,046, end customer 4,644) plus the
    state/date columns needed for win-rate and sales-cycle length. Everything else
    stays in CRM and is read live when a drawer needs it."""
    __tablename__ = "crm_opportunities"
    __table_args__ = (UniqueConstraint("crm_id", name="uq_opportunity_crm_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    crm_id: Mapped[str] = mapped_column(String(40))                    # opportunityid
    number: Mapped[str | None] = mapped_column(String(40), nullable=True)   # ax_opportunity_number
    name: Mapped[str | None] = mapped_column(String(400), nullable=True)

    parent_account_crm_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    architect_crm_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    end_customer_crm_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    sales_channel: Mapped[str | None] = mapped_column(String(80), nullable=True)
    building_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # 0 offen | 1 gewonnen | 2 verloren  (kept as the German label, like segments)
    state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    order_value: Mapped[float | None] = mapped_column(Float, nullable=True)  # ax_order_value
    products: Mapped[list | None] = mapped_column(JSON, nullable=True)   # slx_slproductnames, canonicalised
    # The PROJECT address, not the customer's. A Verkaufschance in Objektvertrieb
    # is a building site, so this is where the glass actually goes — the only
    # geography that says anything about where demand is. Missing from every
    # Excel export we were given (0% filled) while sitting at 85% in the CRM.
    street: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    created_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    closed_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    crm_modified_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    # WHY it was lost, decoded from `statuscode`. Measured over 38,523 lost
    # opportunities: half the lost volume (EUR 557M of EUR 1,123M) sits in reasons
    # a human could act on — "Kein Feedback vom Kunden", "Kein Interesse mehr",
    # "Zu teuer", "Wettbewerb" — and only 6.2% is a straight competitive loss.
    # The other half (Baugenehmigung, Projekt umgeplant, Duplikat, Endkunde hat
    # den Auftrag nicht erhalten) is not winnable and must be excluded from any
    # "we are losing deals" narrative, or the number becomes theatre.
    lost_reason: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    estimated_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_customer_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ---- Projekt-Verknüpfung (Besonderheit Objektvertrieb) ----
    # `sl_primary_opportunityid` groups several Verkaufschancen into ONE Objekt:
    # the primary VC *is* the project (its name is the building address), the
    # members are the per-firm attempts to win it. Business rule from Iheb: a
    # project counts as WON when ANY member wins — one win plus four "losses"
    # is a won project, and the CRM even closes those members as "Zugehörige VC
    # gewonnen". Counting them as losses (as the first loss analysis did)
    # overstates failure; the 1,355 such closures are wins at project level.
    project_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    opportunity_guid: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    project_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # ---- Felder, die im ersten Zug fehlten ("alles was nützlich sein könnte") ----
    # Each is a decoded LABEL, never the raw option-set integer.
    #
    # type_of_use   Gebäudenutzung: Wohnen 19.868 · Hotel/Gastgewerbe 1.115 ·
    #               Verwaltung/Büro 541 · Kultur/Sport 407 · Einzelhandel 373 ·
    #               Bildung 317 · Gesundheit/Pflege · Ausstellung. 99% filled —
    #               the project-type dimension the IPP will need, and already
    #               useful for "which firm wins which kind of building".
    # vc_type       Vertriebs-VC 38.767 vs Architekten-VC 2.796 — the CRM states
    #               the motion outright; we had been inferring it from roles.
    # dealer_status the DEALER's own pipeline state (Neu · Erstkontakt · Termin
    #               vereinbart · Angebot erstellen · Auftrag erhalten · Rückgabe ·
    #               Verloren). Engagement BEHAVIOUR, the strongest non-leaky
    #               partner-quality signal available for existing dealers.
    # origin        wer die Chance gebracht hat: vom Händler · vom Architekten ·
    #               von Solarlux · aus Online Konfigurator · vom Objektkunden ·
    #               von Linara <Standort>. Distinguishes self-generating partners
    #               from those who only convert leads we hand them.
    type_of_use: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    vc_type: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    dealer_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    origin: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    rating: Mapped[str | None] = mapped_column(String(30), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(30), nullable=True)
    sales_stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    vr_presented: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    business_unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_close: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # Was tatsächlich fakturiert wurde — über ax_sap_order.ax_opportunityid.
    # 23.955 Belege tragen diesen Link, 35.856 Angebote ebenfalls. Das schließt
    # die Kette Angebot → Auftrag → Beleg auf PROJEKT-Ebene; vorher war Umsatz
    # nur firmenweit bekannt und die Konversionsquote entsprechend grob.
    invoiced_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    invoiced_count: Mapped[int] = mapped_column(Integer, default=0)
    quoted_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    quoted_count: Mapped[int] = mapped_column(Integer, default=0)
    sap_order_numbers: Mapped[list | None] = mapped_column(JSON, nullable=True)


class PlzGeo(Base):
    """PLZ -> Zentroid, aus den GeoNames-Postleitzahltabellen (CC-BY 4.0).

    Eine Zeile je (Land, PLZ), Koordinate = Mittelwert über alle GeoNames-Orte
    dieser PLZ. Damit ist die GESAMTE Firmenbasis sofort und kostenlos auf
    Stadt-Genauigkeit pinnbar — ohne einen einzigen API-Aufruf. Straßengenau
    (Nominatim, ~1 Anfrage/s) kommt später als eigener, fortsetzbarer Job und
    nur für Firmen, bei denen die Präzision einen Unterschied macht."""
    __tablename__ = "plz_geo"
    __table_args__ = (UniqueConstraint("country", "plz", name="uq_plz_geo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    country: Mapped[str] = mapped_column(String(2), index=True)
    plz: Mapped[str] = mapped_column(String(20), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    place: Mapped[str | None] = mapped_column(String(200), nullable=True)


class CrmOrderEvent(Base):
    """One purchase EVENT: a company + a day, with the Belege of that day summed.

    Belege are not orders. Measured 2026-08-10: 129,676 Belege collapse to
    91,992 events (1.41 per event) over 7,734 companies, 2019-01-01..2026-08-05,
    because a multi-line order is issued as several documents. Computing an order
    rhythm on raw Belege gives medians of 0-3 days for big dealers and makes
    churn detection meaningless — this table exists so the cadence is measured on
    commercial events instead.

    (The figures "73,112 Belege -> 54,534 events" appear in OVERVIEW.md and
    DATA-QUALITY.md and are from the first, smaller import. Re-measure before
    quoting either document.)

    Two more properties of the raw data that this table deliberately preserves
    rather than hides, because both matter when interpreting a number:
      * 14,049 events are 0 EUR (warranty, samples, replacements). They are
        real contact but not revenue, so `amount` can legitimately be 0.
      * a Beleg can be negative (Storno/Retoure) — only 3 in the window, but
        summing without care would silently lose them.
    """
    __tablename__ = "crm_order_events"
    __table_args__ = (UniqueConstraint("company_id", "order_date",
                                       name="uq_order_event_company_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    order_date: Mapped[dt.date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)   # EUR, all Belege that day
    beleg_count: Mapped[int] = mapped_column(Integer, default=1)
    channel: Mapped[str | None] = mapped_column(String(40), nullable=True)   # ax_sales_channel
    discount: Mapped[float | None] = mapped_column(Float, nullable=True)     # Ø Grundrabatt that day


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


class ReportEvent(Base):
    """An audit trail of report creation and delivery: what was built, when, and
    who it was mailed to.

    Exists because a send used to leave no record anywhere. When a send failed
    with the browser's bare "failed to fetch", there was no way to answer the
    only question that mattered — did the mail go out? — which risks sending a
    colleague the same report twice. The rotating log file records it now, but a
    log file is not something a BD user reads, so the same facts are stored here
    and shown in the Logs tab.

    One row per event, never updated: 'created' when a PDF is written, 'sent' or
    'send_failed' per delivery attempt. A single report therefore has one
    'created' row and one row per attempt to mail it.
    """
    __tablename__ = "report_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))            # created | sent | send_failed
    filename: Mapped[str] = mapped_column(String(200))
    report_type: Mapped[str | None] = mapped_column(String(10), nullable=True)   # full | top5
    scope: Mapped[str | None] = mapped_column(String(400), nullable=True)        # the filter label
    recipients: Mapped[list | None] = mapped_column(JSON, nullable=True)         # addresses mailed to
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)  # manual|pipeline|schedule|definition
    detail: Mapped[str | None] = mapped_column(String(600), nullable=True)      # error text on failure
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)


class CrmCompanyProduct(Base):
    """What a company actually asks Solarlux for, by product family.

    Source is the custom `slx_product` table in Dataverse — the quote/order LINE
    ITEMS, 697.943 of them. This is the answer to "which product for whom", and
    it was missing from every export we had: `opportunityproducts` is empty in
    this org and the standard quotedetail/salesorderdetail tables return 403, so
    from the exports it looked as though product data did not exist at all.

    The value is QUOTED, not invoiced: the line items span won and lost
    opportunities alike (EUR 3,39 Mrd against EUR 391,8 Mio of actual revenue).
    That is deliberate and more useful than filtering to wins — it separates what
    a company ASKS for from what it BUYS, which is exactly the gap a BD person
    wants to see. Never present these euros as revenue.

    One row per company and family, so a dealer that quotes both Glas-Faltwand
    and cero has two rows and can be filtered on either.
    """
    __tablename__ = "crm_company_products"
    __table_args__ = (UniqueConstraint("company_id", "family",
                                       name="uq_company_product_family"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    family: Mapped[str] = mapped_column(String(120), index=True)
    positions: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    last_seen: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class CrmOpportunityProduct(Base):
    """Product families on ONE Verkaufschance.

    The company-level twin (CrmCompanyProduct) answers "what does this dealer
    deal in". This answers "what was actually in this project", which is the
    question an Objekt drawer has to answer — a Verkaufschance is a building
    site, and its product mix is the specification.

    Same caveat as everywhere: `value` is QUOTED, not invoiced.
    """
    __tablename__ = "crm_opportunity_products"
    __table_args__ = (UniqueConstraint("opportunity_guid", "family",
                                       name="uq_opp_product_family"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_guid: Mapped[str] = mapped_column(String(40), index=True)
    family: Mapped[str] = mapped_column(String(120), index=True)
    positions: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)


class TargetList(Base):
    """Eine ausgespielte Arbeitsliste — mit ihrer Kontrollgruppe.

    Das ist der Grund, warum es diese Tabelle überhaupt gibt. Eine Rangliste
    abzuarbeiten und hinterher die Abschlüsse zu zählen, misst nichts: die
    obersten Firmen hätten teilweise ohnehin gekauft. Gemessen an der Basisrate
    kaufen 11,3 % der deutschen Händler auch ohne jeden Anruf.

    Was wirklich interessiert, ist nicht die Kaufwahrscheinlichkeit, sondern die
    REAKTION AUF ANSPRACHE (Ascarza 2018: Zielsteuerung nach Neigung ist
    messbar schlechter als nach Reaktionsfähigkeit). Und Reaktion ist nur gegen
    eine Kontrollgruppe messbar.

    Deshalb wird bei der Erzeugung einer Liste ein zufälliger Anteil als
    `holdout` markiert und ausdrücklich NICHT angesprochen. Die Zuteilung
    geschieht EINMAL, beim Anlegen, und ist unveränderlich — eine nachträglich
    verschobene Kontrollgruppe ist keine mehr.
    """
    __tablename__ = "target_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40))     # funnel | bestand | kalt | ipp | manuell
    filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    holdout_share: Mapped[float] = mapped_column(Float, default=0.15)
    seed: Mapped[int] = mapped_column(Integer, default=0)   # macht die Ziehung nachvollziehbar
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class TargetListEntry(Base):
    """Eine Firma auf einer Arbeitsliste, ihr Arm und ihr Ausgang.

    `score_at_creation` wird eingefroren: das Modell ändert sich, die Frage
    „hat die Rangfolge von damals funktioniert" darf sich nicht mit ihm ändern.
    """
    __tablename__ = "target_list_entries"
    __table_args__ = (UniqueConstraint("list_id", "company_id",
                                       name="uq_list_company"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("target_lists.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    score_at_creation: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 'ziel' = ansprechen, 'kontrolle' = bewusst NICHT ansprechen
    arm: Mapped[str] = mapped_column(String(10), default="ziel", index=True)

    contacted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    channel: Mapped[str | None] = mapped_column(String(30), nullable=True)   # telefon|mail|besuch|messe
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    outcome_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class CrmEmail(Base):
    """Eine im CRM an einen Datensatz ANGEHÄNGTE E-Mail — der Verlauf, den
    Kolleginnen und Kollegen bewusst zum Projekt gelegt haben.

    Warum das hier steht und nicht in einem Nebenprojekt: strukturierte Felder
    sagen DASS ein Objekt verloren ging (`lost_reason` kennt zehn Codes), die
    angehängte Korrespondenz sagt WARUM. Gemessen 2026-08-18 an einer Stichprobe
    von 300: alle 300 hängen an einem Datensatz — 150 an einer Verkaufschance,
    99 an einer Firma, 43 an einem Angebot, 8 an einem Lead. Es ist also
    Projektakte, nicht Postfach.

    `body_text` ist der von HTML befreite Text. Das ist keine Kürzung: gemessen
    sind nur 12–24 % der Rohbytes Inhalt, der Rest ist Formatierung und
    Signatur-Markup. `body_raw_chars` hält fest, wie viel Rohlänge dahinterstand,
    damit später niemand rätselt, ob etwas verloren ging.

    `direction` ist das wertvollste Feld: EINGEHEND heißt, die Firma hat sich bei
    UNS gemeldet — das ist Nachfrage, nicht Vertriebsaufwand. In der Stichprobe
    waren 4 von 300 eingehend, also selten und damit potenziell trennscharf.
    """
    __tablename__ = "crm_emails"
    __table_args__ = (UniqueConstraint("activity_id", name="uq_crm_email_activity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[str] = mapped_column(String(40), index=True)
    # woran die Mail hängt, im Original: opportunity | account | lead | ax_sap_quote
    regarding_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    regarding_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    # aufgelöst auf unsere Firma — über account direkt, über opportunity indirekt
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"),
                                                   nullable=True, index=True)
    created_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    sent_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    statecode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_raw_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class ProjectMailAnalysis(Base):
    """Auf Klick erzeugte Auswertung der Korrespondenz EINES Objekts.

    Bewusst auf Abruf und nicht als Lauf über alles: 450.000 E-Mails durch ein
    Sprachmodell zu schicken kostet dreistellig, und niemand braucht die
    Auswertung für Objekte, die keiner ansieht. Wer ein Objekt öffnet und die
    Frage hat, drückt den Knopf — und zahlt einen Bruchteil eines Cents.

    Das Ergebnis wird gespeichert, damit ein zweiter Blick nichts kostet, und
    trägt `model` und `analysed_at`: eine Ableitung ohne Zeitstempel und Modell
    ist in einem halben Jahr nicht mehr einzuordnen.
    """
    __tablename__ = "project_mail_analyses"
    __table_args__ = (UniqueConstraint("project_id", name="uq_project_mail_analysis"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), index=True)
    mails_used: Mapped[int] = mapped_column(Integer, default=0)
    chars_used: Mapped[int] = mapped_column(Integer, default=0)
    # Die Ableitung selbst: Wettbewerber, Einwände, wer verstummte, Kernursache.
    findings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    analysed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CrmLead(Base):
    """Ein Lead aus Dynamics — die Stufe VOR der Firma.

    Warum das eine eigene Tabelle verdient und nicht in `companies` gehört: ein
    Lead ist eine Behauptung, keine geprüfte Firma. Er kann eine Dublette einer
    bestehenden Firma sein, eine Firma, die es nie gab, oder derselbe Betrieb
    zum vierten Mal über ein Messeformular. Ihn in den Firmenbestand zu kippen,
    würde genau die Identitätsdisziplin aufweichen, die dieses Projekt teuer
    aufgebaut hat.

    Gemessen 2026-08-19, und das ist der Grund für den Abruf: 94.219 der 438.979
    angehängten E-Mails — 21,5 % — hängen an Leads, verteilt auf 42.157
    verschiedene. Ein Fünftel der gesamten Korrespondenz zeigte damit auf
    Datensätze, die es in unserem Spiegel nicht gab.

    KEINE PERSONENDATEN. `firstname`, `lastname`, `emailaddress*` und
    `telephone*` werden bewusst NICHT geholt, obwohl sie in Dataverse stehen.
    Gespeichert wird ausschließlich, was die FIRMA beschreibt.
    """
    __tablename__ = "crm_leads"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_crm_lead"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[str] = mapped_column(String(40), index=True)
    company_name: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # statecode 0=offen 1=qualifiziert 2=disqualifiziert; Klartext dazu, weil
    # eine nackte Zahl in einem halben Jahr niemand mehr einordnet
    statecode: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    state_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    statuscode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lead_source: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    quality: Mapped[str | None] = mapped_column(String(60), nullable=True)
    revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    employees: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Wenn der Lead im CRM bereits an eine Firma gehängt wurde, ist das die
    # verlässlichste Auflösung, die es gibt — sie stammt von einem Menschen.
    parent_account_crm_id: Mapped[str | None] = mapped_column(String(40),
                                                              nullable=True, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"),
                                                   nullable=True, index=True)
    created_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    modified_on: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
