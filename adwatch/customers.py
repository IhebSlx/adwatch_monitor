"""Company master data: Excel import + filtered explorer + export.

A company IS a customer — there is no separate entity (see models.Company
docstring). This module owns the bulk operations (import thousands of rows,
filter/sort/paginate/select across them, export); per-row identity work
(linking a Meta/Google page, confirming a match) lives in identity.resolver
and services.py, same as before the merge. A future direct-DB feed would
reuse `upsert_companies()` unchanged — the Excel reader is just one way to
produce the record dicts it takes.

Fetching is opt-in per row simply by being fetched — there's no separate
"promote to tracking" step. A company with resolution_status='pending' has
just never been fetched yet; the first fetch (via the normal pipeline)
resolves its page(s) and the row updates in place.
"""
from __future__ import annotations

import datetime as dt
import io
import re

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from . import config
from .db import SessionLocal
from .models import Company

# ---------------------------------------------------------------------------
# Column mapping — tolerant to whitespace/case so slightly different exports
# still line up. Each model field lists the header variants that map to it.
# ---------------------------------------------------------------------------
_FIELD_HEADERS: dict[str, tuple[str, ...]] = {
    "sap_number": ("sap nummer", "sap-nummer", "sapnummer", "sap"),
    "name": ("firmenname", "firma", "name"),
    "kv": ("kv",),
    "segment": ("kundensegment",),
    "sub_segment": ("kundenuntersegment",),
    "sales_channel": ("vertriebsweg",),
    "street": ("straße", "strasse"),
    "postal_code": ("adresse 1: postleitzahl", "postleitzahl", "plz"),
    "city": ("ort",),
    "country": ("land",),
    "phone": ("telefon 1", "telefon", "tel"),
    "email": ("e-mail", "email", "e mail"),
    "fax": ("fax",),
    "website_domain": ("website", "webseite", "web"),
    "revenue_y0": ("umsatz aktuelles jahr",),
    "revenue_y1": ("umsatz aktuelles jahr -1", "umsatz aktuelles jahr-1"),
    "revenue_y2": ("umsatz aktuelles jahr -2", "umsatz aktuelles jahr-2"),
    "revenue_y3": ("umsatz aktuelles jahr -3", "umsatz aktuelles jahr-3"),
    "revenue_y4": ("umsatz aktuelles jahr -4", "umsatz aktuelles jahr-4"),
}
_REVENUE_FIELDS = ("revenue_y0", "revenue_y1", "revenue_y2", "revenue_y3", "revenue_y4")
# Sort keys the UI is allowed to request -> model column.
SORTABLE = {
    "name": Company.name, "sap_number": Company.sap_number,
    "segment": Company.segment, "sub_segment": Company.sub_segment,
    "sales_channel": Company.sales_channel, "city": Company.city, "country": Company.country,
    "revenue_y0": Company.revenue_y0, "revenue_y1": Company.revenue_y1,
    "revenue_y2": Company.revenue_y2, "revenue_y3": Company.revenue_y3,
    "revenue_y4": Company.revenue_y4, "imported_at": Company.imported_at,
    "resolution_status": Company.resolution_status,
}


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _parse_number(value) -> float | None:
    """Parse a revenue cell that may already be numeric, or a German-formatted
    string ('1.234.567,89' -> 1234567.89). Returns None for blanks/garbage."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^0-9,.\-]", "", str(value))  # drop currency symbols, spaces, etc.
    if not s or s in ("-", ".", ","):
        return None
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        s = s.replace(".", "").replace(",", ".")          # dot=thousands, comma=decimal
    elif has_comma:
        s = s.replace(",", ".")                            # comma=decimal
    elif has_dot:
        # Lone dot: thousands unless it clearly reads as a decimal (e.g. '1.5').
        tail = s.split(".")[-1]
        if s.count(".") > 1 or len(tail) == 3:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _clean_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def parse_excel(file_bytes: bytes) -> tuple[list[dict], list[str]]:
    """Read the first worksheet into record dicts. Returns (records, warnings).
    Raises ValueError if the SAP-Nummer column can't be found at all."""
    import openpyxl  # local import: only needed at upload time
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = next(rows)
    except StopIteration:
        raise ValueError("The uploaded file is empty.")

    # Map each column index to a model field via the normalized header.
    norm_to_field = {}
    for field, variants in _FIELD_HEADERS.items():
        norm_to_field.update({v: field for v in variants})
    col_field: dict[int, str] = {}
    for idx, cell in enumerate(header):
        field = norm_to_field.get(_norm(cell))
        if field and field not in col_field.values():
            col_field[idx] = field

    if "sap_number" not in col_field.values():
        raise ValueError("Could not find a 'SAP Nummer' column in the file's header row.")

    records, warnings = [], []
    for line_no, row in enumerate(rows, start=2):
        rec: dict = {}
        for idx, field in col_field.items():
            value = row[idx] if idx < len(row) else None
            rec[field] = _parse_number(value) if field in _REVENUE_FIELDS else _clean_str(value)
        if not rec.get("sap_number"):
            continue  # silently skip rows with no SAP key (blank trailing rows, etc.)
        records.append(rec)

    missing = [f for f in _FIELD_HEADERS if f not in col_field.values()]
    if missing:
        warnings.append(f"{len(missing)} column(s) not found and left blank: {', '.join(missing)}")
    return records, warnings


