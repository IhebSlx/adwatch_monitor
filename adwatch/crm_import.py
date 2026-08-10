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
# All 49 codes, read from the live option set. The first version of this map had
# only the 14 Handel/Verarbeiter codes, which silently left sub_segment NULL for
# every architect and Baudienstleister — i.e. for more than half the population,
# and for the single best ICP feature there is. Nothing errored; the data was just
# quietly absent, which is why the ICP's coverage check caught it and a spot
# check would not have.
SUB_SEGMENT = {
    100000: "Bauelementehandel", 100001: "Holzhandel",
    100002: "Sonnenschutz-/Rolladenhandel", 100003: "Baustoffhandel",
    100004: "Baubeschlaghandel", 100005: "Garten- und Landschaftsbau",
    101000: "Fensterbau", 101001: "Wintergartenbau",
    101002: "Tischler-Schreiner-Zimmerer", 101003: "Metallbau-Schlosser",
    101004: "Ladenbau/Objekteinrichter", 101005: "Balkonbau", 101006: "Glaser",
    102000: "Generalplaner", 102001: "Architekturbüro",
    102002: "Fachplanungsbüro", 102003: "Landschaftsplaner",
    102004: "Innenarchitektur", 102005: "Bauakustiker",
    103000: "Projektentwickler", 103001: "Immobilienunterneh./Investor",
    103002: "Generalunternehmen/-übern.", 103003: "Fertighaushersteller",
    103004: "Bauunternehmen", 103005: "Bauträger",
    103006: "Facility Management", 103007: "Kooperationspartner",
    104000: "Banken/Verwaltungen", 104001: "Soziale Einrichtungen",
    104002: "Öffentl.Hand/kirchl.Trägerschaft", 104003: "Industrieunternehmen",
    104004: "Hotel/Gastro", 104005: "Handelsunternehmen",
    104006: "Sportstätten / Vereine",
    105000: "Genossenschaft/Verein", 105001: "Eigentümer-Gemeinschaft",
    105002: "Privatwirtschaftl. Unternehmen", 105003: "Kommunale Gesellschaft",
    105004: "Haus- und Wohnungsverwalter",
    106000: "Verbände / Innung", 106001: "Portalbetreiber (Heinze..)",
    106002: "Presse / Print", 106003: "Bildungstätte",
    106004: "Prüfinstitute", 106005: "Sachverständiger",
    107000: "Marktmitspieler", 107001: "Profil-Systemgeber",
    107002: "Sonstige Bauzulieferindustrie",
    108000: "Student",
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


OPP_STATUS = {
    1: "Planungsphase", 2: "Angebotsphase", 3: "Gewonnen",
    100000000: "Auftragsphase", 100000001: "Abschlussphase",
    100000002: "Teilauftrag", 100000003: "Keine Baugenehmigung",
    100000009: "Zu teuer",
    102690000: "Zugehörige VC gewonnen",
    425390000: "Wettbewerb", 425390002: "Selbst abgelehnt",
    425390004: "Duplikat", 425390006: "Kunde hat den Auftrag nicht erhalten",
    852850000: "Kein Feedback vom Kunden", 852850001: "Zu lange Lieferzeit",
    852850002: "Kein Interesse mehr", 852850003: "Projekt umgeplant",
    852850004: "Massenschließung",
}

# Reasons a human could still do something about. Measured: these carry EUR 557M
# of the EUR 1,123M lost volume. The rest is not winnable and including it makes
# any "we are losing deals" figure meaningless.
ADDRESSABLE_LOSSES = frozenset({
    "Kein Feedback vom Kunden", "Kein Interesse mehr", "Zu teuer", "Wettbewerb",
})

STATE = {0: "offen", 1: "gewonnen", 2: "verloren"}

# Option sets read from the live metadata — stored as LABELS so every filter,
# export and report reads German words instead of integers.
TYPE_OF_USE = {
    100: "Ausstellung", 200: "Bildung",
    300: "Einzelhandel/Dienstleistung/Ladenketten", 400: "Gesundheit und Pflege",
    500: "Hotel- und Gastgewerbe", 600: "Kultur und Sport",
    700: "Sonstige/nicht bekannt", 800: "Verwaltungs- und Bürogebäude",
    900: "Wohnen",
}
VC_TYPE = {300523001: "Vertriebs-VC", 300523002: "Architekten-VC"}
DEALER_STATUS = {
    102690000: "Neu", 102690001: "Angenommen", 102690002: "Erstkontakt erfolgt",
    102690006: "Termin vereinbart", 102690008: "Angebot erstellen",
    102690009: "Auftrag erhalten", 102690005: "Rückgabe", 425390000: "Verloren",
}
ORIGIN = {
    425390000: "von Solarlux", 425390008: "vom Architekten",
    425390001: "vom Händler", 425390002: "aus Online Konfigurator",
    425390009: "vom Objektkunden",
    425390003: "von Linara Ahaus", 425390004: "von Linara Augsburg",
    425390005: "von Linara Kaufbeuren", 425390006: "von Linara Vechta",
    425390007: "von Linara OWL", 425390010: "von Linara Berlin-Brandenburg",
    425390012: "von Linara Münsterland",
}
RATING = {1: "Sehr aussichtsreich", 2: "Aussichtsreich", 3: "Wenig aussichtsreich"}
PRIORITY = {0: "Hoch", 1: "Normal", 2: "Niedrig"}


def import_order_events(path: str | Path, *, replace_window: bool = False) -> dict:
    """Belege -> CrmOrderEvent, collapsing each company+day into one purchase event.

    Belege are not orders: 73,112 documents in the 2023+ window are 54,534 events.
    A multi-line order is issued as several documents, so a cadence computed on raw
    Belege reads as 0-3 days for a large dealer and churn detection stops working.
    """
    from .models import CrmOrderEvent
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cols = data.get("cols") or data.get("belege_cols")
    rows = data.get("rows") or data.get("belege")
    if not cols or not rows:
        raise ValueError("export has no Beleg rows")
    ix = {c: i for i, c in enumerate(cols)}

    agg: dict[tuple[str, str], list] = {}
    for r in rows:
        cust = (r[ix["cust"]] or "").strip().lower()
        day = str(r[ix["date"]] or "")[:10]
        if not cust or not day:
            continue
        a = agg.setdefault((cust, day), [0.0, 0, None, []])
        a[0] += float(r[ix["amount"]] or 0)
        a[1] += 1
        if a[2] is None and ix.get("channel") is not None:
            a[2] = r[ix["channel"]] or None
        d = r[ix["discount"]] if "discount" in ix else None
        if d is not None:
            a[3].append(float(d))

    stats = {"belege": len(rows), "events": 0, "unmatched": 0}
    with SessionLocal() as s:
        by_crm = {c.crm_id.strip().lower(): c.id for c in
                  s.scalars(select(Company).where(Company.crm_id.is_not(None)))
                  if c.crm_id}
        days = {d for _, d in agg}
        if replace_window and days:
            s.execute(CrmOrderEvent.__table__.delete().where(
                CrmOrderEvent.order_date >= dt.date.fromisoformat(min(days)),
                CrmOrderEvent.order_date <= dt.date.fromisoformat(max(days))))
        existing = {(cid, d) for cid, d in s.execute(
            select(CrmOrderEvent.company_id, CrmOrderEvent.order_date))}
        pending = []
        for (cust, day), (amt, n, ch, disc) in agg.items():
            cid = by_crm.get(cust)
            if cid is None:
                stats["unmatched"] += 1      # deactivated or Private-Endkunde account
                continue
            date = dt.date.fromisoformat(day)
            if (cid, date) in existing:
                continue
            pending.append(dict(company_id=cid, order_date=date,
                                amount=round(amt, 2), beleg_count=n, channel=ch,
                                discount=round(sum(disc) / len(disc), 3) if disc else None))
        for i in range(0, len(pending), 5000):
            s.bulk_insert_mappings(CrmOrderEvent, pending[i:i + 5000])
        stats["events"] = len(pending)
        s.commit()
    log.info("crm_import.import_order_events: %s", stats)
    return stats


def import_opportunities(path: str | Path) -> dict:
    """Opportunities -> CrmOpportunity, with the loss reason decoded."""
    from .models import CrmOpportunity
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ix = {c: i for i, c in enumerate(data["cols"])}
    rows = data["rows"]
    g = lambda r, c: r[ix[c]] if c in ix else None  # noqa: E731

    def _ts(v):
        if not v:
            return None
        try:
            return dt.datetime.fromisoformat(str(v)[:10])
        except ValueError:
            return None

    stats = {"total": len(rows), "inserted": 0, "lost": 0, "addressable": 0}
    now = dt.datetime.utcnow()
    with SessionLocal() as s:
        s.execute(CrmOpportunity.__table__.delete())
        pending = []
        for r in rows:
            state = STATE.get(g(r, "statecode"))
            label = OPP_STATUS.get(g(r, "statuscode"))
            lost = label if state == "verloren" else None
            if lost:
                stats["lost"] += 1
                if lost in ADDRESSABLE_LOSSES:
                    stats["addressable"] += 1
            # Projekt-Verknüpfung: the primary VC is the Objekt itself. A VC
            # without a primary is its own project (anchor = its own GUID), so
            # grouping by project_id always yields complete projects.
            own_guid = (g(r, "opp_id") or "").strip().lower() or None
            primary = (g(r, "primary_id") or "").strip().lower() or None
            pending.append(dict(
                crm_id=(g(r, "number") or "") or f"row-{len(pending)}",
                number=g(r, "number") or None,
                opportunity_guid=own_guid,
                project_id=primary or own_guid,
                parent_account_crm_id=(g(r, "account") or "").strip().lower() or None,
                architect_crm_id=(g(r, "architect") or "").strip().lower() or None,
                end_customer_crm_id=(g(r, "endcustomer") or "").strip().lower() or None,
                sales_channel=SALES_CHANNEL.get(g(r, "channel")),
                state=state, lost_reason=lost,
                order_value=float(g(r, "order_value") or 0) or None,
                estimated_value=float(g(r, "est_value") or 0) or None,
                end_customer_budget=float(g(r, "endcust_budget") or 0) or None,
                created_on=_ts(g(r, "created")), closed_on=_ts(g(r, "closed")),
                # decoded labels; absent in older export files -> None
                type_of_use=TYPE_OF_USE.get(g(r, "type_of_use")),
                vc_type=VC_TYPE.get(g(r, "vc_type")),
                dealer_status=DEALER_STATUS.get(g(r, "dealer_status")),
                origin=ORIGIN.get(g(r, "origin")),
                rating=RATING.get(g(r, "rating")),
                priority=PRIORITY.get(g(r, "priority")),
                sales_stage=(str(g(r, "sales_stage")) if g(r, "sales_stage") is not None else None),
                vr_presented=(bool(g(r, "vr_presented"))
                              if g(r, "vr_presented") is not None else None),
                business_unit=(g(r, "business_unit") or "").strip() or None,
                total_amount=float(g(r, "total_amount") or 0) or None,
                estimated_close=_date(g(r, "est_close")),
                project_name=(g(r, "project_name") or g(r, "name") or "").strip()[:200] or None,
                synced_at=now))
        for i in range(0, len(pending), 5000):
            s.bulk_insert_mappings(CrmOpportunity, pending[i:i + 5000])
        stats["inserted"] = len(pending)
        s.commit()
    log.info("crm_import.import_opportunities: %s", stats)
    return stats


def import_opportunity_links(path: str | Path) -> dict:
    """Belege and Angebote that name their Verkaufschance -> per-opportunity totals.

    This is the join that was missing. `ax_sap_order.ax_opportunityid` is set on
    23,955 Belege and `ax_sap_quote.ax_opportunityid` on 35,856 quotes, so the
    chain Angebot → Auftrag → **fakturierter Beleg** finally closes at PROJECT
    level. Before this, revenue was only known per company, which made the
    conversion rate a blunt company-wide ratio; now a project's quoted value can
    be compared with what was actually invoiced against it.

    Stored as aggregates on CrmOpportunity (plus the SAP document numbers, so a
    figure can always be traced back to its Belege in SAP).
    """
    from .models import CrmOpportunity
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    bi = {c: i for i, c in enumerate(data["belege_cols"])}
    qi = {c: i for i, c in enumerate(data["quote_cols"])}

    inv: dict[str, list] = {}
    for r in data["belege"]:
        opp = (r[bi["opp"]] or "").strip().lower()
        if not opp:
            continue
        a = inv.setdefault(opp, [0.0, 0, []])
        a[0] += float(r[bi["amount"]] or 0)
        a[1] += 1
        nr = (r[bi["sap_nr"]] or "").strip()
        if nr and len(a[2]) < 20:
            a[2].append(nr)
    quo: dict[str, list] = {}
    for r in data["quotes"]:
        opp = (r[qi["opp"]] or "").strip().lower()
        if not opp:
            continue
        a = quo.setdefault(opp, [0.0, 0])
        a[0] += float(r[qi["amount"]] or 0)
        a[1] += 1

    stats = {"belege": len(data["belege"]), "quotes": len(data["quotes"]),
             "opps_invoiced": 0, "opps_quoted": 0, "unmatched_opps": 0}
    with SessionLocal() as s:
        by_guid = {o.opportunity_guid: o for o in
                   s.scalars(select(CrmOpportunity)
                             .where(CrmOpportunity.opportunity_guid.is_not(None)))}
        for opp, (val, n, nrs) in inv.items():
            o = by_guid.get(opp)
            if o is None:
                stats["unmatched_opps"] += 1
                continue
            o.invoiced_value = round(val, 2)
            o.invoiced_count = n
            o.sap_order_numbers = nrs or None
            stats["opps_invoiced"] += 1
        for opp, (val, n) in quo.items():
            o = by_guid.get(opp)
            if o is None:
                continue
            o.quoted_value = round(val, 2)
            o.quoted_count = n
            stats["opps_quoted"] += 1
        s.commit()
    log.info("crm_import.import_opportunity_links: %s", stats)
    return stats


def import_quotes(path: str | Path) -> dict:
    """Quote totals per company -> Company.quote_count/quote_sum/conversion_rate.

    Only the aggregate is stored: individual quotes add ~44,000 rows and nothing
    the ranking uses. conversion_rate is beleg_sum / quote_sum and is meaningful
    ONLY for Handel/Verarbeiter — see the note on the Company columns.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ix = {c: i for i, c in enumerate(data["cols"])}
    agg: dict[str, list] = {}
    for r in data["rows"]:
        cust = (r[ix["cust"]] or "").strip().lower()
        if not cust:
            continue
        a = agg.setdefault(cust, [0, 0.0])
        a[0] += 1
        a[1] += float(r[ix["amount"]] or 0)

    stats = {"companies_with_quotes": 0, "unmatched": 0,
             "quoted_total": 0.0, "ordered_total": 0.0}
    with SessionLocal() as s:
        by_crm = {c.crm_id.strip().lower(): c for c in
                  s.scalars(select(Company).where(Company.crm_id.is_not(None)))
                  if c.crm_id}
        for cust, (n, total) in agg.items():
            c = by_crm.get(cust)
            if c is None:
                stats["unmatched"] += 1
                continue
            c.quote_count = n
            c.quote_sum = round(total, 2)
            c.conversion_rate = (round((c.beleg_sum or 0) / total, 4)
                                 if total > 0 else None)
            stats["companies_with_quotes"] += 1
            stats["quoted_total"] += total
            stats["ordered_total"] += (c.beleg_sum or 0)
        s.commit()
    stats["quoted_total"] = round(stats["quoted_total"], 2)
    stats["ordered_total"] = round(stats["ordered_total"], 2)
    log.info("crm_import.import_quotes: %s", stats)
    return stats


def import_contact_data(path: str | Path) -> dict:
    """Street / phone / e-mail from CRM — the evidence the identity gate runs on.

    These were missing from the first population load, which is why identity could
    not be verified at all: enrich/validate.validate_site proves a website belongs
    to a company by finding the company's OWN phone, PLZ+street or PLZ+name on the
    page, and none of those were in the database. CRM has a phone for 42,256
    accounts and a street for 46,141 — by far the strongest signals available, and
    free.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ix = {c: i for i, c in enumerate(data["cols"])}
    rows = data["rows"]
    stats = {"total": len(rows), "matched": 0, "street": 0, "phone": 0, "email": 0}
    with SessionLocal() as s:
        by_crm = {c.crm_id.strip().lower(): c for c in
                  s.scalars(select(Company).where(Company.crm_id.is_not(None)))
                  if c.crm_id}
        for r in rows:
            c = by_crm.get((r[ix["crm_id"]] or "").strip().lower())
            if c is None:
                continue
            stats["matched"] += 1
            street = (r[ix["street"]] or "").strip()
            phone = (r[ix["phone"]] or "").strip() or (r[ix["phone2"]] or "").strip()
            email = (r[ix["email"]] or "").strip()
            if street:
                c.street = street
                stats["street"] += 1
            if phone:
                c.phone = phone
                stats["phone"] += 1
            if email:
                c.email = email
                stats["email"] += 1
            fax = (r[ix["fax"]] or "").strip()
            if fax:
                c.fax = fax
        s.commit()
    log.info("crm_import.import_contact_data: %s", stats)
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

    filled = stamped = 0
    with SessionLocal() as s:
        by_crm = {c.crm_id.strip().lower(): c
                  for c in s.scalars(select(Company).where(Company.crm_id.is_not(None)))
                  if c.crm_id}
        for r in rows:
            crm_id = (r[ix["crm_id"]] or "").strip().lower()
            c = by_crm.get(crm_id)
            if c is None:
                continue
            if c.website_domain:
                # Already has a domain. If it IS the CRM one and provenance was
                # never recorded, stamp it — an earlier run of this function wrote
                # these before website_source existed, leaving 22,854 domains that
                # looked verified because nothing said otherwise.
                if not c.website_source and domain(r[ix["website"]]) == c.website_domain:
                    c.website_source = "crm"
                    if not c.identity_status:
                        c.identity_status = "unverified"
                    stamped += 1
                continue
            d = domain(r[ix["website"]])
            if d:
                c.website_domain = d
                # A URL a colleague typed into CRM is good evidence, not proof.
                # Marking provenance is what keeps it distinguishable from a
                # domain the gate actually confirmed — see Company.website_source.
                c.website_source = "crm"
                if not c.identity_status:
                    c.identity_status = "unverified"
                filled += 1
        s.commit()
    log.info("crm_import.backfill_websites: filled %s, stamped %s", filled, stamped)
    return {"filled": filled, "stamped_provenance": stamped}


# ---------------------------------------------------------------------------
# slx_product — the quote/order LINE ITEMS, pulled from Dataverse rather than an
# export. This is the "which product for whom" data that looked non-existent
# from the spreadsheets: opportunityproducts is empty in this org and the
# standard quotedetail/salesorderdetail tables answer 403, so the only place the
# product actually lives is a custom table hanging off the account.
#
# The browser pulls it (auth lives in the Dynamics session) and writes the
# aggregated JSON; this reads that file. Shape:
#   {"families": [...], "accounts": {guid: {n, v, f: {famIdx: n}, d0, d1}},
#    "opportunities": {guid: {n, v, f: {...}}}}
# ---------------------------------------------------------------------------

def import_products(path: str | Path) -> dict:
    """Load per-company product families from the slx_product pull.

    Two links carry two DIFFERENT things, and conflating them is what made the
    first attempt read EUR 430 per company:
      * the account link (199.541 rows) says WHICH families a company deals in,
        but carries a value on 23 rows out of 199.541 — it is a catalogue
        relationship, not a transaction
      * the opportunity link (156.401 rows) carries the euros — EUR 411,7 Mio
        over 30.114 valued rows — and reaches a company through the deal's Käufer

    So positions come from the first and euros from the second. Values are
    QUOTED, spanning won and lost deals alike, which is the point: it separates
    what a company ASKS for from what it BUYS. Never present them as revenue.

    Private Endkunden are dropped — they are out of scope everywhere else, and a
    product profile for them would leak into the ICP features.
    """
    import json as _json
    from . import scope
    from .models import CrmCompanyProduct, CrmOpportunity

    data = _json.loads(Path(path).read_text(encoding="utf-8"))
    families = data.get("families") or []
    now = dt.datetime.utcnow()

    with SessionLocal() as s:
        companies = {c.crm_id.lower(): c for c in s.scalars(select(Company)) if c.crm_id}
        keep = {c.id for c in companies.values() if scope.is_in_scope(c.segment)}
        opp_to_company: dict[str, int] = {}
        for o in s.scalars(select(CrmOpportunity)):
            if o.opportunity_guid and o.parent_account_crm_id:
                c = companies.get(o.parent_account_crm_id.lower())
                if c:
                    opp_to_company[o.opportunity_guid.lower()] = c.id

        # company -> family -> [positions, value, first, last]
        agg: dict[int, dict[str, list]] = {}

        def slot(cid: int, fam_idx) -> list | None:
            try:
                fam = families[int(fam_idx)]
            except (ValueError, IndexError, TypeError):
                return None
            return agg.setdefault(cid, {}).setdefault(fam, [0, 0.0, None, None])

        direct = 0
        for guid, blk in (data.get("accounts") or {}).items():
            c = companies.get(guid.lower())
            if not c or c.id not in keep:
                continue
            direct += 1
            for fi, n in (blk.get("f") or {}).items():
                sl = slot(c.id, fi)
                if sl is None:
                    continue
                sl[0] += n
                for d, i in ((blk.get("d0"), 2), (blk.get("d1"), 3)):
                    if d and (sl[i] is None or (d < sl[i] if i == 2 else d > sl[i])):
                        sl[i] = d

        via = 0
        for guid, blk in (data.get("opportunities") or {}).items():
            cid = opp_to_company.get(guid.lower())
            if cid is None or cid not in keep:
                continue
            via += 1
            for fi, n in (blk.get("f") or {}).items():
                sl = slot(cid, fi)
                if sl is not None and not (data.get("accounts") or {}):
                    sl[0] += n
            for fi, v in (blk.get("v") or {}).items():
                sl = slot(cid, fi)
                if sl is not None:
                    sl[1] += float(v or 0)

        s.query(CrmCompanyProduct).delete()
        rows = 0
        for cid, per_fam in agg.items():
            for fam, (n, value, d0, d1) in per_fam.items():
                if not n and not value:
                    continue
                s.add(CrmCompanyProduct(
                    company_id=cid, family=fam[:120], positions=n,
                    value=round(value, 2) or None,
                    first_seen=dt.date.fromisoformat(d0) if d0 else None,
                    last_seen=dt.date.fromisoformat(d1) if d1 else None,
                    synced_at=now))
                rows += 1
        s.commit()

    return {"families": len(families), "companies": len(agg), "rows": rows,
            "from_account_link": direct, "from_opportunity_link": via}
