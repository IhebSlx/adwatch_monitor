"""Generate the weekly PDF report from stored metrics.

Filenames are named after the ISO calendar week (e.g. adwatch_top5_KW29_2026.pdf)
rather than the exact day, since one report is meant to represent one week.
Generating again within the same week never overwrites the previous file —
it gets an incrementing suffix instead (_01, _02, ...), so every past report
stays available in output/."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import unquote
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from . import config
from .services import latest_metrics

INK = colors.HexColor("#1f2933")
ACCENT = colors.HexColor("#2b6cb0")
ACCENT_SOFT = colors.HexColor("#ebf2fb")
MUTED = colors.HexColor("#647380")
LINE = colors.HexColor("#d9e2ec")
BG = colors.HexColor("#f7f9fb")
_LINK_HEX = "#2b6cb0"

# Report text is German (business audience), independent of any locale
# setting on the machine that generates it — hence the manual month names
# rather than relying on strftime's %b/%B.
_DE_MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
              "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]

_CATEGORY_LABEL_DE = {
    "recruitment": "Personalsuche", "product_sale": "Verkauf",
    "brand_awareness": "Marke", "event_promo": "Veranstaltungen", "other": "Sonstiges",
}
_METHOD_LABEL_DE = {"reach": "Reichweite", "count": "Anzahl", "mixed": "gemischt"}

_REVENUE_HISTORY_LABEL_DE = {
    "lapsed": "ehemalige Käufer (kein Umsatz akt. Jahr)",
    "new": "Neukäufer (nur akt. Jahr)",
    "any": "jemals Umsatz",
    "never": "nie Umsatz",
}
_FILTER_FIELD_LABEL_DE = {
    "kv": "KV", "segment": "Segment", "sub_segment": "Untersegment",
    "sales_channel": "Vertriebsweg", "country": "Land",
}
_STATUS_LABEL_DE = {
    "locked": "Meta-Seite gesperrt", "confirmed": "Meta-Seite gefunden", "ambiguous": "Meta-Seite unklar",
    "no_ads_found": "keine Meta-Seite gefunden", "pending": "Meta nicht geprüft",
}


def _describe_filters_de(filters: dict | None) -> str | None:
    """Human-readable German summary of an active Companies-Explorer filter,
    for display in the report header — None if no filter was applied."""
    if not filters:
        return None
    # An explicit hand-picked selection ("report for selected") — describe it as
    # a selection, not a filter chain.
    if filters.get("ids"):
        n = len(filters["ids"])
        return f"Auswahl: {n} {'ausgewählte Firma' if n == 1 else 'ausgewählte Firmen'}"
    bits = []
    if filters.get("q"):
        bits.append(f'Suche: "{filters["q"]}"')
    for field, label in _FILTER_FIELD_LABEL_DE.items():
        value = filters.get(field)
        if value:
            value_str = ", ".join(value) if isinstance(value, list) else value
            bits.append(f"{label}: {value_str}")
    if filters.get("resolution_status"):
        vals = filters["resolution_status"]
        vals = vals if isinstance(vals, list) else [vals]
        bits.append("Status: " + ", ".join(_STATUS_LABEL_DE.get(v, v) for v in vals))
    if filters.get("ad_activity"):
        _aa = {"active": "nur mit aktiven Anzeigen", "any": "nur je beworben",
               "none": "nur ohne aktive Anzeigen"}.get(filters["ad_activity"], filters["ad_activity"])
        _src = {"meta": "Meta", "google": "Google"}.get(filters.get("ad_source"))
        bits.append(f"{_aa} ({_src})" if _src else _aa)
    rmin, rmax = filters.get("revenue_min"), filters.get("revenue_max")
    if rmin is not None and rmax is not None:
        bits.append(f"Umsatz: {_eur(rmin)}–{_eur(rmax)}")
    elif rmin is not None:
        bits.append(f"Umsatz ab {_eur(rmin)}")
    elif rmax is not None:
        bits.append(f"Umsatz bis {_eur(rmax)}")
    if filters.get("revenue_history"):
        bits.append(_REVENUE_HISTORY_LABEL_DE.get(filters["revenue_history"], filters["revenue_history"]))
    if filters.get("exclude_kv"):
        bits.append(f"ohne KV: {', '.join(filters['exclude_kv'])}")
    if filters.get("exclude_segment"):
        bits.append(f"ohne Segment: {', '.join(filters['exclude_segment'])}")
    if filters.get("exclude_sub_segment"):
        bits.append(f"ohne Untersegment: {', '.join(filters['exclude_sub_segment'])}")
    if filters.get("has_website"):
        bits.append("nur mit Website")
    if filters.get("page_id_state") == "with":
        bits.append("nur mit Meta-Page-ID")
    elif filters.get("page_id_state") == "without":
        bits.append("nur ohne Meta-Page-ID")
    if filters.get("tracked") is not None:
        bits.append("nur getrackte Firmen" if filters["tracked"] else "nur ungetrackte Firmen")
    return "Gefiltert nach: " + "; ".join(bits) if bits else None


def _filtered_company_ids(filters: dict | None) -> list[int] | None:
    if not filters:
        return None
    from sqlalchemy import select as _select
    from .customers import _apply_filters
    from .db import SessionLocal as _SessionLocal
    from .models import Company as _Company
    with _SessionLocal() as s:
        return list(s.scalars(_apply_filters(_select(_Company.id), filters)))


def _eur(v) -> str:
    if v is None:
        return "-"
    return f"€{v:,.0f}".replace(",", ".")


def _de_date(d: dt.date) -> str:
    return f"{d.day}. {_DE_MONTHS[d.month - 1]} {d.year}"


def _de_datetime(d: dt.datetime) -> str:
    return f"{_de_date(d)}, {d:%H:%M} Uhr"


def week_label(d: dt.date | None = None) -> str:
    """'KW29_2026' — ISO calendar week, filename-safe."""
    d = d or dt.date.today()
    iso_year, iso_week, _ = d.isocalendar()
    return f"KW{iso_week:02d}_{iso_year}"


def next_report_path(prefix: str, label: str | None = None) -> Path:
    """First free path for `{prefix}_{label}.pdf`, else `_01`, `_02`, ... so an
    existing report for that week is never overwritten."""
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    label = label or week_label()
    base = config.OUTPUT_DIR / f"{prefix}_{label}.pdf"
    if not base.exists():
        return base
    n = 1
    while True:
        candidate = config.OUTPUT_DIR / f"{prefix}_{label}_{n:02d}.pdf"
        if not candidate.exists():
            return candidate
        n += 1


_REPORT_FILENAME_RE = re.compile(r"^adwatch_(top5|report)_(KW\d{2}_\d{4})(?:_(\d{2}))?\.pdf$")


def parse_report_filename(filename: str) -> dict | None:
    """Reverse of the naming scheme, for the reports-history listing.
    Returns {report_type, label, version} or None if it doesn't match."""
    m = _REPORT_FILENAME_RE.match(filename)
    if not m:
        return None
    kind, label, version = m.groups()
    return {"report_type": "top5" if kind == "top5" else "full",
           "label": label, "version": int(version) if version else None}


