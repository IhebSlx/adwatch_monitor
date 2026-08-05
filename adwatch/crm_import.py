"""Bulk-load the full CRM account population plus SAP Beleg revenue.

Why this exists as a separate path from crm_accounts.sync_delta: that one applies
a handful of changed accounts arriving from the Power Automate flow. This one
lands the whole population — ~46,000 active business accounts — in a single pass,
and it is the load that turns the ICP from unusable into usable.

The measurement that forced it: the Excel export the app was built on contained
BUYERS ONLY. Measured against real Belege, 21.9% of the 15,235 active
Handel+Verarbeiter accounts bought since 2023 — not the 87% the old backtest saw.
The missing 78% are the negative examples every look-alike model needs.

Two deliberate decisions:

  * New rows arrive with monitored=False. Ad monitoring costs money per company
    and the team chooses who to watch; the population is here to be *analysed*,
    not automatically fetched. Rows that already existed keep monitored=True.

  * The revenue_y0..y4 snapshot columns are NOT touched. They come from a CRM
    field filled on 2.9% of accounts. The Belege land in their own columns and
    insights.rfm decides which to trust, so nothing that already read the
    snapshot silently changes meaning.

The input is the JSON produced by the Dataverse export (see
docs/DATAVERSE_FIELD_MAP.md): a `cols` list plus row arrays, which keeps a
46,000-row payload around 10 MB instead of the ~60 MB it would be as objects.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from sqlalchemy import select

from . import markets
from .crm_accounts import LOCAL_OWNED
from .db import SessionLocal
from .models import Company

log = logging.getLogger("adwatch.crm_import")

# Option-set labels. Written as LABELS, never codes — an integer in a column that
# holds "Architekten" would corrupt every filter, report and ICP profile.
SEGMENT = {
    100: "Handel", 101: "Verarbeiter", 102: "Architekten", 103: "Baudienstleister",
    104: "Gebäudebetreiber", 105: "Wohnungswirtschaft", 106: "Meinungsbildner",
    107: "Bauzulieferindustrie", 108: "Private Endkunden",
}
SUB_SEGMENT = {
    100000: "Bauelementehandel", 100001: "Holzhandel",
    100002: "Sonnenschutz-/Rolladenhandel", 100003: "Baustoffhandel",
    100004: "Baubeschlaghandel", 100005: "Garten- und Landschaftsbau",
    101000: "Fensterbau", 101001: "Wintergartenbau",
    101002: "Tischler-Schreiner-Zimmerer", 101003: "Metallbau-Schlosser",
    101004: "Ladenbau/Objekteinrichter", 101005: "Balkonbau", 101006: "Glaser",
    102000: "Generalplaner",
}
SALES_CHANNEL = {
    102690000: "Direktvertrieb", 102690001: "Fachhandelsvertrieb",
    102690002: "Objektvertrieb", 102690003: "Architektenberatung",
    102690004: "Linara Vertrieb", 102690005: "SLect",
}
KUNDE_INTERESSENT = {102690000: "Interessent", 102690001: "Kunde"}

# Columns this importer is allowed to write. Asserted against LOCAL_OWNED in the
# tests so a future addition cannot quietly opt itself into being overwritten.
WRITES = frozenset({
    "name", "country", "sap_number", "segment", "sub_segment", "sales_channel",
    "postal_code", "city", "crm_id", "crm_synced_at",
    "beleg_count", "beleg_sum", "beleg_first", "beleg_last", "beleg_by_year",
    "avg_discount", "arch_projects", "arch_won", "arch_won_value",
})

_MIN_SANE_ROWS = 500  # a truncated download must not be able to wipe the base


def _date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _norm_name(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def load_export(path: str | Path) -> dict:
    """Parse and sanity-check the export file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cols, rows = data.get("cols"), data.get("rows")
    if not cols or not rows:
        raise ValueError("export has no cols/rows")
    if len(rows) < _MIN_SANE_ROWS:
        raise ValueError(
            f"export has only {len(rows)} rows — refusing to treat a partial "
            f"download as the full population")
    ix = {c: i for i, c in enumerate(cols)}
    return {"ix": ix, "rows": rows, "meta": {
        k: data.get(k) for k in ("generated", "source", "belege_window_from")}}