def upsert_companies(records: list[dict]) -> dict:
    """Insert new companies / update existing ones, matched on sap_number.
    A record whose sap_number isn't known yet but whose NAME exactly matches
    an existing (e.g. hand-added, pre-Excel) company folds into that row
    instead of erroring on the name-uniqueness constraint. Never overwrites
    `name` on an existing row (renaming could break Meta page resolution) or
    a `website_domain`/`country` that's already set. Ad-tracking fields
    (page_id, resolution_status, ...) are never touched by import."""
    now = dt.datetime.utcnow()
    inserted = updated = skipped_no_name = name_collisions = 0
    with SessionLocal() as s:
        by_sap = {c.sap_number: c for c in
                  s.scalars(select(Company).where(Company.sap_number.is_not(None)))}
        by_name = {c.name: c for c in s.scalars(select(Company))}

        for rec in records:
            sap = rec.get("sap_number")
            name = (rec.get("name") or "").strip()
            row = by_sap.get(sap) if sap else None
            if row is None and name:
                row = by_name.get(name)   # fold into a pre-existing un-SAP'd row by exact name

            if row is None:
                if not name:
                    skipped_no_name += 1
                    continue
                row = Company(name=name, source="meta", resolution_status="pending")
                s.add(row)
                try:
                    s.flush()
                except IntegrityError:
                    # Genuine duplicate company name across two different SAP
                    # numbers (plausible at this scale) — disambiguate rather
                    # than dropping the row or crashing the whole import.
                    s.rollback()
                    by_sap = {c.sap_number: c for c in
                              s.scalars(select(Company).where(Company.sap_number.is_not(None)))}
                    by_name = {c.name: c for c in s.scalars(select(Company))}
                    disambiguated = f"{name} ({sap})"
                    row = Company(name=disambiguated, source="meta", resolution_status="pending")
                    s.add(row)
                    s.flush()
                    name_collisions += 1
                by_sap[sap] = row
                by_name[row.name] = row
                inserted += 1
            else:
                updated += 1

            for field, value in rec.items():
                if field in ("sap_number", "name"):
                    if field == "sap_number" and value and row.sap_number != value:
                        row.sap_number = value
                    continue
                if field == "website_domain":
                    if value and not row.website_domain:
                        row.website_domain = value
                    continue
                if field == "country":
                    if value and len(value) <= 3:
                        row.country = value
                    continue
                setattr(row, field, value)
            row.imported_at = now
        s.commit()
    return {"received": len(records), "inserted": inserted, "updated": updated,
           "skipped_no_name": skipped_no_name, "name_collisions": name_collisions}


def import_excel(file_bytes: bytes) -> dict:
    records, warnings = parse_excel(file_bytes)
    summary = upsert_companies(records)
    summary["warnings"] = warnings
    return summary


# ---------------------------------------------------------------------------
# Querying / filtering
# ---------------------------------------------------------------------------