def subject_for_filename(filename: str) -> str:
    """'Bericht-KW-29' from a report filename like adwatch_top5_KW29_2026.pdf;
    falls back to a generic subject if the filename doesn't match the naming
    scheme (e.g. a renamed or pre-existing file)."""
    parsed = parse_report_filename(filename)
    if not parsed:
        return "AdWatch Weekly Report"
    week = parsed["label"].split("_")[0][2:]  # 'KW29_2026' -> '29'
    return f"Bericht-KW-{week}"


def week_str_for_filename(filename: str) -> str:
    """'KW 29' from a report filename, for use in the email body. Empty
    string if the filename doesn't match the naming scheme."""
    parsed = parse_report_filename(filename)
    if not parsed:
        return ""
    week = parsed["label"].split("_")[0][2:]
    return f"KW {week}"


def _esc(text) -> str:
    """Escape dynamic text for reportlab's mini-XML (company names contain & etc.)."""
    return _xml_escape(str(text if text is not None else ""))


def _page_label(name) -> str:
    """A Facebook page slug is URL-encoded and hyphenated, so a Spanish page
    printed as 'Dise%C3%B1a-Soluciones-En-Vidrio-175791052761874'. Decode it,
    drop the trailing numeric id and turn hyphens back into spaces so the report
    shows 'Diseña Soluciones En Vidrio'."""
    label = unquote(str(name or ""))
    if "-" in label:
        parts = label.split("-")
        if parts[-1].isdigit() and len(parts) > 1:
            parts = parts[:-1]                    # the page id, not part of the name
        label = " ".join(p for p in parts if p)
    return label.strip() or str(name or "")


def _link(text, url: str | None) -> str:
    """A clickable link paragraph fragment; plain escaped text when no URL."""
    safe = _esc(text)
    if not url:
        return safe
    href = _xml_escape(str(url), {'"': "&quot;"})
    return f'<a href="{href}" color="{_LINK_HEX}"><u>{safe}</u></a>'


def _ad_library_url(page_id: str | None, country: str | None = "DE") -> str | None:
    """Deep link to a page's ACTIVE ads in the Meta Ad Library — click to see the
    exact ads behind the numbers in this report."""
    if not page_id:
        return None
    return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country={country or 'DE'}&view_all_page_id={page_id}&search_type=page&media_type=all")


def _google_transparency_url(advertiser_id: str | None, country: str | None = "DE") -> str | None:
    """Deep link to a company's ads in the Google Ads Transparency Center."""
    if not advertiser_id:
        return None
    return f"https://adstransparency.google.com/advertiser/{advertiser_id}?region={country or 'DE'}"


def _web_url(domain: str | None) -> str | None:
    if not domain:
        return None
    return domain if domain.startswith(("http://", "https://")) else "https://" + domain


def _page_link_map(company_ids) -> dict:
    """{company_id: {page_id, country, website, google_id}} for the given
    companies — used to turn names into Ad-Library / Google-Transparency links
    without an extra query per row. `page_id` is the Meta (Facebook) page id;
    `google_id` is the Google advertiser id (from its source='google' page)."""
    if not company_ids:
        return {}
    from sqlalchemy import select as _select
    from .db import SessionLocal as _SessionLocal
    from .models import Company as _Company, CompanyPage as _CompanyPage
    with _SessionLocal() as s:
        rows = s.execute(_select(_Company.id, _Company.page_id, _Company.country,
                                 _Company.website_domain).where(_Company.id.in_(company_ids))).all()
        gp = s.execute(_select(_CompanyPage.company_id, _CompanyPage.page_id).where(
            _CompanyPage.source == "google", _CompanyPage.active,
            _CompanyPage.company_id.in_(company_ids))).all()
    gmap = {cid: pid for cid, pid in gp}
    return {cid: {"page_id": pid, "country": ctry, "website": web, "google_id": gmap.get(cid)}
            for cid, pid, ctry, web in rows}


def _company_link(name: str, info: dict | None) -> str:
    """Company name linked to its Ad Library page (preferred) or its website."""
    info = info or {}
    url = _ad_library_url(info.get("page_id"), info.get("country")) or _web_url(info.get("website"))
    return _link(name, url)


def _ads_cta(info: dict | None) -> str:
    """German call-to-action link(s) to a company's ACTIVE ads. The link TEXT is
    the CTA, not the company name/URL. Adds a Meta link when a numeric page id is
    resolved and a Google link when a Google advertiser id is resolved — so a
    Google-only advertiser still gets a working link. '' when neither exists."""
    info = info or {}
    parts = []
    meta = _ad_library_url(info.get("page_id"), info.get("country"))
    if meta:
        parts.append(_link("» Aktive Anzeigen ansehen", meta))
    google = _google_transparency_url(info.get("google_id"), info.get("country"))
    if google:
        parts.append(_link("» Google-Anzeigen ansehen", google))
    return " &nbsp;·&nbsp; ".join(parts)


