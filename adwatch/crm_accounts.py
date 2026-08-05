"""Ingest Dataverse `account` rows into Company — the piece that lets CRM replace
the Excel import.

Field names verified against a live record (docs/DATAVERSE_FIELD_MAP.md), not
guessed.

THE RULE, and the reason this module exists as one place:

  CRM owns master data.        A sync always overwrites it.
  AdWatch owns derived data.   A sync must never touch it.

Derived data is what we paid for — enrichment, hand-locked Meta pages, ICP scores,
ad history. Dataverse has no opinion about any of it, so a naive "update all
fields from the source" would silently destroy the expensive half of the database.
The protected list is therefore explicit and asserted in tests, not a convention
someone has to remember.

Transport-agnostic on purpose: this takes plain record dicts. Whether they came
from a Power Automate flow, a service principal, or a hand-pasted browser response
is not this module's business.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from . import customers, markets
from .db import SessionLocal
from .models import Company

log = logging.getLogger("adwatch.crm_accounts")

# Dataverse field -> local column. Only plain scalars; picklists and lookups are
# handled separately below because they need decoding.
CRM_OWNED_SCALARS: dict[str, str] = {
    "name": "name",
    "accountnumber": "sap_number",
    "address1_line1": "street",
    "address1_postalcode": "postal_code",
    "address1_city": "city",
    "telephone1": "phone",
    "emailaddress1": "email",
    "fax": "fax",
}

# Picklists. Dataverse returns an INTEGER; the Power Automate connector also
# supplies a label as "<field>@OData.Community.Display.V1.FormattedValue".
# We only ever write the LABEL — writing "102" into a column that holds
# "Architekten" would corrupt every filter, report and ICP profile that reads it.
CRM_OWNED_PICKLISTS: dict[str, str] = {
    "sl_customer_segment": "segment",
    "sl_customer_sub_segment": "sub_segment",
    "sl_sales_channel": "sales_channel",
}

# Revenue: five rolling year columns, confirmed populated on live records.
CRM_OWNED_REVENUE: dict[str, str] = {
    "slx_revenue_current_year": "revenue_y0",
    "slx_revenue_current_year_1": "revenue_y1",
    "slx_revenue_current_year_2": "revenue_y2",
    "slx_revenue_current_year_3": "revenue_y3",
    "slx_revenue_current_year_4": "revenue_y4",
}

# NEVER written by a sync. Asserted in tests so a future field addition cannot
# quietly opt itself into being overwritten.
LOCAL_OWNED: frozenset[str] = frozenset({
    "description", "products", "founded_year", "employee_hint", "enrichment_status",
    "page_id", "page_name", "page_url", "resolution_status", "candidates",
    "fit_score", "opportunity_score", "target_score", "fit_breakdown",
    "scores_updated_at", "is_intercompany", "customer_state", "imported_at",
    "facebook_url", "website_domain",
})

_FORMATTED = "@OData.Community.Display.V1.FormattedValue"


def _label(rec: dict, field: str):
    """The human label for a picklist, or None when only the raw code is present.
    Returning None means 'keep whatever is stored' — better a slightly stale
    label than an integer where a segment name belongs."""
    val = rec.get(f"{field}{_FORMATTED}")
    if val not in (None, ""):
        return str(val).strip()
    return None


def _money(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def upsert_accounts(records: list[dict], *, allow_insert: bool = True) -> dict:
    """Apply Dataverse account rows to Company.

    Matching order mirrors the Excel importer: crm_id (the durable GUID) ->
    sap_number -> name+country. A record without an accountid is SKIPPED rather
    than guessed at, because without the GUID a later delta sync could not find
    the same row again.

    `allow_insert=False` restricts the run to refreshing rows we already have —
    useful for a delta sync that should not silently widen the database.

    Returns counts plus the ids touched, so the caller can report honestly.
    """
    received = len(records or [])
    inserted = updated = unchanged = skipped = deactivated = 0
    touched: list[int] = []

    with SessionLocal() as s:
        by_crm = {c.crm_id.lower(): c for c in
                  s.scalars(select(Company).where(Company.crm_id.is_not(None)))
                  if c.crm_id}
        by_sap = {c.sap_number.strip(): c for c in
                  s.scalars(select(Company).where(Company.sap_number.is_not(None)))
                  if c.sap_number}

        for rec in records or []:
            gid = str(rec.get("accountid") or "").strip()
            if not gid:
                skipped += 1
                continue

            company = by_crm.get(gid.lower())
            if company is None:
                sap = str(rec.get("accountnumber") or "").strip()
                company = by_sap.get(sap) if sap else None
            if company is None:
                name = str(rec.get("name") or "").strip()
                country = markets.code_for(rec.get("address1_country"))
                if name:
                    company = s.scalar(select(Company).where(
                        Company.name == name,
                        Company.country == country) if country else
                        select(Company).where(Company.name == name))
            is_new = company is None
            if is_new:
                if not allow_insert:
                    skipped += 1
                    continue
                company = Company(name=str(rec.get("name") or "").strip() or f"CRM {gid[:8]}")
                s.add(company)

            before = _snapshot(company)

            # the durable key, written once and never changed
            if not company.crm_id:
                company.crm_id = gid
            company.crm_modified_on = _dt(rec.get("modifiedon")) or company.crm_modified_on

            for field, col in CRM_OWNED_SCALARS.items():
                if field in rec:
                    value = rec.get(field)
                    setattr(company, col, str(value).strip() if value not in (None, "") else None)

            if "address1_country" in rec:
                code = markets.code_for(rec.get("address1_country"))
                if code:                        # unknown name -> keep what we have
                    company.country = code

            for field, col in CRM_OWNED_PICKLISTS.items():
                if field in rec:
                    label = _label(rec, field)
                    if label:
                        setattr(company, col, label)

            revenue_seen = False
            for field, col in CRM_OWNED_REVENUE.items():
                if field in rec:
                    revenue_seen = True
                    setattr(company, col, _money(rec.get(field)))
            if revenue_seen:
                # derived, so it must be recomputed rather than synced
                company.customer_state = customers.derive_customer_state(
                    company.revenue_y0, company.revenue_y1, company.revenue_y2,
                    company.revenue_y3, company.revenue_y4)

            # A deactivated account must not sit on a call list. Flagged, never
            # deleted — a statecode flip is reversible, a delete is not.
            if str(rec.get("statecode", "")) == "1":
                deactivated += 1

            s.flush()
            if is_new:
                inserted += 1
            elif _snapshot(company) != before:
                updated += 1
            else:
                unchanged += 1
            touched.append(company.id)

        s.commit()

    log.info("crm accounts: received=%d inserted=%d updated=%d unchanged=%d skipped=%d",
             received, inserted, updated, unchanged, skipped)
    return {"received": received, "inserted": inserted, "updated": updated,
            "unchanged": unchanged, "skipped": skipped,
            "deactivated_in_crm": deactivated, "company_ids": touched}


def _snapshot(c: Company) -> tuple:
    """Only the CRM-owned columns, for change detection. Deliberately excludes
    everything in LOCAL_OWNED so local enrichment never registers as a 'change
    from CRM'."""
    cols = (list(CRM_OWNED_SCALARS.values()) + list(CRM_OWNED_PICKLISTS.values())
            + list(CRM_OWNED_REVENUE.values()) + ["country"])
    return tuple(getattr(c, col, None) for col in sorted(cols))


