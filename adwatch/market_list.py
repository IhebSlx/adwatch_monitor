"""Import a hand-researched market list (a colleague's scrape) as companies.

Written for the Spain market analysis of 2026-08, and deliberately general: the
shape of "a colleague sends a spreadsheet of firms they found" recurs, and every
one of them arrives with the same four problems.

WHY THE FILE CANNOT JUST BE LOADED
----------------------------------
1. The delimiter breaks on Spanish legal forms. `CARPYVENT, S.L.` was written to a
   semicolon-separated file with its comma turned into a semicolon, so 46 of 534
   rows are shifted left — 41 by one field, 5 by three. Raw, `S.L.` becomes the
   company TYPE and every later column lands in the wrong place. Repaired by
   finding the first field whose value is a known `Typ`, joining everything before
   it back into the name. Verified: this recovers all 534 rows.

2. `Typ` mixes things that must be treated in opposite ways. 95 rows say
   `wettbewerber`, but only 68 are a competitor's OWN location (a Schüco showroom,
   a CORTIZO branch). The other 27 are independent fabricators that INSTALL
   competitor systems — "Schüco Premium Partner", "Sky-Frame Exklusivpartner" —
   which makes them the most valuable prospects in the file, not competitors.
   They are routed to prospects and keep `import_type='wettbewerber'` so their
   origin stays auditable.

3. Duplicates. 39 companies appear twice (78 rows), and the pairs often carry
   DIFFERENT `Typ` values with otherwise identical content — the same firm entered
   once as `wettbewerber` and once as `potenzialkunde`.

4. Names do not join to CRM. Only 7 of 534 matched by name, while 30 matched on the
   `Kd-Nr.` buried in the free-text `Notizen` field: `IBZ Cristal` is
   `IBZ Cortinas De Cristal SL`, `Cerramientos T27` is `Comercial T27 SL`. So the
   customer number is the primary join and the name is a last resort.

Also: `Lat`/`Lng` are dropped. Excel corrupted them in transit (one longitude reads
"Feb 23", once 2.23) and the address survived intact, so the coordinates are worse
than nothing.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
from pathlib import Path

from sqlalchemy import select

from . import markets
from .db import SessionLocal
from .models import Company

log = logging.getLogger("adwatch.market_list")

# The vocabulary of the `Typ` column. Used to locate the real column boundary in a
# shifted row, so it must stay exhaustive for the files being imported.
KNOWN_TYPES = ("potenzialkunde", "architekt", "wettbewerber", "bestandskunde",
               "fertigungspartner")

# Manufacturer names. A hit in the company's OWN NAME means this row is that
# manufacturer's location. A hit only in the brands/notes means the firm SELLS
# those systems — a conquest target, the opposite of a competitor.
COMPETITOR_BRANDS = (
    "schueco", "schuco", "schüco", "reynaers", "cortizo", "sunflex", "technal",
    "skyframe", "sky-frame", "drutex", "strugal", "koemmerling", "kömmerling",
    "hydro", "velux", "oikos", "aluprof", "wicona", "hueck",
)

# How each source classification is routed. (segment, is_competitor_candidate)
# Segments use the CRM vocabulary so the ICP and every filter keep working.
_ROUTING = {
    "potenzialkunde": ("Verarbeiter", False),
    "architekt": ("Architekten", False),
    "bestandskunde": (None, False),      # matched to an existing row; segment kept
    "fertigungspartner": ("Verarbeiter", False),
    "wettbewerber": ("Verarbeiter", True),   # candidate — decided per row below
}


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _domain(url: str | None) -> str | None:
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


def _customer_number(notes: str | None) -> str | None:
    """The SAP number hidden in free text, e.g. 'Kd-Nr. 5164611 | Lizenznehmer...'."""
    m = re.search(r"Kd-?Nr\.?\s*:?\s*(\d{5,10})", notes or "", re.I)
    return m.group(1) if m else None


def _postal_and_city(address: str | None) -> tuple[str | None, str | None]:
    """Pull a Spanish 5-digit postcode and the town out of a free-text address.

    Spanish addresses put the postcode before the town: 'Donostia Ibilbidea 80,
    20115 Astigarraga, Guipuzcoa, Spanien'.
    """
    a = (address or "").strip()
    if not a:
        return None, None
    m = re.search(r"\b(\d{5})\b\s*,?\s*([^,]+)", a)
    if m:
        return m.group(1), m.group(2).strip() or None
    parts = [p.strip() for p in a.split(",") if p.strip()]
    # drop a trailing country word so the town is not "Spanien"
    if parts and _norm(parts[-1]) in ("spanien", "spain", "espana", "españa"):
        parts = parts[:-1]
    return None, (parts[-1] if parts else None)


def parse(path: str | Path) -> dict:
    """Repair and normalise the file. Pure — touches no database."""
    raw = Path(path).read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw), delimiter=";"))
    if not rows:
        raise ValueError("empty file")
    header = [h.strip() for h in rows[0]]

    parsed, unparsable, shifts = [], [], {}
    for r in rows[1:]:
        if not any(x.strip() for x in r):
            continue
        ti = next((i for i, v in enumerate(r)
                   if v.strip().lower() in KNOWN_TYPES), None)
        if ti is None:
            unparsable.append(r)
            continue
        shifts[ti] = shifts.get(ti, 0) + 1
        rec = dict(zip(header[1:], r[ti:]))
        # everything before the Typ column belongs to the name; the comma that
        # became a semicolon is restored
        rec["Name"] = ", ".join(x.strip() for x in r[:ti] if x.strip())
        parsed.append(rec)

    out = []
    for rec in parsed:
        typ = (rec.get("Typ") or "").strip().lower()
        name = " ".join((rec.get("Name") or "").split())
        if not name:
            continue
        brands = " ".join(str(rec.get(k) or "") for k in
                          ("Marken/Produkte", "Notizen", "Untertyp", "Kategorie"))
        name_has_brand = any(b in _norm(name) for b in COMPETITOR_BRANDS)
        seg, competitor_candidate = _ROUTING.get(typ, ("Verarbeiter", False))
        # A competitor only when the manufacturer's name is the COMPANY's name.
        is_competitor = bool(competitor_candidate and name_has_brand)
        plz, city = _postal_and_city(rec.get("Adresse"))
        out.append({
            "name": name,
            "import_type": typ,
            "segment": seg,
            "is_competitor": is_competitor,
            # visible flag for the conquest cases: routed to prospect, but arrived
            # tagged as competitor because it installs a rival's systems
            "carries_competitor_brand": bool(
                competitor_candidate and not name_has_brand),
            "website_domain": _domain(rec.get("Website")),
            "address": (rec.get("Adresse") or "").strip() or None,
            "postal_code": plz, "city": city,
            "sub_type": (rec.get("Untertyp") or "").strip() or None,
            "brands": (rec.get("Marken/Produkte") or "").strip() or None,
            "notes": (rec.get("Notizen") or "").strip() or None,
            "assessment": (rec.get("Einschaetzung") or "").strip() or None,
            "contact": (rec.get("Ansprechpartner") or "").strip() or None,
            "customer_number": _customer_number(rec.get("Notizen")),
        })

    # ---- de-duplicate within the file ----
    # Keyed on normalised name. Where the same firm appears with two different
    # Typ values, the record carrying more information wins and the discarded
    # Typ is remembered, so nothing about its origin is silently lost.
    best: dict[str, dict] = {}
    dupes = 0
    for rec in out:
        k = _norm(rec["name"])
        prev = best.get(k)
        if prev is None:
            best[k] = rec
            continue
        dupes += 1
        prev.setdefault("also_imported_as", set()).add(rec["import_type"])
        rec_fill = sum(1 for v in rec.values() if v)
        prev_fill = sum(1 for v in prev.values() if v)
        if rec_fill > prev_fill:
            rec["also_imported_as"] = prev.get("also_imported_as", set()) | {prev["import_type"]}
            best[k] = rec
        # a competitor verdict is sticky: if EITHER row says the name is a
        # manufacturer's own location, do not let the other row make it a target
        if rec["is_competitor"]:
            best[k]["is_competitor"] = True

    records = list(best.values())
    for r in records:
        extra = {t for t in r.pop("also_imported_as", set()) if t != r["import_type"]}
        r["also_imported_as"] = sorted(extra) or None

    return {
        "records": records,
        "stats": {
            "rows_in_file": len(rows) - 1,
            "parsed": len(parsed),
            "unparsable": len(unparsable),
            "shift_histogram": shifts,       # {1: 488, 2: 41, 3: 5} -> 1 = intact
            "duplicates_removed": dupes,
            "unique": len(records),
            "competitors": sum(1 for r in records if r["is_competitor"]),
            "carries_competitor_brand": sum(
                1 for r in records if r["carries_competitor_brand"]),
            "with_customer_number": sum(1 for r in records if r["customer_number"]),
            "with_website": sum(1 for r in records if r["website_domain"]),
        },
    }


def _note_block(rec: dict) -> str:
    """The colleague's research, kept verbatim and attributed."""
    bits = []
    for label, key in (("Untertyp", "sub_type"), ("Marken/Produkte", "brands"),
                       ("Notizen", "notes"), ("Einschätzung", "assessment"),
                       ("Ansprechpartner", "contact"), ("Adresse", "address")):
        if rec.get(key):
            bits.append(f"{label}: {rec[key]}")
    if rec.get("also_imported_as"):
        bits.append("auch erfasst als: " + ", ".join(rec["also_imported_as"]))
    return "\n".join(bits)