def _scope_banner(filters: dict | None, n_companies: int, n_active: int, styles) -> Table:
    """The explicit 'what this report covers' box — filter used + counts, shown
    prominently near the top so the reader always knows the scope."""
    desc = _describe_filters_de(filters)
    scope_line = _esc(desc) if desc else "Alle Firmen (kein Filter angewendet)"
    label = ParagraphStyle("scopelbl", parent=styles["Normal"], textColor=ACCENT,
                           fontSize=8, leading=11, spaceAfter=1)
    val = ParagraphStyle("scopeval", parent=styles["Normal"], textColor=INK,
                         fontSize=10.5, leading=14)
    meta = ParagraphStyle("scopemeta", parent=styles["Normal"], textColor=MUTED,
                          fontSize=8.5, leading=12, spaceBefore=2)
    inner = [
        Paragraph("BERICHT-UMFANG", label),
        Paragraph(scope_line, val),
        Paragraph(f"{n_companies} Firmen im Bericht &nbsp;·&nbsp; "
                  f"<b>{n_active}</b> mit aktiven Anzeigen", meta),
    ]
    t = Table([[inner]], colWidths=[178 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_SOFT),
        ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _divergence_story(filters: dict | None = None, limit: int = 10, links: dict | None = None) -> list:
    """The 'Interessante Partner' lead section for both report types — top
    divergence rows (Marketing-Aktivität × Umsatz-Lücke) with the German
    reason line per partner. Empty list when nothing scored ≥ 10, in which
    case the section is simply omitted."""
    from .insights.divergence import compute_divergence
    div = compute_divergence(_filtered_company_ids(filters) if filters else None)
    rows = [r for r in div["rows"] if r["divergence"] >= 10][:limit]
    if not rows:
        return []

    styles = getSampleStyleSheet()
    h2 = ParagraphStyle("divh2", parent=styles["Heading2"], textColor=ACCENT,
                        fontSize=13, spaceBefore=4, spaceAfter=3)
    note = ParagraphStyle("divnote", parent=styles["Normal"], textColor=MUTED,
                          fontSize=8, leading=11)
    cellh = ParagraphStyle("divcellh", parent=styles["Normal"], textColor=colors.white,
                           fontSize=8.5, leading=11)
    cell = ParagraphStyle("divcell", parent=styles["Normal"], textColor=INK,
                          fontSize=8.5, leading=11)

    out = [
        Paragraph("Interessante Partner (Divergenz)", h2),
        Paragraph(
            "Divergenz = Marketing-Aktivität × Umsatz-Lücke: Partner, die kaum noch bei uns "
            "kaufen, aber aktiv werben — Win-back-Kandidaten zuerst. Formel siehe App → "
            "„How it works“.", note),
        Spacer(1, 4),
    ]
    links = links or _page_link_map([r["company_id"] for r in rows])
    header = ["#", "Firma", "Divergenz", "Typ", "Grund"]
    trows = [[Paragraph(h, cellh) for h in header]]
    for i, r in enumerate(rows, start=1):
        cta = _ads_cta(links.get(r["company_id"]))
        firma = _esc(r["company"]) + (f'<br/><font size="7.5">{cta}</font>' if cta else "")
        trows.append([
            Paragraph(str(i), cell),
            Paragraph(firma, cell),
            Paragraph(f"<b>{r['divergence']}</b>/100", cell),
            Paragraph(_esc(r["label"] or "—"), cell),
            Paragraph(_esc(r["reason"]), cell),
        ])
    table = Table(trows, colWidths=[8 * mm, 42 * mm, 18 * mm, 22 * mm, 84 * mm], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    for i in range(1, len(trows)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), BG))
    table.setStyle(TableStyle(style))
    out += [table, Spacer(1, 12)]
    return out


def _delta_frag(delta) -> str:
    """'(+3)' green / '(-2)' red vs last week, or '' when unchanged/unknown.
    Plain ASCII +/- (the base PDF font has no ▲/▼ glyphs)."""
    if not delta:
        return ""
    up = delta > 0
    return f' <font color="{"#2f855a" if up else "#b04a3a"}">({"+" if up else "-"}{abs(delta)})</font>'


def _source_label(rows) -> str:
    """Name the ad sources that actually contributed to this report — credit
    Google only when Google ads are present, so a Meta-only report isn't
    mislabelled as using Google."""
    has_google = any((d.get("google_active_ads") or 0) > 0 for d in rows)
    return "Meta Ad Library + Google Ads Transparency" if has_google else "Meta Ad Library"


def _enrichment_map(company_ids: list[int] | None) -> dict[int, dict]:
    """company_id -> enriched fields (see enrich/). Empty dict when a company was
    never enriched, so callers can simply skip it."""
    from sqlalchemy import select as _select

    from .db import SessionLocal as _S
    from .models import Company as _C, CompanyEnrichment as _E
    out: dict[int, dict] = {}
    with _S() as s:
        stmt = _select(_E, _C).join(_C, _C.id == _E.company_id)
        if company_ids is not None:
            stmt = stmt.where(_E.company_id.in_(company_ids))
        for enr, comp in s.execute(stmt):
            f = enr.fields or {}
            out[enr.company_id] = {
                "description": f.get("description_de") or comp.description,
                "assessment": f.get("assessment_de"),
                "products": f.get("products") or comp.products or [],
                "founded_year": f.get("founded_year") or comp.founded_year,
                "employee_hint": f.get("employee_hint") or comp.employee_hint,
                "legal_form": f.get("legal_form"),
                "service_area": f.get("service_area"),
                "mentions_solarlux": f.get("mentions_solarlux"),
                "competitor_brands": comp.competitor_brands or f.get("competitor_brands") or [],
                "website": comp.website_domain,
                "status": enr.status,
                # Qualification fields, from the two extraction profiles. Read
                # from the Company columns (the merged, retraction-aware copy)
                # rather than this run's raw blob — same rule the Explorer uses.
                # betrieb: does this company already sell what we build?
                "solarlux_fit": comp.solarlux_fit,
                "partner_of": comp.partner_of or [],
                "own_fabrication": comp.own_fabrication,
                "installs": comp.installs,
                "has_showroom": comp.has_showroom,
                # architekt: does this office plan projects where we fit, and
                # does it decide? (Spain: offices genuinely award contracts)
                "solarlux_relevance": comp.solarlux_relevance,
                "decision_role": comp.decision_role,
                "office_type": comp.office_type,
                "reference_scale": comp.reference_scale,
                "project_focus": comp.project_focus or [],
                "segment": comp.segment,
                "city": comp.city,
                "identity_status": comp.identity_status,
            }
    return out


