"""Generate the weekly PDF report from stored metrics."""
from __future__ import annotations

import datetime as dt

from reportlab.lib import colors
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
MUTED = colors.HexColor("#647380")
LINE = colors.HexColor("#d9e2ec")
BG = colors.HexColor("#f0f4f8")


def _eur(v) -> str:
    if v is None:
        return "-"
    return f"€{v:,.0f}".replace(",", ".")


def build_report(path: str | None = None) -> str:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if path is None:
        stamp = dt.date.today().isoformat()
        path = str(config.OUTPUT_DIR / f"adwatch_report_{stamp}.pdf")

    data = latest_metrics()
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], textColor=INK, fontSize=20, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=MUTED, fontSize=9)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=ACCENT, fontSize=13, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["Normal"], textColor=INK, fontSize=9.5, leading=13)
    note = ParagraphStyle("note", parent=styles["Normal"], textColor=MUTED, fontSize=8, leading=11)
    cellh = ParagraphStyle("cellh", parent=styles["Normal"], textColor=colors.white, fontSize=8.5, leading=11)
    cell = ParagraphStyle("cell", parent=styles["Normal"], textColor=INK, fontSize=8.5, leading=11)

    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    story = []
    mode_tag = "SAMPLE DATA (mock mode)" if config.MODE != "live" else f"live · {config.LIVE_SOURCE}"
    story.append(Paragraph("Competitor Ad-Activity Report", h1))
    story.append(Paragraph(f"Generated {dt.datetime.now():%d %b %Y, %H:%M} &nbsp;·&nbsp; {mode_tag} &nbsp;·&nbsp; Meta Ad Library", sub))
    story.append(Spacer(1, 8))

    # ---- summary table ----
    header = ["Company", "Active ads", "Hiring", "Selling", "Est. spend / wk"]
    rows = [[Paragraph(h, cellh) for h in header]]
    for d in data:
        cats = d.get("ads_by_category") or {}
        if not d["has_data"]:
            spend = "no data"
            active = "-"
            hiring = selling = "-"
        else:
            active = str(d["total_active_ads"])
            if d.get("delta_ads") is not None:
                arrow = "▲" if d["delta_ads"] > 0 else ("▼" if d["delta_ads"] < 0 else "=")
                active += f"  {arrow}{abs(d['delta_ads'])}"
            hiring = str(cats.get("recruitment", 0))
            selling = str(cats.get("product_sale", 0))
            if d["total_active_ads"] == 0:
                spend = "0"
            else:
                spend = f"{_eur(d['spend_low'])}–{_eur(d['spend_high'])}"
        rows.append([
            Paragraph(d["company"], cell),
            Paragraph(active, cell),
            Paragraph(hiring, cell),
            Paragraph(selling, cell),
            Paragraph(spend, cell),
        ])

    table = Table(rows, colWidths=[62 * mm, 24 * mm, 16 * mm, 16 * mm, 44 * mm], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    for i in range(1, len(rows)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), BG))
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Spend is a <b>modelled estimate</b> (low–high interval), not a figure published by Meta. "
        "Meta only discloses spend for regulated ad categories; everything else is estimated from "
        "EU reach data or ad counts. See spend_assumptions.yaml for the assumptions used.", note))

    # ---- per-company detail ----
    for d in data:
        story.append(Paragraph(d["company"], h2))
        if not d["has_data"]:
            story.append(Paragraph(f"Status: {d['resolution_status']} · no collection data yet.", body))
            continue
        if d["total_active_ads"] == 0:
            story.append(Paragraph("Confirmed page — <b>0 active ads</b> this week (not an error).", body))
            continue
        cats = d.get("ads_by_category") or {}
        cat_str = ", ".join(f"{k}: {v}" for k, v in cats.items() if v) or "-"
        products = ", ".join(d.get("products") or []) or "-"
        story.append(Paragraph(f"<b>Total active ads:</b> {d['total_active_ads']}", body))
        story.append(Paragraph(f"<b>By category:</b> {cat_str}", body))
        story.append(Paragraph(f"<b>Products advertised:</b> {products}", body))
        story.append(Paragraph(
            f"<b>Estimated spend/week:</b> {_eur(d['spend_low'])}–{_eur(d['spend_high'])} "
            f"(modelled est., method: {d.get('spend_method')})", body))

    doc.build(story)
    return path
