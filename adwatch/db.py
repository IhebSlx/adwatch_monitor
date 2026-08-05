"""Database engine / session helpers.

init_db() also performs a lightweight in-place migration for pre-existing
SQLite files (adds new columns, backfills company_pages from the legacy
single page_id column) so upgrading never requires wiping collected history.
"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .models import Base

logger = logging.getLogger("adwatch.db")

_engine = create_engine(config.DB_URL, future=True)


# WAL + a real busy timeout: writers no longer block readers, and a concurrent
# writer waits up to 15s instead of failing instantly with "database is locked"
# (the default was 5s). Applied to every new SQLite connection.
@event.listens_for(_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    if config.DB_URL.startswith("sqlite"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


_Maker = sessionmaker(bind=_engine, future=True, expire_on_commit=False)


def SessionLocal() -> Session:
    return _Maker()


def integrity_ok() -> bool:
    """PRAGMA quick_check on startup — logs a loud warning if the DB file is
    corrupt so a bad file is noticed rather than silently half-working."""
    if not config.DB_URL.startswith("sqlite"):
        return True
    try:
        with _engine.connect() as c:
            result = c.exec_driver_sql("PRAGMA quick_check").scalar()
        if result != "ok":
            logger.error("SQLite quick_check FAILED: %s — restore from a backup in %s",
                         result, config.BACKUP_DIR)
            return False
        return True
    except Exception:
        logger.exception("SQLite quick_check could not run")
        return False


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
        # ads: link to view the ad itself + where its CTA points
        cols = _existing_columns(conn, "ads")
        if cols:
            for name in ("ad_library_url", "landing_url"):
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE ads ADD COLUMN {name} VARCHAR(500)"))
        # companies: website domain, used to resolve the Google Ads advertiser
        cols = _existing_columns(conn, "companies")
        if cols and "website_domain" not in cols:
            conn.execute(text("ALTER TABLE companies ADD COLUMN website_domain VARCHAR(200)"))
        # companies: master-data columns — a company IS a customer, no separate
        # table (see models.Company docstring). Additive only; nullable.
        if cols:
            for name, ddl in [
                ("sap_number", "VARCHAR(40)"), ("kv", "VARCHAR(120)"), ("segment", "VARCHAR(120)"),
                ("sub_segment", "VARCHAR(120)"), ("sales_channel", "VARCHAR(120)"),
                ("street", "VARCHAR(300)"), ("postal_code", "VARCHAR(20)"), ("city", "VARCHAR(200)"),
                ("phone", "VARCHAR(80)"), ("email", "VARCHAR(300)"), ("fax", "VARCHAR(80)"),
                ("revenue_y0", "FLOAT"), ("revenue_y1", "FLOAT"), ("revenue_y2", "FLOAT"),
                ("revenue_y3", "FLOAT"), ("revenue_y4", "FLOAT"), ("imported_at", "DATETIME"),
            ]:
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {ddl}"))
        # (a stray legacy 'customer_id' column may exist from an earlier build
        # that briefly had a separate customers table — left alone, unused)

        # One-time-ish fold-in: earlier builds had a SEPARATE `customers` table
        # (from before "a company IS a customer" was settled). If it's still
        # there, fold every row into `companies` — update ones already linked
        # via the old customer_id, match unlinked ones by exact name, else
        # insert a new row — then leave the old table alone (never dropped).
        # Idempotent: safe to run on every startup once the fold is done.
        have_customers = _existing_columns(conn, "customers")
        cols = _existing_columns(conn, "companies")
        if have_customers and cols and "customer_id" in cols:
            customer_rows = conn.execute(text("""
                SELECT id, sap_number, company_name, kv, segment, sub_segment, sales_channel,
                       street, postal_code, city, country, phone, email, fax, website,
                       revenue_y0, revenue_y1, revenue_y2, revenue_y3, revenue_y4, imported_at
                FROM customers
            """)).fetchall()
            for row in customer_rows:
                (cust_id, sap, cname, kv, seg, subseg, chan, street, plz, city, land, phone,
                 email, fax, website, r0, r1, r2, r3, r4, imported_at) = row
                params = {
                    "cid": cust_id, "sap": sap, "kv": kv, "seg": seg, "subseg": subseg,
                    "chan": chan, "street": street, "plz": plz, "city": city,
                    "phone": phone, "email": email, "fax": fax, "website": website,
                    "r0": r0, "r1": r1, "r2": r2, "r3": r3, "r4": r4,
                    "imported_at": imported_at, "name": cname,
                    "country": land if (land and len(land) <= 3) else config.DEFAULT_COUNTRY,
                }
                linked = conn.execute(text("SELECT id FROM companies WHERE customer_id = :cid"),
                                      {"cid": cust_id}).first()
                if linked:
                    conn.execute(text("""
                        UPDATE companies SET sap_number=:sap, kv=:kv, segment=:seg, sub_segment=:subseg,
                               sales_channel=:chan, street=:street, postal_code=:plz, city=:city,
                               phone=:phone, email=:email, fax=:fax,
                               revenue_y0=:r0, revenue_y1=:r1, revenue_y2=:r2, revenue_y3=:r3, revenue_y4=:r4,
                               imported_at=:imported_at,
                               website_domain = CASE WHEN website_domain IS NULL OR website_domain = ''
                                                      THEN :website ELSE website_domain END
                        WHERE customer_id=:cid
                    """), params)
                    continue
                if not cname:
                    continue  # nothing to name a new row with, and nothing to match by name
                existing_by_name = conn.execute(
                    text("SELECT id FROM companies WHERE name = :name AND customer_id IS NULL"),
                    {"name": cname}).first()
                if existing_by_name:
                    conn.execute(text("""
                        UPDATE companies SET sap_number=:sap, kv=:kv, segment=:seg, sub_segment=:subseg,
                               sales_channel=:chan, street=:street, postal_code=:plz, city=:city,
                               phone=:phone, email=:email, fax=:fax,
                               revenue_y0=:r0, revenue_y1=:r1, revenue_y2=:r2, revenue_y3=:r3, revenue_y4=:r4,
                               imported_at=:imported_at, customer_id=:cid,
                               website_domain = CASE WHEN website_domain IS NULL OR website_domain = ''
                                                      THEN :website ELSE website_domain END
                        WHERE id=:existing_id
                    """), {**params, "existing_id": existing_by_name[0]})
                    continue
                already = conn.execute(text("SELECT 1 FROM companies WHERE name = :name"),
                                       {"name": cname}).first()
                if already:
                    continue  # name collision with a row not tied to this customer — skip, don't guess
                conn.execute(text("""
                    INSERT INTO companies (name, country, source, resolution_status, customer_id,
                        sap_number, kv, segment, sub_segment, sales_channel, street, postal_code,
                        city, phone, email, fax, website_domain,
                        revenue_y0, revenue_y1, revenue_y2, revenue_y3, revenue_y4, imported_at)
                    VALUES (:name, :country, 'meta', 'pending', :cid,
                        :sap, :kv, :seg, :subseg, :chan, :street, :plz,
                        :city, :phone, :email, :fax, :website,
                        :r0, :r1, :r2, :r3, :r4, :imported_at)
                """), params)
        # companies: enrichment fields promoted onto the row (see enrich/ and
        # models.CompanyEnrichment). Additive + nullable; enrichment_status gets a
        # DEFAULT so existing rows read as "never enriched" rather than NULL.
        cols = _existing_columns(conn, "companies")
        if cols:
            for name, ddl in [
                ("description", "TEXT"), ("products", "JSON"),
                ("founded_year", "INTEGER"), ("employee_hint", "VARCHAR(120)"),
                ("enrichment_status", "VARCHAR(20) DEFAULT 'none'"),
            ]:
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {ddl}"))
            conn.execute(text("UPDATE companies SET enrichment_status = 'none' "
                              "WHERE enrichment_status IS NULL"))
        # companies: scoring columns (customer lifecycle + ICP fit) — additive.
        cols = _existing_columns(conn, "companies")
        if cols:
            for name, ddl in [
                ("customer_state", "VARCHAR(12)"), ("fit_score", "FLOAT"),
                ("opportunity_score", "FLOAT"), ("target_score", "FLOAT"),
                ("fit_breakdown", "JSON"), ("scores_updated_at", "DATETIME"),
                ("is_intercompany", "BOOLEAN DEFAULT 0"),
                ("crm_id", "VARCHAR(40)"), ("crm_modified_on", "DATETIME"),
            ]:
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {ddl}"))
            # backfill customer_state from the revenue columns (same derivation
            # as customers.derive_customer_state, inlined as SQL for the one-off)
            conn.execute(text("""
                UPDATE companies SET customer_state = CASE
                  WHEN COALESCE(revenue_y0,0) > 0 AND (COALESCE(revenue_y1,0) > 0 OR COALESCE(revenue_y2,0) > 0
                       OR COALESCE(revenue_y3,0) > 0 OR COALESCE(revenue_y4,0) > 0) THEN 'active'
                  WHEN COALESCE(revenue_y0,0) > 0 THEN 'new'
                  WHEN COALESCE(revenue_y1,0) > 0 OR COALESCE(revenue_y2,0) > 0
                       OR COALESCE(revenue_y3,0) > 0 OR COALESCE(revenue_y4,0) > 0 THEN 'lapsed'
                  ELSE 'never' END
                WHERE customer_state IS NULL
            """))
        # fetch_jobs: kind discriminator (fetch = ads, identity = page resolution only)
        cols = _existing_columns(conn, "fetch_jobs")
        if cols and "plan" not in cols:
            conn.execute(text("ALTER TABLE fetch_jobs ADD COLUMN plan JSON"))
        if cols and "kind" not in cols:
            conn.execute(text("ALTER TABLE fetch_jobs ADD COLUMN kind VARCHAR(20) DEFAULT 'fetch'"))
            conn.execute(text("UPDATE fetch_jobs SET kind = 'fetch' WHERE kind IS NULL"))
        # report_events is created by create_all — nothing to migrate, but an
        # existing DB has no history, so the Logs tab starts from this release.
        # report_recipients: remembered tick state for the send boxes. Existing
        # rows default to 1 so nobody silently drops out of a send they were
        # already part of.
        cols = _existing_columns(conn, "report_recipients")
        if cols and "preselected" not in cols:
            conn.execute(text("ALTER TABLE report_recipients "
                              "ADD COLUMN preselected BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE report_recipients SET preselected = 1 "
                              "WHERE preselected IS NULL"))
        # schedule_config: which source(s) the auto-fetch job runs
        cols = _existing_columns(conn, "schedule_config")
        if cols and "fetch_sources" not in cols:
            conn.execute(text("ALTER TABLE schedule_config ADD COLUMN fetch_sources JSON"))
            conn.execute(text("UPDATE schedule_config SET fetch_sources = '[\"meta\"]' WHERE fetch_sources IS NULL"))
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
        # seed the recipients table from .env's default, once, if still empty
        if _existing_columns(conn, "report_recipients"):
            has_any = conn.execute(text("SELECT 1 FROM report_recipients LIMIT 1")).first()
            if not has_any and config.REPORT_EMAIL_DEFAULT_RECIPIENT:
                conn.execute(text(
                    "INSERT INTO report_recipients (name, email, active, added_at) "
                    "VALUES (:name, :email, 1, CURRENT_TIMESTAMP)"
                ), {"name": None, "email": config.REPORT_EMAIL_DEFAULT_RECIPIENT})


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    integrity_ok()                     # loud warning if the DB file is corrupt
    from .backup import backup_now
    backup_now(tag="startup")          # snapshot before any migration runs
    Base.metadata.create_all(_engine)
    _migrate(_engine)