def _profiles_story(data: list[dict], filters: dict | None, styles, limit: int = 80) -> list:
    """FIRMENPROFILE — one compact block per enriched company: what the website
    says (belegt), then the AI assessment (clearly marked as an estimate), then
    the hard fields and any ad activity.

    Deliberately driven by the company scope, not by ad metrics: a market that has
    never been fetched (e.g. Spain) still gets full profiles."""
    enr = _enrichment_map([d["company_id"] for d in data])
    rows = [d for d in data if enr.get(d["company_id"], {}).get("description")
            or enr.get(d["company_id"], {}).get("assessment")]
    if not rows:
        return []

    h2 = ParagraphStyle("ph2", parent=styles["Heading2"], textColor=INK, fontSize=13,
                        spaceBefore=16, spaceAfter=4)
    nm = ParagraphStyle("pnm", parent=styles["Normal"], textColor=INK, fontSize=10,
                        leading=13, spaceBefore=7, spaceAfter=1)
    fact = ParagraphStyle("pfact", parent=styles["Normal"], textColor=INK, fontSize=8.8,
                          leading=12, leftIndent=6)
    est = ParagraphStyle("pest", parent=styles["Normal"], textColor=colors.HexColor("#4a5568"),
                         fontSize=8.8, leading=12, leftIndent=6)
    meta = ParagraphStyle("pmeta", parent=styles["Normal"], textColor=MUTED, fontSize=8,
                          leading=11, leftIndent=6, spaceAfter=2)
    note = ParagraphStyle("pnote", parent=styles["Normal"], textColor=MUTED, fontSize=7.5,
                          leading=11, spaceBefore=2)

    story: list = [
        Paragraph("Firmenprofile", h2),
        Paragraph("Je Firma: <b>Beschreibung</b> = wörtlich von der eigenen Website belegt. "
                  "<b>Einschätzung</b> = KI-gestützte Ableitung aus dem Seiteninhalt "
                  "(Größenklasse, Zielkundschaft, Positionierung) — plausibel, aber "
                  "<b>keine belegte Angabe</b> und vor einer Ansprache zu prüfen.", note),
        Spacer(1, 4),
    ]

    for d in rows[:limit]:
        e = enr[d["company_id"]]
        head = _esc(d["company"])
        if e.get("website"):
            head += f' &nbsp;·&nbsp; {_link(_web_url(e["website"]), _esc(e["website"]))}'
        story.append(Paragraph(f"<b>{head}</b>", nm))

        if e.get("description"):
            story.append(Paragraph(f'<b>Beschreibung:</b> {_esc(e["description"])}', fact))
        if e.get("assessment"):
            story.append(Paragraph(f'<b>Einschätzung:</b> <i>{_esc(e["assessment"])}</i>', est))

        bits = []
        if e.get("products"):
            bits.append("Produkte: " + _esc(", ".join(e["products"][:6])))
        if e.get("founded_year"):
            bits.append(f'Gegründet: {e["founded_year"]}')
        if e.get("employee_hint"):
            bits.append("Größe: " + _esc(str(e["employee_hint"])))
        if e.get("legal_form"):
            bits.append("Rechtsform: " + _esc(e["legal_form"]))
        if e.get("service_area"):
            bits.append("Gebiet: " + _esc(str(e["service_area"])[:60]))
        if bits:
            story.append(Paragraph(" &nbsp;·&nbsp; ".join(bits), meta))

        # The qualification line — the fields a colleague actually calls on.
        # Wording mirrors ONBOARDING: Passung/Fremdmarken for a Betrieb,
        # Relevanz/Entscheidungsrolle for an Architekturbüro.
        qual = []
        if e.get("solarlux_fit"):
            qual.append(f'<b>Passung: {_esc(e["solarlux_fit"])}</b>')
        if e.get("own_fabrication") is True:
            qual.append("eigene Fertigung")
        if e.get("installs") is True:
            qual.append("montiert selbst")
        if e.get("has_showroom") is True:
            qual.append("Showroom")
        if e.get("partner_of"):
            qual.append("Vertragspartner von <b>" + _esc(", ".join(e["partner_of"][:4])) + "</b>")
        if e.get("solarlux_relevance"):
            qual.append(f'<b>Relevanz: {_esc(e["solarlux_relevance"])}</b>')
        if e.get("decision_role"):
            qual.append(_esc(e["decision_role"]))
        if e.get("office_type"):
            qual.append(_esc(e["office_type"]))
        if e.get("reference_scale"):
            qual.append(_esc(str(e["reference_scale"])[:70]))
        if qual:
            story.append(Paragraph(" &nbsp;·&nbsp; ".join(qual), meta))

        brands = []
        if e.get("mentions_solarlux") is True:
            brands.append("<b>nennt Solarlux</b>")
        elif e.get("mentions_solarlux") is False:
            brands.append("nennt Solarlux nicht")
        if e.get("competitor_brands"):
            brands.append("Wettbewerber auf der Website: <b>"
                          + _esc(", ".join(e["competitor_brands"][:6])) + "</b>")
        act = d.get("total_active_ads") or 0
        if d.get("has_data"):
            brands.append(f"Anzeigen: {act} aktiv" if act else "keine aktiven Anzeigen")
        if brands:
            story.append(Paragraph(" &nbsp;·&nbsp; ".join(brands), meta))

    if len(rows) > limit:
        story.append(Paragraph(f"… {len(rows) - limit} weitere angereicherte Firmen nicht "
                               "dargestellt — vollständig im Excel-Export.", note))
    return story