def import_accounts(path: str | Path, *, allow_insert: bool = True) -> dict:
    """Upsert the export into Company. Matching order crm_id -> sap -> name+country,
    the same order used everywhere else in the app."""
    parsed = load_export(path)
    ix, rows = parsed["ix"], parsed["rows"]
    g = lambda r, c: r[ix[c]] if c in ix else None  # noqa: E731

    stats = {"total": len(rows), "updated": 0, "inserted": 0, "skipped_no_id": 0,
             "renamed_collision": 0, "with_belege": 0, "unknown_country": 0}
    now = dt.datetime.utcnow()

    with SessionLocal() as s:
        by_crm, by_sap, by_name = {}, {}, {}
        for c in s.scalars(select(Company)):
            if c.crm_id:
                by_crm[c.crm_id.strip().lower()] = c
            if c.sap_number:
                by_sap[c.sap_number.strip().lstrip("0")] = c
            by_name.setdefault((_norm_name(c.name), (c.country or "").upper()), c)
        # Names already taken, so a bulk insert can never trip the UNIQUE index
        # on companies.name. 411 CRM names legitimately repeat across branches.
        taken = {_norm_name(c.name) for c in by_name.values()}
        taken |= {_norm_name(c.name) for c in by_crm.values()}

        for r in rows:
            crm_id = (g(r, "crm_id") or "").strip()
            if not crm_id:
                stats["skipped_no_id"] += 1
                continue
            name = " ".join((g(r, "name") or "").split())
            if not name:
                stats["skipped_no_id"] += 1
                continue

            raw_country = g(r, "country") or ""
            code = markets.code_for(raw_country)
            if code is None and raw_country:
                stats["unknown_country"] += 1
            sap = (g(r, "accountnumber") or "").strip() or None

            c = (by_crm.get(crm_id.lower())
                 or (by_sap.get(sap.lstrip("0")) if sap else None)
                 or by_name.get((_norm_name(name), (code or "").upper())))

            if c is None:
                if not allow_insert:
                    continue
                unique = name
                if _norm_name(unique) in taken:
                    # Disambiguate rather than drop the row: two branches of the
                    # same firm are both real. The city is what a human would add.
                    city = (g(r, "city") or "").strip()
                    suffix = city or crm_id[:8]
                    unique = f"{name} · {suffix}"
                    n = 2
                    while _norm_name(unique) in taken:
                        unique = f"{name} · {suffix} ({n})"
                        n += 1
                    stats["renamed_collision"] += 1
                taken.add(_norm_name(unique))
                c = Company(name=unique, country=code or "DE",
                            source="crm", monitored=False,
                            resolution_status="pending")
                s.add(c)
                stats["inserted"] += 1
            else:
                stats["updated"] += 1
                if code:
                    c.country = code

            c.crm_id = crm_id
            if sap:
                c.sap_number = sap
            c.segment = SEGMENT.get(g(r, "segment")) or c.segment
            c.sub_segment = SUB_SEGMENT.get(g(r, "sub_segment")) or c.sub_segment
            c.sales_channel = SALES_CHANNEL.get(g(r, "sales_channel")) or c.sales_channel
            c.postal_code = (g(r, "postal_code") or "").strip() or c.postal_code
            c.city = (g(r, "city") or "").strip() or c.city

            # ---- Belege ----
            n_bel = int(g(r, "beleg_count") or 0)
            c.beleg_count = n_bel
            c.beleg_sum = float(g(r, "beleg_sum") or 0)
            c.beleg_first = _date(g(r, "beleg_first"))
            c.beleg_last = _date(g(r, "beleg_last"))
            years = {}
            for y in (2023, 2024, 2025, 2026):
                v = g(r, f"rev_{y}")
                if v:
                    years[str(y)] = float(v)
            c.beleg_by_year = years or None
            disc = g(r, "avg_discount")
            c.avg_discount = float(disc) if disc not in (None, "") else None
            if n_bel:
                stats["with_belege"] += 1

            c.arch_projects = int(g(r, "arch_projects") or 0)
            c.arch_won = int(g(r, "arch_won") or 0)
            c.arch_won_value = float(g(r, "arch_won_value") or 0)
            c.crm_synced_at = now

        s.commit()

    log.info("crm_import: %s", stats)
    return stats


def backfill_websites(path: str | Path) -> dict:
    """Copy CRM's websiteurl into website_domain where we have nothing.

    23,941 CRM accounts already carry a website. The app was paying Serper to
    discover websites it could have read for free, and website_domain is what the
    Google-Ads resolver and the enrichment crawler both key off. Only ever fills
    a BLANK — website_domain is in LOCAL_OWNED, so a domain a human or the
    validator established is never overwritten by a CRM value.
    """
    parsed = load_export(path)
    ix, rows = parsed["ix"], parsed["rows"]
    if "website" not in ix:
        return {"filled": 0, "reason": "export has no website column"}

    def domain(url: str) -> str | None:
        u = (url or "").strip().lower()
        if not u or "." not in u:
            return None
        for p in ("https://", "http://"):
            if u.startswith(p):
                u = u[len(p):]
        u = u.split("/")[0].split("?")[0].split(":")[0]
        if u.startswith("www."):
            u = u[4:]
        return u or None

    filled = 0
    with SessionLocal() as s:
        by_crm = {c.crm_id.strip().lower(): c
                  for c in s.scalars(select(Company).where(Company.crm_id.is_not(None)))
                  if c.crm_id}
        for r in rows:
            crm_id = (r[ix["crm_id"]] or "").strip().lower()
            c = by_crm.get(crm_id)
            if c is None or c.website_domain:
                continue
            d = domain(r[ix["website"]])
            if d:
                c.website_domain = d
                filled += 1
        s.commit()
    log.info("crm_import.backfill_websites: filled %s", filled)
    return {"filled": filled}
