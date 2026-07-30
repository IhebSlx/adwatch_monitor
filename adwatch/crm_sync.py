"""Ingest CRM (Dataverse) records into the local database.

Deliberately transport-agnostic: this module takes plain record dicts and knows
nothing about HOW they were obtained. That lets the same code serve every route
we might use — a Power-Automate flow POSTing to /api/crm/*, a direct Web API
pull once a read-only application user exists, or a one-off hand-carried export.

Identity rule: every CRM row carries the Dataverse GUID, so joins to local
companies go through `Company.crm_id` (see models.Company.crm_id). A record
whose dealer GUID we don't know locally is still STORED — with company_id NULL
and counted as `unmatched` — because dropping it would hide the fact that the
company master is incomplete.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from .db import SessionLocal
from .models import Company, CrmShowroom


def _as_date(value) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def upsert_showrooms(records: list[dict]) -> dict:
    """Mirror `sl_dealer_exposition` rows. Each record:
        {crm_id, dealer_crm_id, product_family, product, installed_on}
    Idempotent on `crm_id`, so re-running a full pull only updates.
    Returns {received, inserted, updated, matched, unmatched, skipped}."""
    received = len(records or [])
    inserted = updated = matched = unmatched = skipped = 0

    with SessionLocal() as s:
        by_crm = {c.crm_id: c.id for c in
                  s.scalars(select(Company).where(Company.crm_id.is_not(None)))}
        existing = {r.crm_id: r for r in s.scalars(select(CrmShowroom))}

        for rec in records or []:
            crm_id = (rec.get("crm_id") or "").strip()
            if not crm_id:
                skipped += 1          # no stable key -> cannot be upserted safely
                continue
            dealer = (rec.get("dealer_crm_id") or "").strip() or None
            company_id = by_crm.get(dealer) if dealer else None
            if dealer:
                if company_id:
                    matched += 1
                else:
                    unmatched += 1

            row = existing.get(crm_id)
            if row is None:
                row = CrmShowroom(crm_id=crm_id)
                s.add(row)
                existing[crm_id] = row
                inserted += 1
            else:
                updated += 1
            row.dealer_crm_id = dealer
            row.company_id = company_id
            row.product_family = (rec.get("product_family") or None)
            row.product = (rec.get("product") or None)
            row.installed_on = _as_date(rec.get("installed_on"))
            row.synced_at = dt.datetime.utcnow()
        s.commit()

    return {"received": received, "inserted": inserted, "updated": updated,
            "matched": matched, "unmatched": unmatched, "skipped": skipped}


def showroom_overview() -> dict:
    """Which product families are exhibited, by how many dealers — plus the
    per-company family sets the cross-sell analysis builds on."""
    from collections import Counter, defaultdict

    with SessionLocal() as s:
        rows = list(s.scalars(select(CrmShowroom)))
        names = {c.id: c.name for c in s.scalars(select(Company))}

    per_company: dict[int, set[str]] = defaultdict(set)
    fam_dealers: dict[str, set] = defaultdict(set)
    products = Counter()
    for r in rows:
        key = r.company_id or f"crm:{r.dealer_crm_id}"
        if r.product_family:
            per_company[key].add(r.product_family)
            fam_dealers[r.product_family].add(key)
        if r.product:
            products[r.product] += 1

    return {
        "rows": len(rows),
        "dealers": len(per_company),
        "matched_dealers": sum(1 for k in per_company if isinstance(k, int)),
        "families": sorted(((f, len(d)) for f, d in fam_dealers.items()),
                           key=lambda kv: -kv[1]),
        "top_products": products.most_common(15),
        "per_company": {k: sorted(v) for k, v in per_company.items() if isinstance(k, int)},
        "company_names": {cid: names.get(cid) for cid in per_company if isinstance(cid, int)},
    }