def _apply_filters(stmt, f: dict):
    # Explicit selection (hand-picked company ids, e.g. "report for selected"):
    # restrict to exactly these. Kept first so it composes with any other
    # filters, though the selection flow normally passes ids alone.
    if f.get("ids"):
        stmt = stmt.where(Company.id.in_([int(i) for i in f["ids"]]))
    if f.get("q"):
        like = f"%{f['q'].strip()}%"
        stmt = stmt.where(or_(Company.name.ilike(like), Company.sap_number.ilike(like)))
    for field, col in (("kv", Company.kv), ("segment", Company.segment),
                       ("sub_segment", Company.sub_segment),
                       ("sales_channel", Company.sales_channel), ("country", Company.country)):
        values = f.get(field)
        if values:
            stmt = stmt.where(col.in_(values) if isinstance(values, list) else col == values)
    for field, col in (("exclude_kv", Company.kv), ("exclude_segment", Company.segment),
                       ("exclude_sub_segment", Company.sub_segment)):
        values = f.get(field)
        if values:
            # NULL-safe: a company with no value in this column isn't "one of
            # the excluded ones", so it stays in the result (unlike a bare
            # NOT IN, which SQL silently drops NULLs from).
            stmt = stmt.where(or_(col.is_(None), col.not_in(values)))
    if f.get("resolution_status"):
        values = f["resolution_status"]
        stmt = stmt.where(Company.resolution_status.in_(values)
                          if isinstance(values, list) else Company.resolution_status == values)
    if f.get("has_website"):
        stmt = stmt.where(Company.website_domain.is_not(None), Company.website_domain != "")
    # Identity fetch-readiness: a numeric page_id is what the Ad Library scrape
    # needs — "without" surfaces the ⚠ no-id rows (confirmed but not fetch-ready).
    if f.get("page_id_state") == "with":
        stmt = stmt.where(Company.page_id.is_not(None), Company.page_id != "")
    elif f.get("page_id_state") == "without":
        stmt = stmt.where(or_(Company.page_id.is_(None), Company.page_id == ""))
    if f.get("tracked") is not None:
        stmt = stmt.where(Company.resolution_status != "pending") if f["tracked"] \
            else stmt.where(Company.resolution_status == "pending")
    # Ad activity — based on the LATEST fetched week:
    #   active: latest week sums to >=1 active ad (the win-back signal)
    #   any:    any real ad ever on record (active or ended)
    #   none:   fetched at least once but no active ad in the latest week
    # Companies never fetched have no metric, so they match none of these.
    aa = f.get("ad_activity")
    if aa in ("active", "any", "none"):
        from sqlalchemy import and_ as _and
        from .models import Ad as _Ad, CollectionRun as _CR, WeeklyCompanyMetric as _WCM
        _latest = (select(_WCM.company_id, func.max(_WCM.week_start).label("wk"))
                   .group_by(_WCM.company_id).subquery())
        _running = (select(_WCM.company_id)
                    .join(_latest, _and(_WCM.company_id == _latest.c.company_id,
                                        _WCM.week_start == _latest.c.wk))
                    .group_by(_WCM.company_id)
                    .having(func.sum(_WCM.total_active_ads) > 0))
        if aa == "active":
            stmt = stmt.where(Company.id.in_(_running))
        elif aa == "any":
            # ever advertised: a real ad on record (incl. ended) OR any week with
            # active ads. Union guarantees "any" is a superset of "active".
            _real_ad = (select(_CR.company_id).join(_Ad, _Ad.run_id == _CR.id)
                        .where(_Ad.external_ad_id.is_not(None)))
            _ever_active = select(_WCM.company_id).where(_WCM.total_active_ads > 0)
            stmt = stmt.where(or_(Company.id.in_(_real_ad), Company.id.in_(_ever_active)))
        else:  # none — fetched but not currently advertising
            stmt = stmt.where(Company.id.in_(select(_WCM.company_id)),
                              Company.id.not_in(_running))
    if f.get("revenue_min") is not None:
        stmt = stmt.where(func.coalesce(Company.revenue_y0, 0) >= f["revenue_min"])
    if f.get("revenue_max") is not None:
        stmt = stmt.where(func.coalesce(Company.revenue_y0, 0) <= f["revenue_max"])
    if f.get("revenue_history"):
        y0 = func.coalesce(Company.revenue_y0, 0)
        prior_any = or_(*(func.coalesce(getattr(Company, f"revenue_y{i}"), 0) > 0 for i in (1, 2, 3, 4)))
        hist = f["revenue_history"]
        if hist == "lapsed":       # bought in a prior year, nothing this year — win-back candidates
            stmt = stmt.where(y0 <= 0, prior_any)
        elif hist == "new":        # buying this year, no history before it
            stmt = stmt.where(y0 > 0, ~prior_any)
        elif hist == "any":        # revenue in at least one year, current or past
            stmt = stmt.where(or_(y0 > 0, prior_any))
        elif hist == "never":      # no revenue recorded in any year
            stmt = stmt.where(y0 <= 0, ~prior_any)
    return stmt


_REVENUE_SORT_KEYS = {"revenue_y0", "revenue_y1", "revenue_y2", "revenue_y3", "revenue_y4"}


def _apply_sort(stmt, sort: str | None, direction: str):
    key = sort or "name"
    col = SORTABLE.get(key, Company.name)
    if key in _REVENUE_SORT_KEYS:
        # Missing revenue counts as €0 here too, so it sorts in place rather
        # than always trailing behind real numbers.
        col = func.coalesce(col, 0)
    else:
        # NULLs last regardless of direction, so blanks never top a desc sort.
        stmt = stmt.order_by(col.is_(None))
    return stmt.order_by(col.desc() if direction == "desc" else col.asc())


