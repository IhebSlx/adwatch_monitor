"""Database engine / session helpers.

Mock and live data live in SEPARATE database files, chosen by config.MODE at
call time. This keeps deterministic mock data (and its fake MOCK:: page ids)
from ever polluting real live data, and makes the dashboard show the dataset
that matches the selected mode.

init_db() also performs a lightweight in-place migration for pre-existing
SQLite files (adds new columns, backfills company_pages from the legacy
single page_id column) so upgrading never requires wiping collected history.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .models import Base

_engines: dict[str, object] = {}
_makers: dict[str, sessionmaker] = {}


def db_url_for_mode() -> str:
    """Live uses config.DB_URL (respects ADWATCH_DB_URL); mock uses a sibling file."""
    if config.MODE == "mock":
        return f"sqlite:///{config.DATA_DIR / 'adwatch_mock.db'}"
    return config.DB_URL


def _maker() -> sessionmaker:
    url = db_url_for_mode()
    if url not in _makers:
        eng = create_engine(url, future=True)
        _engines[url] = eng
        _makers[url] = sessionmaker(bind=eng, future=True, expire_on_commit=False)
    return _makers[url]


def SessionLocal() -> Session:
    """Return a new Session bound to the current mode's database."""
    return _maker()()


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def _migrate(engine) -> None:
    """Add columns introduced after the first release; backfill company_pages."""
    with engine.begin() as conn:
        # collection_runs: page attribution columns
        cols = _existing_columns(conn, "collection_runs")
        if cols:
            for name, ddl in [("page_id", "VARCHAR(120)"), ("page_name", "VARCHAR(300)"),
                              ("page_role", "VARCHAR(20)")]:
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE collection_runs ADD COLUMN {name} {ddl}"))
        # weekly_company_metrics: freshness + score
        cols = _existing_columns(conn, "weekly_company_metrics")
        if cols:
            if "new_ads" not in cols:
                conn.execute(text("ALTER TABLE weekly_company_metrics ADD COLUMN new_ads INTEGER DEFAULT 0"))
            if "score" not in cols:
                conn.execute(text("ALTER TABLE weekly_company_metrics ADD COLUMN score FLOAT"))
        # backfill: every company with a legacy page_id gets a main CompanyPage row
        have_companies = _existing_columns(conn, "companies")
        have_pages = _existing_columns(conn, "company_pages")
        if have_companies and have_pages:
            conn.execute(text("""
                INSERT INTO company_pages (company_id, source, page_id, page_name, role, status,
                                           evidence, active, linked_at)
                SELECT c.id, c.source, c.page_id, c.page_name, 'main',
                       CASE WHEN c.resolution_status = 'confirmed' THEN 'confirmed' ELSE 'auto' END,
                       NULL, 1, CURRENT_TIMESTAMP
                FROM companies c
                WHERE c.page_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM company_pages p
                                  WHERE p.source = c.source AND p.page_id = c.page_id)
            """))


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = _maker().kw["bind"]
    Base.metadata.create_all(engine)
    _migrate(engine)