def _qualification_story(filters: dict | None, styles) -> list:
    """MARKTQUALIFIZIERUNG — the scorecard section: who in this scope should be
    called first, judged from evidence, not from a model.

    Exists because a market can deserve a report long before it can carry an
    ICP. Spain today: 21 material buyers against a floor of 30, so a trained
    ranking would be noise wearing a number — but 397 enriched companies carry
    Passung, Relevanz, Entscheidungsrolle and the brand evidence, and those
    ARE rankable by rule. The section is scope-driven and market-agnostic;
    nothing here names a country (the hard-coded-Spain lesson is in the git
    log twice).

    Tiering follows ONBOARDING verbatim:
      Betriebe    Passung 'hoch' first; within that, carrying a DIRECT category
                  brand (Sunflex, Vitrocsa, ...) outranks everything — that
                  company demonstrably sells our category today, and the brand
                  names who we would displace.
      Architekten Relevanz 'hoch' + 'vergibt Aufträge' first — in Spain the
                  office genuinely awards the contract.
    Buyers on record are listed as the REFERENCE set, never mixed into the
    prospect tiers.
    """
    from sqlalchemy import select as _select

    from . import scope as _scope
    from .db import SessionLocal as _S
    from .enrich.extract import BRANDS_DIRECT
    from .models import Company as _C, CrmOrderEvent as _O

    with _S() as s:
        stmt = _scope.apply(_select(_C)).where(_C.is_intercompany.is_(False))
        ids = _filtered_company_ids(filters)
        if ids is not None:
            stmt = stmt.where(_C.id.in_(ids))
        pop = list(s.scalars(stmt))
        buys: dict[int, tuple[int, float, dt.date | None, float]] = {}
        if pop:
            pids = [c.id for c in pop]
            for cid, n, total, last, biggest in s.execute(
                    _select(_O.company_id, _sql_count(_O.id),
                            _sql_sum(_O.amount), _sql_max(_O.order_date),
                            _sql_max(_O.amount))
                    .where(_O.company_id.in_(pids)).group_by(_O.company_id)):
                buys[cid] = (n, float(total or 0), last, float(biggest or 0))
    if not pop:
        return []

    direct = {b.lower() for b in BRANDS_DIRECT}

    def _has_direct(c) -> list[str]:
        return [b for b in (c.competitor_brands or []) if b.lower() in direct]

    # ---- tiers ---------------------------------------------------------------
    buyers = sorted((c for c in pop if c.id in buys),
                    key=lambda c: -buys[c.id][1])
    prospects = [c for c in pop if c.id not in buys]
    betriebe_hoch = sorted(
        (c for c in prospects if c.solarlux_fit == "hoch"),
        key=lambda c: (not _has_direct(c), (c.name or "").lower()))
    arch_top = sorted(
        (c for c in prospects if c.solarlux_relevance == "hoch"
         and c.decision_role == "vergibt Aufträge"),
        key=lambda c: (c.name or "").lower())
    arch_next = sorted(
        (c for c in prospects if c.solarlux_relevance == "hoch"
         and c.decision_role != "vergibt Aufträge"),
        key=lambda c: (c.name or "").lower())

    n = len(pop)
    enriched = sum(1 for c in pop if c.enrichment_status == "enriched")
    with_site = sum(1 for c in pop if c.website_domain)
    verified = sum(1 for c in pop if c.identity_status == "verified")
    material = sum(1 for (_c, _t, _l, biggest) in buys.values() if biggest >= 2000)

    h2 = ParagraphStyle("qh2", parent=styles["Heading2"], textColor=INK,
                        fontSize=13, spaceBefore=16, spaceAfter=4)
    h3 = ParagraphStyle("qh3", parent=styles["Normal"], textColor=INK,
                        fontSize=10.5, leading=14, spaceBefore=10, spaceAfter=2)
    body = ParagraphStyle("qbody", parent=styles["Normal"], textColor=INK,
                          fontSize=9, leading=13)
    note = ParagraphStyle("qnote", parent=styles["Normal"], textColor=MUTED,
                          fontSize=7.5, leading=11, spaceBefore=2)
    cell = ParagraphStyle("qcell", parent=styles["Normal"], textColor=INK,
                          fontSize=8.5, leading=12)

    story: list = [
        Paragraph("Marktqualifizierung", h2),
        Paragraph(
            f"{n:,} Firmen im Bericht &nbsp;·&nbsp; Website bekannt: {with_site:,} "
            f"&nbsp;·&nbsp; davon nachweislich die eigene: {verified:,} "
            f"&nbsp;·&nbsp; angereichert: {enriched:,} &nbsp;·&nbsp; "
            f"Käufer im Bestand: {len(buys):,} (davon {material:,} mit Auftrag "
            f"ab 2.000&nbsp;€)".replace(",", "."), body),
        Paragraph("Die Einstufungen stammen von der jeweils eigenen Website "
                  "(Extraktion mit Beleg). <b>Passung</b>: verkauft der Betrieb "
                  "heute schon große Verglasungen? <b>Relevanz</b>: plant das Büro "
                  "Projekte, in die wir gehören? Firmen ohne Anreicherung fehlen "
                  "hier — nicht, weil sie unpassend wären, sondern weil noch "
                  "niemand nachgesehen hat.", note),
    ]

    def _rowtable(rows, widths):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG]),
            ("GRID", (0, 0), (-1, -1), 0.4, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    hdr = ParagraphStyle("qhdr", parent=styles["Normal"], textColor=colors.white,
                         fontSize=8.5, leading=11)

    # ---- Betriebe ------------------------------------------------------------
    if betriebe_hoch:
        story.append(Paragraph(
            f"Verarbeiter / Handel mit Passung <b>hoch</b> — {len(betriebe_hoch)} "
            "Firmen, noch ohne Kauf", h3))
        story.append(Paragraph(
            "Reihenfolge: Firmen mit einer <b>direkten Kategorie-Marke</b> "
            "(Sunflex, Vitrocsa, Sky-Frame …) zuerst — sie verkaufen unsere "
            "Produktkategorie nachweislich heute, die Marke sagt, wen wir "
            "verdrängen müssten.", note))
        rows = [[Paragraph("<b>Firma</b>", hdr), Paragraph("<b>Ort</b>", hdr),
                 Paragraph("<b>Kategorie-Marken</b>", hdr),
                 Paragraph("<b>Merkmale</b>", hdr)]]
        for c in betriebe_hoch[:40]:
            feats = [x for x, ok in (("eigene Fertigung", c.own_fabrication),
                                     ("montiert", c.installs),
                                     ("Showroom", c.has_showroom)) if ok]
            if c.partner_of:
                feats.append("Partner: " + ", ".join(c.partner_of[:3]))
            rows.append([
                Paragraph(_esc(c.name), cell), Paragraph(_esc(c.city or "—"), cell),
                Paragraph("<b>" + _esc(", ".join(_has_direct(c))) + "</b>"
                          if _has_direct(c) else "—", cell),
                Paragraph(_esc(" · ".join(feats)) or "—", cell)])
        story.append(_rowtable(rows, [52 * mm, 30 * mm, 40 * mm, 56 * mm]))
        if len(betriebe_hoch) > 40:
            story.append(Paragraph(f"… {len(betriebe_hoch) - 40} weitere im Explorer "
                                   "(Filter: Solarlux-Passung = hoch).", note))

    # ---- Architekten ----------------------------------------------------------
    if arch_top or arch_next:
        story.append(Paragraph(
            f"Architektur- und Planungsbüros mit Relevanz <b>hoch</b> — "
            f"{len(arch_top)} vergeben Aufträge, {len(arch_next)} weitere", h3))
        story.append(Paragraph(
            "Ein Büro verkauft nichts — es plant und schreibt aus. <b>vergibt "
            "Aufträge</b> heißt: Bauleitung / Ausschreibung / schlüsselfertig "
            "belegt auf der eigenen Website. Diese Büros zuerst.", note))
        rows = [[Paragraph("<b>Büro</b>", hdr), Paragraph("<b>Ort</b>", hdr),
                 Paragraph("<b>Rolle</b>", hdr), Paragraph("<b>Profil</b>", hdr)]]
        for c in (arch_top + arch_next)[:40]:
            prof = [p for p in (c.office_type,) if p]
            if c.project_focus:
                prof.append(", ".join(c.project_focus[:3]))
            if c.reference_scale:
                prof.append(str(c.reference_scale)[:60])
            rows.append([
                Paragraph(_esc(c.name), cell), Paragraph(_esc(c.city or "—"), cell),
                Paragraph("<b>vergibt Aufträge</b>" if c in arch_top
                          else _esc(c.decision_role or "unklar"), cell),
                Paragraph(_esc(" · ".join(prof)) or "—", cell)])
        story.append(_rowtable(rows, [52 * mm, 28 * mm, 30 * mm, 68 * mm]))
        if len(arch_top) + len(arch_next) > 40:
            story.append(Paragraph(f"… {len(arch_top) + len(arch_next) - 40} weitere im "
                                   "Explorer (Filter: Solarlux-Relevanz = hoch).", note))

    # ---- the reference set ----------------------------------------------------
    if buyers:
        story.append(Paragraph(f"Referenz: Käufer im Bestand — {len(buyers)} Firmen", h3))
        story.append(Paragraph(
            "Wer hier bereits kauft, ist der Maßstab für alles oben — und der "
            "Türöffner: eine Referenz in derselben Stadt schlägt jedes Argument.",
            note))
        rows = [[Paragraph("<b>Firma</b>", hdr), Paragraph("<b>Ort</b>", hdr),
                 Paragraph("<b>Segment</b>", hdr),
                 Paragraph("<b>Bestellungen</b>", hdr),
                 Paragraph("<b>Umsatz</b>", hdr), Paragraph("<b>zuletzt</b>", hdr)]]
        for c in buyers[:25]:
            cnt, total, last, _biggest = buys[c.id]
            rows.append([
                Paragraph(_esc(c.name), cell), Paragraph(_esc(c.city or "—"), cell),
                Paragraph(_esc(c.segment or "—"), cell),
                Paragraph(str(cnt), cell), Paragraph(_eur(total), cell),
                Paragraph(_de_date(last) if last else "—", cell)])
        story.append(_rowtable(rows, [50 * mm, 28 * mm, 26 * mm, 22 * mm, 26 * mm, 26 * mm]))

    # ---- honesty --------------------------------------------------------------
    if material < 30:
        story.append(Paragraph(
            f"<b>Warum hier kein Score steht:</b> in diesem Bericht haben "
            f"{material} Firmen einen Auftrag ab 2.000&nbsp;€ — unter 30 sind "
            "statistische Profile Rauschen. Die Rangfolge oben ist deshalb eine "
            "belegte <b>Checkliste</b> (Passung, Marken, Entscheidungsrolle), "
            "kein trainiertes Modell. Ehrlich gereiht schlägt falsch gewichtet.",
            note))
    return story


def _sql_count(col):
    from sqlalchemy import func as _f
    return _f.count(col)


def _sql_sum(col):
    from sqlalchemy import func as _f
    return _f.sum(col)


def _sql_max(col):
    from sqlalchemy import func as _f
    return _f.max(col)


def build_report(path: str | None = None, filters: dict | None = None) -> str:
    if path is None:
        path = str(next_report_path("adwatch_report"))
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    data = latest_metrics(_filtered_company_ids(filters))
    links = _page_link_map([d["company_id"] for d in data])
    n_active = sum(1 for d in data if d.get("has_data") and (d.get("total_active_ads") or 0) > 0)
    source_label = _source_label(data)

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], textColor=INK, fontSize=21, spaceAfter=2, alignment=0)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=MUTED, fontSize=9)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=INK, fontSize=13, spaceBefore=16, spaceAfter=5)
    body = ParagraphStyle("body", parent=styles["Normal"], textColor=INK, fontSize=9.5, leading=13)
    note = ParagraphStyle("note", parent=styles["Normal"], textColor=MUTED, fontSize=7.5, leading=11, spaceBefore=3)
    cellh = ParagraphStyle("cellh", parent=styles["Normal"], textColor=colors.white, fontSize=8.5, leading=11)
    cellhr = ParagraphStyle("cellhr", parent=cellh, alignment=TA_RIGHT)
    cell = ParagraphStyle("cell", parent=styles["Normal"], textColor=INK, fontSize=8.5, leading=12)
    cellr = ParagraphStyle("cellr", parent=cell, alignment=TA_RIGHT)
    detail = ParagraphStyle("detail", parent=styles["Normal"], textColor=INK, fontSize=9, leading=13, spaceAfter=1)
    detailm = ParagraphStyle("detailm", parent=detail, textColor=MUTED)

    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=15 * mm, bottomMargin=16 * mm,
                            title="AdWatch — Anzeigen-Aktivitätsbericht")
    story = [
        Paragraph("Anzeigen-Aktivitätsbericht", h1),
        Paragraph(f"Erstellt am {_de_datetime(dt.datetime.now())} &nbsp;·&nbsp; "
                  f"live · {config.LIVE_SOURCE} &nbsp;·&nbsp; Quelle: {source_label}", sub),
        Spacer(1, 9),
        _scope_banner(filters, len(data), n_active, styles),
        Spacer(1, 13),
    ]
    story += _divergence_story(filters, links=links)
    # The scorecard: who to call first, judged from website evidence. Carries a
    # market that has no ad data and too few buyers for an ICP — which is every
    # market except Germany today.
    story += _qualification_story(filters, styles)

    if not data:
        story.append(Paragraph("Keine Firmen entsprechen dem gewählten Filter.", body))
        doc.build(story)
        return path

    # active advertisers first (most active on top), then the rest by name
    def _sortkey(d):
        act = d.get("total_active_ads") or 0
        has = 1 if (d.get("has_data") and act > 0) else 0
        return (-has, -act, (d["company"] or "").lower())
    data = sorted(data, key=_sortkey)

    # ---- overview table ----
    story.append(Paragraph("Übersicht", h2))
    header = ["Firma", "Aktive Anz.", "Personal", "Verkauf", "Marke", "Gesch. Ausg./Wo."]
    rows = [[Paragraph(header[0], cellh)] + [Paragraph(h, cellhr) for h in header[1:]]]
    for d in data:
        cats = d.get("ads_by_category") or {}
        name = Paragraph(_esc(d["company"]), cell)
        if not d.get("has_data"):
            dash = Paragraph("—", cellr)
            rows.append([name, dash, dash, dash, dash,
                         Paragraph('<font color="#647380">keine Daten</font>', cellr)])
            continue
        act = d["total_active_ads"] or 0
        active_txt = (f"<b>{act}</b>" + _delta_frag(d.get("delta_ads"))) if act else "0"
        spend = "0" if act == 0 else f"{_eur(d['spend_low'])}–{_eur(d['spend_high'])}"
        rows.append([
            name,
            Paragraph(active_txt, cellr),
            Paragraph(str(cats.get("recruitment", 0)), cellr),
            Paragraph(str(cats.get("product_sale", 0)), cellr),
            Paragraph(str(cats.get("brand_awareness", 0)), cellr),
            Paragraph(_esc(spend), cellr),
        ])

    table = Table(rows, colWidths=[60 * mm, 25 * mm, 19 * mm, 18 * mm, 16 * mm, 40 * mm], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("LINEBELOW", (0, 0), (-1, 0), 0, ACCENT),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]
    table.setStyle(TableStyle(style))
    story.append(table)

    # ---- Firmenprofile: the enriched picture per company. Comes BEFORE the ad
    # detail blocks and does not depend on ad data, so a market that has never
    # been fetched still produces a substantive report. ----
    story += _profiles_story(data, filters, styles)

    # ---- detail: only the active advertisers (keeps the report uncluttered) ----
    active_rows = [d for d in data if d.get("has_data") and (d.get("total_active_ads") or 0) > 0]
    if active_rows:
        story.append(Paragraph("Details — aktive Werbetreibende", h2))
        for d in active_rows:
            cats = d.get("ads_by_category") or {}
            method_de = _METHOD_LABEL_DE.get(d.get("spend_method"), d.get("spend_method") or "")
            page = (f' &nbsp;·&nbsp; Seite: {_esc(_page_label(d["page_name"]))}'
                    if d.get("page_name") else "")
            cat_str = " · ".join(f"{_CATEGORY_LABEL_DE.get(k, k)} {v}"
                                 for k, v in cats.items() if v) or "—"
            products = ", ".join(d.get("products") or [])
            story.append(Paragraph(f'<b>{_esc(d["company"])}</b>{page}', detail))
            story.append(Paragraph(
                f'{d["total_active_ads"]} aktive Anzeigen &nbsp;·&nbsp; {cat_str} &nbsp;·&nbsp; '
                f'Ausgaben ~{_eur(d["spend_low"])}–{_eur(d["spend_high"])}/Wo. ({method_de})', detailm))
            if products:
                story.append(Paragraph(f'Produkte: {_esc(products)}', detailm))
            cta = _ads_cta(links.get(d["company_id"]))
            if cta:
                story.append(Paragraph(cta, detail))
            story.append(Spacer(1, 6))

    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Die Links <b>„Aktive Anzeigen ansehen“</b> (Meta Ad Library) und <b>„Google-Anzeigen "
        "ansehen“</b> (Google Ads Transparency) öffnen die laufenden Anzeigen der Firma — sobald eine "
        "Meta-Seiten- bzw. Google-Advertiser-ID hinterlegt ist. (+/-) = Veränderung ggü. Vorwoche. "
        "Ausgaben sind ein <b>geschätzter</b> Intervallwert (von–bis), keine offiziell veröffentlichte "
        "Zahl — nur regulierte Kategorien werden offengelegt, sonst geschätzt aus "
        "EU-Reichweite/Anzeigenzahl (Annahmen: spend_assumptions.yaml).", note))

    doc.build(story)
    return path