def _to_dict(c: Company) -> dict:
    return {
        "id": c.id, "sap_number": c.sap_number, "name": c.name,
        "kv": c.kv, "segment": c.segment, "sub_segment": c.sub_segment,
        "sales_channel": c.sales_channel, "street": c.street, "postal_code": c.postal_code,
        "city": c.city, "country": c.country, "phone": c.phone, "email": c.email,
        "fax": c.fax, "website_domain": c.website_domain,
        "revenue_y0": c.revenue_y0, "revenue_y1": c.revenue_y1, "revenue_y2": c.revenue_y2,
        "revenue_y3": c.revenue_y3, "revenue_y4": c.revenue_y4,
        "resolution_status": c.resolution_status, "tracked": c.resolution_status != "pending",
        "page_id": c.page_id, "page_name": c.page_name, "page_url": c.page_url,
    }


def get_company(company_id: int) -> dict | None:
    """One company's full master-data row — the company drawer's data source
    when the company isn't in the Explorer's currently loaded page (e.g. the
    drawer opened from the dashboard or the divergence list). Includes the
    identity `candidates` blob (only carried on the single-company fetch, not
    the list, to keep list payloads lean) so the drawer can offer a pick-list."""
    with SessionLocal() as s:
        c = s.get(Company, company_id)
        if not c:
            return None
        d = _to_dict(c)
        d["candidates"] = c.candidates or []
        return d


def query_companies(filters: dict, sort: str | None = None, direction: str = "asc",
                    page: int = 1, page_size: int = 50) -> dict:
    with SessionLocal() as s:
        base = _apply_filters(select(Company), filters)
        total = s.scalar(select(func.count()).select_from(base.subquery()))
        stmt = _apply_sort(base, sort, direction).limit(page_size).offset((page - 1) * page_size)
        rows = [_to_dict(c) for c in s.scalars(stmt)]
        return {"total": total, "page": page, "page_size": page_size, "rows": rows}


def top_ids(filters: dict, sort: str | None, direction: str, n: int) -> list[int]:
    """The first N company ids under the current filter+sort — powers the
    'select top N' action so a subset is chosen across the WHOLE sorted set,
    not just the visible page."""
    with SessionLocal() as s:
        stmt = _apply_sort(_apply_filters(select(Company.id), filters), sort, direction).limit(n)
        return list(s.scalars(stmt))


def filter_options() -> dict:
    """Distinct values for the filter dropdowns."""
    with SessionLocal() as s:
        def distinct(col):
            return sorted(v for v in s.scalars(select(col).distinct()) if v)
        return {
            "kv": distinct(Company.kv),
            "segment": distinct(Company.segment),
            "sub_segment": distinct(Company.sub_segment),
            "sales_channel": distinct(Company.sales_channel),
            "country": distinct(Company.country),
        }


def count_companies() -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(Company)) or 0


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

_EXPORT_COLUMNS = [
    ("sap_number", "SAP Nummer"), ("name", "Firmenname"), ("kv", "KV"),
    ("segment", "Kundensegment"), ("sub_segment", "Kundenuntersegment"),
    ("sales_channel", "Vertriebsweg"), ("street", "Straße"), ("postal_code", "Postleitzahl"),
    ("city", "Ort"), ("country", "Land"), ("phone", "Telefon 1"), ("email", "E-Mail"),
    ("fax", "Fax"), ("website_domain", "Website"),
    ("revenue_y0", "Umsatz aktuelles Jahr"), ("revenue_y1", "Umsatz aktuelles Jahr -1"),
    ("revenue_y2", "Umsatz aktuelles Jahr -2"), ("revenue_y3", "Umsatz aktuelles Jahr -3"),
    ("revenue_y4", "Umsatz aktuelles Jahr -4"),
]


def export_xlsx(filters: dict | None = None, ids: list[int] | None = None,
                sort: str | None = None, direction: str = "asc") -> bytes:
    """Export either an explicit selection (ids) or the whole filtered set,
    as a .xlsx matching the original column layout."""
    import openpyxl
    with SessionLocal() as s:
        stmt = select(Company)
        if ids is not None:
            stmt = stmt.where(Company.id.in_(ids))
        else:
            stmt = _apply_filters(stmt, filters or {})
        stmt = _apply_sort(stmt, sort, direction)
        companies = list(s.scalars(stmt))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Companies"
    ws.append([label for _, label in _EXPORT_COLUMNS])
    for c in companies:
        d = _to_dict(c)
        ws.append([d.get(field) for field, _ in _EXPORT_COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
