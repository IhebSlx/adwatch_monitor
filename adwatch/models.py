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
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), unique=True)
    country: Mapped[str] = mapped_column(String(4), default="DE")
    source: Mapped[str] = mapped_column(String(20), default="meta")

    page_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    page_name: Mapped[str | None] = mapped_column(String(300), nullable=True)   # matched Facebook page name
    page_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    resolution_status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending = never fetched yet | confirmed = page_id locked in | ambiguous = best-guess, needs a human look
    # no_ads_found = a name search ran and returned zero ads (wrong name OR genuinely inactive)
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