def _signal(cats: dict, products: list) -> str:
    """A one-line read on what a company is mainly doing with its ads."""
    hire = cats.get("recruitment", 0)
    sell = cats.get("product_sale", 0)
    event = cats.get("event_promo", 0)
    bits = []
    if hire and hire >= sell:
        bits.append(f"Personalsuche ({hire} Anzeigen)")
    if sell:
        bits.append(f"Verkauf{' — ' + ', '.join(products[:3]) if products else ''} ({sell} Anzeigen)")
    if event:
        bits.append(f"Veranstaltungen ({event} Anzeigen)")
    return "; ".join(bits) or "Marke / allgemeine Präsenz"


def build_top5_report(path: str | None = None, filters: dict | None = None) -> str:
    """A focused PDF: the 5 most active advertisers this week, with insights."""
    if path is None:
        path = str(next_report_path("adwatch_top5"))
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_metrics = latest_metrics(_filtered_company_ids(filters))
    data = [d for d in all_metrics if d["has_data"] and (d["total_active_ads"] or 0) > 0]
    # rank by activity score (falls back to ad count for rows without one)
    data.sort(key=lambda d: (d.get("score") or 0, d["total_active_ads"]), reverse=True)
    top = data[:5]
    n_active = len(data)
    card_links = _page_link_map([d["company_id"] for d in top])
    source_label = _source_label(all_metrics)

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], textColor=INK, fontSize=20, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=MUTED, fontSize=9)
    rank = ParagraphStyle("rank", parent=styles["Heading2"], textColor=ACCENT, fontSize=14, spaceBefore=12, spaceAfter=2)
    body = ParagraphStyle("body", parent=styles["Normal"], textColor=INK, fontSize=10, leading=14)
    note = ParagraphStyle("note", parent=styles["Normal"], textColor=MUTED, fontSize=8, leading=11)
    fact = ParagraphStyle("t5fact", parent=body, fontSize=9, leading=12.5)
    est = ParagraphStyle("t5est", parent=body, fontSize=9, leading=12.5,
                         textColor=colors.HexColor("#4a5568"))

    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    week = next((d["week_start"] for d in top if d.get("week_start")), None)
    week_de = _de_date(dt.date.fromisoformat(week)) if week else None
    # Don't promise five when the data holds two: say what is actually shown, so
    # a short list reads as a finding about the market rather than a broken report.
    shown = len(top)
    story = [
        Paragraph("Top 5 Werbetreibende — Anzeigen-Aktivitätsbericht" if shown >= 5
                  else f"Werbetreibende mit aktiven Anzeigen ({shown}) — "
                       "Anzeigen-Aktivitätsbericht", h1),
        Paragraph(
            f"Erstellt am {_de_datetime(dt.datetime.now())}"
            + (f" &nbsp;·&nbsp; Woche vom {week_de}" if week_de else "")
            + f" &nbsp;·&nbsp; live · {config.LIVE_SOURCE} &nbsp;·&nbsp; Quelle: {source_label}", sub),
        Spacer(1, 9),
        _scope_banner(filters, len(all_metrics), n_active, styles),
        Spacer(1, 13),
    ]
    story += _divergence_story(filters)

    if not top:
        story.append(Paragraph("Keine Unternehmen mit aktiven Anzeigen in den aktuellen Daten.", body))
        doc.build(story)
        return path

    if shown < 5:
        # German needs the singular here — "Es werden 1 Firmen gezeigt" appeared in
        # a report that actually went out to colleagues.
        shown_de = "wird 1 Firma" if shown == 1 else f"werden {shown} Firmen"
        active_de = (f"nur 1 von {len(all_metrics)} Firmen im Bericht-Umfang aktive Anzeigen hatte"
                     if n_active == 1 else
                     f"nur {n_active} von {len(all_metrics)} Firmen im Bericht-Umfang aktive "
                     "Anzeigen hatten")
        story.append(Paragraph(
            f"<b>Hinweis:</b> Es {shown_de} gezeigt, weil in der letzten Erfassung "
            f"{active_de} — die Liste ist nicht gekürzt.", note))
        story.append(Spacer(1, 8))

    enr = _enrichment_map([d["company_id"] for d in top])

    for i, d in enumerate(top, start=1):
        cats = d.get("ads_by_category") or {}
        products = d.get("products") or []
        score_tag = f" &nbsp;·&nbsp; Score {d['score']:.0f}/100" if d.get("score") is not None else ""
        story.append(Paragraph(f"{i}. {_esc(d['company'])}{score_tag}", rank))
        matched = (f" &nbsp;·&nbsp; Seite: {_esc(_page_label(d['page_name']))}"
                   if d.get("page_name") else "")
        story.append(Paragraph(
            f"<b>{d['total_active_ads']} aktive Anzeigen</b>"
            + (f" &nbsp;({'+' if (d.get('delta_ads') or 0) > 0 else ''}{d['delta_ads']} ggü. Vorwoche)"
               if d.get("delta_ads") not in (None, 0) else "")
            + matched, body))
        cta = _ads_cta(card_links.get(d["company_id"]))
        if cta:
            story.append(Paragraph(cta, body))

        # Who the company is, before what it advertises: the enrichment is the
        # only part of this report that explains WHY the ad activity matters.
        e = enr.get(d["company_id"]) or {}
        if e.get("description"):
            story.append(Paragraph(f'<b>Beschreibung:</b> {_esc(e["description"])}', fact))
        if e.get("assessment"):
            story.append(Paragraph("<b>Einschätzung (KI, unbestätigt):</b> "
                                   f'<i>{_esc(e["assessment"])}</i>', est))
        profile_bits = []
        if e.get("products"):
            profile_bits.append("Produkte (Website): " + _esc(", ".join(e["products"][:6])))
        if e.get("employee_hint"):
            profile_bits.append("Größe: " + _esc(str(e["employee_hint"])))
        if e.get("founded_year"):
            profile_bits.append(f'Gegründet: {e["founded_year"]}')
        if e.get("mentions_solarlux") is True:
            profile_bits.append("<b>nennt Solarlux</b>")
        if e.get("competitor_brands"):
            profile_bits.append("Wettbewerber auf der Website: <b>"
                                + _esc(", ".join(e["competitor_brands"][:5])) + "</b>")
        if profile_bits:
            story.append(Paragraph(" &nbsp;·&nbsp; ".join(profile_bits), note))

        story.append(Paragraph(f"<b>Signal:</b> {_esc(_signal(cats, products))}", body))
        method_de = _METHOD_LABEL_DE.get(d.get("spend_method"), d.get("spend_method"))
        story.append(Paragraph(
            f"<b>Aufschlüsselung:</b> Personalsuche {cats.get('recruitment', 0)} · "
            f"Verkauf {cats.get('product_sale', 0)} · Marke {cats.get('brand_awareness', 0)} · "
            f"Veranstaltungen {cats.get('event_promo', 0)}", body))
        if products:
            story.append(Paragraph(f"<b>Beworbene Produkte:</b> {_esc(', '.join(products))}", body))
        story.append(Paragraph(
            f"<b>Gesch. Ausgaben/Woche:</b> {_eur(d['spend_low'])}–{_eur(d['spend_high'])} "
            f"(geschätzt, {method_de})", body))
        story.append(Spacer(1, 4))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Die Links <b>„Aktive Anzeigen ansehen“</b> (Meta Ad Library) und <b>„Google-Anzeigen "
        "ansehen“</b> (Google Ads Transparency) öffnen die laufenden Anzeigen der Firma. Ausgaben sind "
        "ein <b>geschätzter</b> Intervallwert (von–bis), nicht offiziell veröffentlicht. Rangfolge "
        "nach Aktivität in der letzten wöchentlichen Erfassung. <b>Beschreibung</b> und "
        "<b>Produkte (Website)</b> sind von der eigenen Website der Firma belegt; die "
        "<b>Einschätzung</b> ist eine KI-gestützte Ableitung daraus — plausibel, aber "
        "<b>keine belegte Angabe</b> und vor einer Ansprache zu prüfen. <b>Beworbene "
        "Produkte</b> stammen aus dem Anzeigentext und sind auf die deutschen "
        "Produktfamilien normalisiert.", note))
    doc.build(story)
    return path