def import_list(path: str | Path, *, lead_source: str, country: str = "ES",
                dry_run: bool = False) -> dict:
    """Apply a parsed market list to Company.

    Matching order against the existing base: customer number -> website domain ->
    name+country. `bestandskunde` rows are EXPECTED to match; a match is never
    overwritten with list data — CRM owns master data — the research is appended
    to `notes` instead, which is local-owned.
    """
    parsed = parse(path)
    records = parsed["records"]
    code = markets.code_for(country) or country.upper()
    stamp = dt.datetime.utcnow()

    stats = {**parsed["stats"], "inserted": 0, "matched": 0,
             "matched_by": {"customer_number": 0, "website": 0, "name": 0},
             "renamed_collision": 0}
    if dry_run:
        return {**stats, "dry_run": True,
                "preview": [{k: r[k] for k in
                             ("name", "import_type", "segment", "is_competitor",
                              "carries_competitor_brand", "city", "website_domain")}
                            for r in records[:15]]}

    with SessionLocal() as s:
        all_c = list(s.scalars(select(Company)))
        by_sap = {(c.sap_number or "").lstrip("0"): c for c in all_c if c.sap_number}
        by_dom = {c.website_domain: c for c in all_c if c.website_domain}
        by_name = {(_norm(c.name), (c.country or "").upper()): c for c in all_c}
        taken = {_norm(c.name) for c in all_c}

        for rec in records:
            match = None
            kd = rec["customer_number"]
            if kd and kd.lstrip("0") in by_sap:
                match, how = by_sap[kd.lstrip("0")], "customer_number"
            elif rec["website_domain"] and rec["website_domain"] in by_dom:
                match, how = by_dom[rec["website_domain"]], "website"
            elif (_norm(rec["name"]), code) in by_name:
                match, how = by_name[(_norm(rec["name"]), code)], "name"

            block = _note_block(rec)
            if match is not None:
                stats["matched"] += 1
                stats["matched_by"][how] += 1
                # never touch CRM-owned master data on a match
                header = f"--- {lead_source} ({rec['import_type']}) ---"
                if block and header not in (match.notes or ""):
                    match.notes = ((match.notes or "").rstrip()
                                   + ("\n\n" if match.notes else "")
                                   + header + "\n" + block)
                if rec["is_competitor"]:
                    match.is_competitor = True
                if not match.import_type:
                    match.import_type = rec["import_type"]
                continue

            unique = rec["name"]
            if _norm(unique) in taken:
                suffix = rec["city"] or code
                unique = f"{rec['name']} · {suffix}"
                n = 2
                while _norm(unique) in taken:
                    unique = f"{rec['name']} · {suffix} ({n})"
                    n += 1
                stats["renamed_collision"] += 1
            taken.add(_norm(unique))

            c = Company(
                name=unique, country=code, source="marktanalyse",
                lead_source=lead_source, import_type=rec["import_type"],
                segment=rec["segment"], is_competitor=rec["is_competitor"],
                # A scraped list is a lead list, not something to start spending
                # ad-fetch budget on automatically — the team turns monitoring on.
                monitored=False,
                resolution_status="pending",
                postal_code=rec["postal_code"], city=rec["city"],
                street=rec["address"],
                website_domain=rec["website_domain"],
                website_source="marktanalyse" if rec["website_domain"] else None,
                identity_status="unverified" if rec["website_domain"] else None,
                customer_state="never",
                notes=block or None,
            )
            s.add(c)
            stats["inserted"] += 1
        s.commit()

    log.info("market_list.import_list(%s): %s", lead_source, stats)
    return stats