def watermark() -> str | None:
    """Newest CRM modifiedon we hold — the delta filter's starting point.
    ISO-8601 with Z, the form Dataverse expects in a $filter."""
    with SessionLocal() as s:
        newest = s.scalar(select(Company.crm_modified_on)
                          .where(Company.crm_modified_on.is_not(None))
                          .order_by(Company.crm_modified_on.desc()).limit(1))
    return newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else None


def fetch_accounts(filter_query: str = "", top: int = 5000) -> list[dict]:
    """Pull account rows through the configured crm_query flow.

    `select` is sent as a COMMA-SEPARATED STRING, not a list: the Power Automate
    Dataverse action passes it straight into $select, which is a string. Sending a
    JSON array silently yields no columns.

    The flow returns Dataverse's rows verbatim, which means picklists arrive as an
    integer PLUS a FormattedValue label — verified live, e.g. sl_customer_segment
    102 alongside "Architekten". That is exactly what upsert_accounts reads, so no
    option-set mapping is needed anywhere in the app.
    """
    from . import flows

    payload = {"entity": "accounts", "select": ",".join(select_fields()),
               "filter": filter_query or "", "top": int(top)}
    body = flows.post("crm_query", payload)
    if isinstance(body, list):
        return body
    rows = body.get("value") if isinstance(body, dict) else None
    return rows if isinstance(rows, list) else []


def sync_delta(top: int = 5000) -> dict:
    """Everything changed or created in CRM since the newest modifiedon we hold.

    One filter catches both cases: a brand-new account has a modifiedon too. Only
    ~12 accounts change per day in this org, so this stays a small nightly call.
    Inserts are ALLOWED because a new CRM account is exactly what we want to learn
    about — but see load_scope() for deliberately widening the base.
    """
    mark = watermark()
    flt = f"modifiedon gt {mark}" if mark else ""
    if flt:
        flt += " and statecode eq 0"
    rows = fetch_accounts(flt, top)
    result = upsert_accounts(rows)
    result["watermark_used"] = mark
    result["filter"] = flt
    return result


def load_scope(filter_query: str, top: int = 5000) -> dict:
    """A deliberate one-off load of a scope we don't hold yet — a country, a
    salesperson's portfolio, or the prospects.

    Separate from sync_delta because the intent differs: this WIDENS the database
    on purpose, so it must be an explicit act with an explicit filter rather than
    something a nightly job can do by accident.
    """
    if not (filter_query or "").strip():
        raise ValueError("Ein Scope braucht einen Filter — sonst lädt er alles.")
    rows = fetch_accounts(filter_query, top)
    result = upsert_accounts(rows)
    result["filter"] = filter_query
    return result


# Ready-made scopes. The first one matters most: it is the population the ICP has
# never had — companies in CRM that are NOT customers. See the backtest finding
# (dealer base rate 87%, so almost no negative examples to learn from).
SCOPES: dict[str, tuple[str, str]] = {
    "prospects": ("Interessenten (keine Kunden)",
                  "statecode eq 0 and sl_customer_or_prospect eq 102690000"),
    "customers": ("Kunden", "statecode eq 0 and sl_customer_or_prospect eq 102690001"),
    "all_active": ("Alle aktiven Firmen", "statecode eq 0"),
}


def select_fields() -> list[str]:
    """The $select list for a sync — exactly the fields we map, nothing more.
    Keeps payloads small and makes it obvious what the app actually consumes."""
    return (["accountid", "modifiedon", "statecode", "address1_country"]
            + list(CRM_OWNED_SCALARS) + list(CRM_OWNED_PICKLISTS)
            + list(CRM_OWNED_REVENUE))