REPORT_TYPE_LABEL = {"top5": "Top 5", "full": "Full report"}


def _meta_path(pdf_path) -> Path:
    """Sidecar file that records WHY a report looks the way it does (the filter
    scope, and the saved-definition name if any) — a plain `<pdf>.meta.json`
    next to the PDF, so it never matches the `adwatch_*.pdf` report glob."""
    return Path(str(pdf_path) + ".meta.json")


def write_report_meta(pdf_path, filters: dict | None = None,
                      definition_name: str | None = None,
                      source: str | None = None) -> None:
    """Persist the filter scope of a just-generated report so the Reports list
    can label it (e.g. 'Gefiltert nach: …') instead of an indistinguishable
    'Report — KW30'. Best-effort: a failure here must never break generation.

    Also records the 'created' audit event for the Logs tab — every build path
    already calls this, which makes it the one place that cannot be forgotten."""
    label = None
    try:
        label = _describe_filters_de(filters)
        meta = {"filter_label": label, "definition": definition_name}
        _meta_path(pdf_path).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 — labelling is cosmetic, never fatal
        pass
    from . import report_log
    name = Path(str(pdf_path)).name
    report_log.record("created", name, scope=label,
                      report_type=(parse_report_filename(name) or {}).get("report_type"),
                      source=source or ("definition" if definition_name else None),
                      detail=(f"Definition: {definition_name}" if definition_name else None))


def _read_report_meta(pdf_path) -> dict:
    try:
        p = _meta_path(pdf_path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def list_reports() -> list[dict]:
    """Every generated report still on disk, newest first."""
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in config.OUTPUT_DIR.glob("adwatch_*.pdf"):
        parsed = parse_report_filename(f.name)
        stat = f.stat()
        week = (parsed or {}).get("label", "").replace("_", " ")
        version = (parsed or {}).get("version")
        label = f"{REPORT_TYPE_LABEL.get((parsed or {}).get('report_type'), 'Report')} — {week or f.stem}"
        if version:
            label += f" (v{version})"
        meta = _read_report_meta(f)
        out.append({
            "filename": f.name,
            "report_type": (parsed or {}).get("report_type", "unknown"),
            "label": label,
            "filter_label": meta.get("filter_label"),   # None -> a plain, unfiltered report
            "definition": meta.get("definition"),       # set when produced by a saved ReportDefinition
            "size_bytes": stat.st_size,
            "created_at": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes"),
        })
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out
