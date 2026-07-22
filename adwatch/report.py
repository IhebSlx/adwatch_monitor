"""Generate the weekly PDF report from stored metrics.

Filenames are named after the ISO calendar week (e.g. adwatch_top5_KW29_2026.pdf)
rather than the exact day, since one report is meant to represent one week.
Generating again within the same week never overwrites the previous file —
it gets an incrementing suffix instead (_01, _02, ...), so every past report
stays available in output/."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
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
    "locked": "gesperrt", "confirmed": "bestätigt", "ambiguous": "mehrdeutig",
    "no_ads_found": "keine Seite gefunden", "pending": "nicht geprüft",
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
        bits.append({"active": "nur mit aktiven Anzeigen", "any": "nur je beworben",
                     "none": "nur ohne aktive Anzeigen"}.get(filters["ad_activity"], filters["ad_activity"]))
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
        bits.append("nur mit Page-ID")
    elif filters.get("page_id_state") == "without":
        bits.append("nur ohne Page-ID")
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

    # ---- detail: only the active advertisers (keeps the report uncluttered) ----
    active_rows = [d for d in data if d.get("has_data") and (d.get("total_active_ads") or 0) > 0]
    if active_rows:
        story.append(Paragraph("Details — aktive Werbetreibende", h2))
        for d in active_rows:
            cats = d.get("ads_by_category") or {}
            method_de = _METHOD_LABEL_DE.get(d.get("spend_method"), d.get("spend_method") or "")
            page = f' &nbsp;·&nbsp; Seite: {_esc(d["page_name"])}' if d.get("page_name") else ""
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

    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    week = next((d["week_start"] for d in top if d.get("week_start")), None)
    week_de = _de_date(dt.date.fromisoformat(week)) if week else None
    story = [
        Paragraph("Top 5 Werbetreibende — Anzeigen-Aktivitätsbericht", h1),
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

    for i, d in enumerate(top, start=1):
        cats = d.get("ads_by_category") or {}
        products = d.get("products") or []
        score_tag = f" &nbsp;·&nbsp; Score {d['score']:.0f}/100" if d.get("score") is not None else ""
        story.append(Paragraph(f"{i}. {_esc(d['company'])}{score_tag}", rank))
        matched = f" &nbsp;·&nbsp; Seite: {_esc(d['page_name'])}" if d.get("page_name") else ""
        story.append(Paragraph(
            f"<b>{d['total_active_ads']} aktive Anzeigen</b>"
            + (f" &nbsp;({'+' if (d.get('delta_ads') or 0) > 0 else ''}{d['delta_ads']} ggü. Vorwoche)"
               if d.get("delta_ads") not in (None, 0) else "")
            + matched, body))
        cta = _ads_cta(card_links.get(d["company_id"]))
        if cta:
            story.append(Paragraph(cta, body))
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
        "nach Aktivität in der letzten wöchentlichen Erfassung.", note))
    doc.build(story)
    return path


REPORT_TYPE_LABEL = {"top5": "Top 5", "full": "Full report"}


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
        out.append({
            "filename": f.name,
            "report_type": (parsed or {}).get("report_type", "unknown"),
            "label": label,
            "size_bytes": stat.st_size,
            "created_at": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes"),
        })
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out
