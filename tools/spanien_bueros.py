"""Dossier: jedes Architekturbüro, das in Spanien baut — eines nach dem anderen.

Der Marktbericht (`tools/spanien_bericht.py`) fasst die 231 Büros in zwei
Abschnitten zusammen. Das reicht, um den Markt zu verstehen, und nicht, um
jemanden anzurufen. Dieser Bericht ist die ausgeschriebene Fassung von
Abschnitt 5 und 6: ein Eintrag je Büro, mit Link, Kontakt, Orten und dem, was
die Website über das Büro hergibt.

DIE REIHENFOLGE IST DAS EIGENTLICHE ERGEBNIS.
231 Einträge in alphabetischer Ordnung wären ein Telefonbuch. Sortiert wird
deshalb in vier Stufen, und die Regel steht im Bericht selbst — eine
Rangfolge, deren Kriterium man nicht sieht, ist eine Behauptung:

    A  schon zusammengearbeitet      Beziehung 3 oder 4
    B  baut, wo wir gewinnen         Balearen und Zweitwohnsitzküste
    C  schon einmal berührt          Beziehung 1 oder 2
    D  die übrigen                   nach Zahl der spanischen Orte

Bewusst NICHT im Rang: die Spalte `decision_role` („vergibt Aufträge").
Sie wurde gegen echte Ausgänge geprüft und trennt nicht. Sie steht im
Eintrag, weil sie sagt, worüber man mit dem Büro reden kann — aber sie
schiebt niemanden nach oben.

KEINE PERSONENDATEN. Firmenanschrift, Zentrale und Rollen-Postfächer
(info@, contact@ …) sind Firmenangaben und stehen drin. Eine Adresse, die
nach einem Vornamen aussieht, wird unterdrückt; sie steht in AdWatch.

Aufruf:
    python tools/spanien_bueros.py       -> output/Solarlux_Spanien_Bueros_<datum>.pdf
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reportlab.lib import colors                                      # noqa: E402
from reportlab.lib.enums import TA_RIGHT                              # noqa: E402
from reportlab.lib.pagesizes import A4                                # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm                                    # noqa: E402
from reportlab.platypus import (                                      # noqa: E402
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from sqlalchemy import text as _sql                                   # noqa: E402

from adwatch import config, taetigkeit                                # noqa: E402
from adwatch.db import SessionLocal                                   # noqa: E402
from adwatch.enrich.laender import LAND_VORWAHL                       # noqa: E402
from adwatch.report import (                                          # noqa: E402
    ACCENT, ACCENT_SOFT, BG, INK, LINE, MUTED, _de_datetime, _esc, _link,
)

LAND = "ES"

# Die Orte, an denen Solarlux in Spanien tatsächlich gewinnt: Balearen und die
# Zweitwohnsitzküste. Hergeleitet, nicht geraten — im Marktbericht liegen dort
# 7 von 9 gewonnenen Verkaufschancen und 90 % des gewonnenen Auftragswerts.
_GEWINNREGION = (
    "mallorca", "palma", "calvi", "santa pon", "andratx", "santany", "illetas",
    "palmanova", "cala ", "sa pobla", "ibiza", "eivissa", "menorca", "formentera",
    "balear", "llucmajor", "montuiri", "alcudia", "pollen", "soller", "manacor",
    "felanitx", "deia", "valldemossa", "marbella", "javea", "xabia", "teulada",
    "moraira", "torremolinos", "denia", "altea", "estepona", "nerja", "mijas",
    "benahavis", "sotogrande", "calpe", "benissa", "fuengirola",
)

# Postfächer, die einer Rolle gehören und keiner Person.
_ROLLENPOSTFACH = re.compile(
    r"^(info|kontakt|contact|contacto|office|mail|e?mail|studio|estudio|hello|hallo|"
    r"correo|administracion|administracio|admin|arquitectura|arquitectos|buero|"
    r"b[uü]ro|post|zentrale|welcome|reception|recepcion|empfang|anfrage|projekt|"
    r"projects|proyectos|sekretariat|general|hi|team)([.\-_]?\w*)?$", re.I)

_VORWAHLEN = sorted(LAND_VORWAHL, key=len, reverse=True)


def _land_fraglich(land: str | None, telefon: str | None) -> str | None:
    """Das Land, auf das die Telefonvorwahl deutet — wenn es dem CRM widerspricht.

    Gemessen 2026-09-08: sieben der 231 Büros stehen im CRM als DE und
    telefonieren unter +44, +353 oder +370. Fünf davon sitzen in London. In
    einem Bericht, der aus dem Haus geht, liest sich „London · DE" wie ein
    Fehler des Berichts — er ist einer der Daten, und deshalb steht er
    markiert da statt stillschweigend korrigiert.

    Nur europäische Vorwahlen; +52 (Mexiko) etwa fällt durch.
    """
    if not land or not telefon:
        return None
    nummer = re.sub(r"[^\d+]", "", telefon)
    if not nummer.startswith("+"):
        return None
    for vorwahl in _VORWAHLEN:
        if nummer.startswith(vorwahl):
            soll = LAND_VORWAHL[vorwahl]
            return soll if soll != land else None
    return None


STUFENTEXT = {5: "gemeinsames Objekt gewonnen", 4: "auf einer Verkaufschance benannt",
              3: "Schriftverkehr", 2: "Debitor angelegt", 1: "als Lead erfasst",
              0: "nur Stammdaten"}


def _ist_gewinnregion(orte) -> list[str]:
    """Die Orte des Büros, die in der Gewinnregion liegen — leer, wenn keiner."""
    return [o for o in orte if any(k in o.lower() for k in _GEWINNREGION)]


def _postfach_zeigen(mail: str | None) -> str | None:
    """Die Adresse, wenn sie einer Rolle gehört; sonst None.

    Ein Postfach wie `bruno@patrickgenard.com` ist eine Person, und Personen
    stehen hier nicht drin. `a3@a3arquitectos.es` ist keine — deshalb gilt
    auch als Rolle, was im Domainnamen wieder auftaucht.
    """
    if not mail or "@" not in mail:
        return None
    lokal, _, domain = mail.partition("@")
    if _ROLLENPOSTFACH.match(lokal):
        return mail
    kurz = re.sub(r"[^a-z0-9]", "", lokal.lower())
    if kurz and kurz in re.sub(r"[^a-z0-9]", "", domain.lower()):
        return mail
    return None


# ---------------------------------------------------------------------------
# Messen
# ---------------------------------------------------------------------------

def erheben() -> dict:
    b = taetigkeit.bueros(land=LAND)
    zeilen = b["rows"]
    inp = ",".join(str(z["id"]) for z in zeilen)
    with SessionLocal() as s:
        zusatz = {r[0]: r for r in s.execute(_sql(f"""
            SELECT id, street, postal_code, city, country, phone, email,
                   website_domain, office_type, service_area, project_focus,
                   reference_scale, description, assessment, site_language,
                   solarlux_relevance, relation_why, decision_role_evidence,
                   COALESCE(arch_projects,0), legal_form, founded_year,
                   employee_hint, certifications, linkedin_url, instagram_url,
                   sap_number, crm_created_on
            FROM companies WHERE id IN ({inp})"""))}
    felder = ("id", "street", "plz", "ort", "land", "telefon", "email", "domain",
              "buerotyp", "taetigkeitsgebiet", "schwerpunkte", "referenzgroesse",
              "beschreibung", "einschaetzung", "sprache", "relevanz", "beziehung_warum",
              "rollenbeleg", "crm_projekte", "rechtsform", "gegruendet", "mitarbeiter",
              "zertifikate", "linkedin", "instagram", "sap", "angelegt")
    for z in zeilen:
        r = zusatz.get(z["id"])
        z["d"] = dict(zip(felder, r)) if r else {}
        z["gewinnorte"] = _ist_gewinnregion(z["orte"])
    return {"zeilen": zeilen, "bueros": b["bueros"], "orte": b["orte"]}


def einordnen(zeilen: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """Die vier Stufen. Jedes Büro genau einmal, die Regel steht im Bericht."""
    def rang(z):
        return (-(z["stufe"] or 0), -len(z["gewinnorte"]), -len(z["orte"]), z["name"] or "")

    a = sorted([z for z in zeilen if (z["stufe"] or 0) >= 3], key=rang)
    rest = [z for z in zeilen if (z["stufe"] or 0) < 3]
    b = sorted([z for z in rest if z["gewinnorte"]], key=rang)
    inb = {id(z) for z in b}
    c = sorted([z for z in rest if id(z) not in inb and (z["stufe"] or 0) >= 1], key=rang)
    inc = {id(z) for z in c}
    d = sorted([z for z in rest if id(z) not in inb and id(z) not in inc], key=rang)
    return [
        ("A", "Schon zusammengearbeitet", a),
        ("B", "Baut dort, wo wir gewinnen", b),
        ("C", "Schon einmal berührt", c),
        ("D", "Die übrigen", d),
    ]


# ---------------------------------------------------------------------------
# Setzen
# ---------------------------------------------------------------------------

def bauen(daten: dict, pfad: str | None = None) -> str:
    heute = dt.date.today()
    if pfad is None:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        pfad = str(config.OUTPUT_DIR / f"Solarlux_Spanien_Bueros_{heute:%Y-%m-%d}.pdf")

    st = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=st["Title"], textColor=INK, fontSize=21,
                        alignment=0, spaceAfter=3)
    h2 = ParagraphStyle("h2", parent=st["Heading2"], textColor=INK, fontSize=13,
                        spaceBefore=14, spaceAfter=5)
    sub = ParagraphStyle("sub", parent=st["Normal"], textColor=MUTED, fontSize=9, leading=12.5)
    body = ParagraphStyle("body", parent=st["Normal"], textColor=INK, fontSize=9.5,
                          leading=13.5, spaceAfter=5)
    note = ParagraphStyle("note", parent=st["Normal"], textColor=MUTED, fontSize=8, leading=11)
    kname = ParagraphStyle("kname", parent=st["Normal"], textColor=INK, fontSize=10.5,
                           leading=13.5)
    kzeile = ParagraphStyle("kzeile", parent=st["Normal"], textColor=INK, fontSize=8.5,
                            leading=11.5)
    klabel = ParagraphStyle("klabel", parent=kzeile, textColor=MUTED)
    kbadge = ParagraphStyle("kbadge", parent=st["Normal"], textColor=colors.white,
                            fontSize=8, leading=10.5, alignment=TA_RIGHT)

    doc = SimpleDocTemplate(pfad, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=15 * mm, bottomMargin=17 * mm,
                            title="Solarlux — Architekturbüros mit Projekten in Spanien",
                            author="AdWatch")
    B = doc.width
    stufen = einordnen(daten["zeilen"])
    zaehler = {k: len(v) for k, _, v in stufen}

    def zeile(label: str, wert: str) -> list:
        return [Paragraph(label, klabel), Paragraph(wert, kzeile)]

    def karte(z: dict, nr: int) -> KeepTogether:
        d = z["d"]
        domain = (d.get("domain") or z.get("website") or "").strip()
        url = f"https://{domain}" if domain else None
        stufe = z["stufe"] or 0

        kopf = Table([[
            Paragraph(f"<b>{nr}. {_esc(z['name'])}</b>", kname),
            Paragraph(f"Beziehung {stufe} von 5", kbadge),
        ]], colWidths=[B - 92, 92 - 12])
        kopf.setStyle(TableStyle([
            ("BACKGROUND", (1, 0), (1, 0), ACCENT if stufe >= 3 else MUTED),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (1, 0), (1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))

        rr: list[list] = []
        if url:
            rr.append(zeile("Website", _link(domain, url)))
        anschrift = ", ".join(x for x in (
            d.get("street"), " ".join(x for x in (d.get("plz"), d.get("ort")) if x),
            d.get("land")) if x)
        if anschrift:
            text_sitz = _esc(anschrift)
            fraglich = _land_fraglich(d.get("land"), d.get("telefon"))
            if fraglich:
                text_sitz += (f' <font size="7.5" color="#647380">— Ländercode im CRM '
                              f'fraglich, die Vorwahl deutet auf {fraglich}</font>')
            rr.append(zeile("Sitz", text_sitz))
        kontakt = []
        if d.get("telefon"):
            kontakt.append(_esc(d["telefon"]))
        postfach = _postfach_zeigen(d.get("email"))
        if postfach:
            kontakt.append(_link(postfach, f"mailto:{postfach}"))
        elif d.get("email"):
            kontakt.append('<font color="#647380">persönliche Adresse — in AdWatch</font>')
        if d.get("linkedin"):
            kontakt.append(_link("LinkedIn", d["linkedin"]))
        if kontakt:
            rr.append(zeile("Kontakt", " &nbsp;·&nbsp; ".join(kontakt)))

        orte = ", ".join(z["orte"])
        if z["gewinnorte"]:
            treffer = set(z["gewinnorte"])
            orte = ", ".join((f"<b>{_esc(o)}</b>" if o in treffer else _esc(o))
                             for o in z["orte"])
        else:
            orte = _esc(orte)
        rr.append(zeile(f"Orte ({len(z['orte'])})", orte))

        bez = _esc(d.get("beziehung_warum") or STUFENTEXT.get(stufe, ""))
        if d.get("crm_projekte"):
            k = d["crm_projekte"]
            bez += f" &nbsp;·&nbsp; {k} {'Objekt' if k == 1 else 'Objekte'} im CRM"
        if d.get("sap"):
            bez += " &nbsp;·&nbsp; SAP-Nummer vorhanden"
        rr.append(zeile("Beziehung", bez))

        if z.get("rolle"):
            r = _esc(z["rolle"])
            if d.get("rollenbeleg"):
                r += f' <font size="7.5" color="#647380">(erkannt an: ' \
                     f'{_esc(d["rollenbeleg"])})</font>'
            rr.append(zeile("Rolle", r))

        profil = []
        if d.get("buerotyp"):
            profil.append(_esc(d["buerotyp"]))
        if d.get("rechtsform"):
            profil.append(_esc(d["rechtsform"]))
        if d.get("gegruendet"):
            profil.append(f"gegr. {d['gegruendet']}")
        if d.get("mitarbeiter"):
            profil.append(_esc(str(d["mitarbeiter"])))
        if d.get("sprache"):
            profil.append(f"Website {_esc(str(d['sprache']).split('-')[0])}")
        if profil:
            rr.append(zeile("Profil", " &nbsp;·&nbsp; ".join(profil)))

        roh = d.get("schwerpunkte")
        if roh:
            try:
                liste = roh if isinstance(roh, list) else json.loads(roh)
            except (ValueError, TypeError):
                liste = [str(roh)]
            if liste:
                rr.append(zeile("Schwerpunkte", _esc(", ".join(str(x) for x in liste))))
        if d.get("taetigkeitsgebiet"):
            rr.append(zeile("Tätig in", _esc(d["taetigkeitsgebiet"])))
        if d.get("zertifikate"):
            rr.append(zeile("Zertifikate", _esc(d["zertifikate"])))
        if d.get("beschreibung"):
            rr.append(zeile("Von der Website", _esc(d["beschreibung"])))
        if d.get("referenzgroesse"):
            rr.append(zeile("Referenzen", _esc(d["referenzgroesse"])))
        if d.get("relevanz"):
            rr.append(zeile("Relevanz",
                            f'{_esc(d["relevanz"])} <font size="7.5" color="#647380">'
                            f'(Modellschätzung, nicht gegen Abschlüsse geprüft)</font>'))

        t = Table(rr, colWidths=[30 * mm, B - 30 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("LEFTPADDING", (1, 0), (1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ]))
        rahmen = Table([[[kopf, Spacer(1, 3), t]]], colWidths=[B])
        rahmen.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ACCENT_SOFT if stufe >= 3 else BG),
            ("LINEBEFORE", (0, 0), (0, -1), 2.5, ACCENT if stufe >= 3 else LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        return KeepTogether([rahmen, Spacer(1, 6)])

    S = [
        Paragraph("Architekturbüros mit Projekten in Spanien", h1),
        Paragraph(f"Solarlux · {daten['bueros']} Büros, {daten['orte']} genannte Orte · "
                  f"Stand {_de_datetime(dt.datetime.now())}", sub),
        Spacer(1, 11),
    ]

    reihen = Table([[[
        Paragraph("<b>Wie diese Liste sortiert ist</b>", body),
        Paragraph("231 Einträge in alphabetischer Ordnung wären ein Telefonbuch. Sortiert "
                  "wird deshalb in vier Stufen — oben steht, wer uns am nächsten ist:", body),
        Paragraph(f"<b>A · Schon zusammengearbeitet</b> ({zaehler['A']}) — es gab eine "
                  f"gemeinsame Verkaufschance oder Schriftverkehr.<br/>"
                  f"<b>B · Baut dort, wo wir gewinnen</b> ({zaehler['B']}) — Projekte auf den "
                  f"Balearen oder an der Zweitwohnsitzküste, wo 90 % unseres gewonnenen "
                  f"spanischen Auftragswerts liegt.<br/>"
                  f"<b>C · Schon einmal berührt</b> ({zaehler['C']}) — im CRM angelegt, aber "
                  f"ohne Schriftverkehr.<br/>"
                  f"<b>D · Die übrigen</b> ({zaehler['D']}) — nach Zahl der spanischen Orte.", body),
        Paragraph("Innerhalb jeder Stufe zuerst die engste Beziehung, dann die meisten Orte "
                  "in der Gewinnregion, dann die meisten Orte überhaupt. Orte in der "
                  "Gewinnregion sind <b>fett</b> gesetzt.", body),
        Paragraph("<b>Bewusst nicht im Rang:</b> die Spalte „Rolle“. Ob ein Büro die "
                  "Ausführung steuert oder nur entwirft, wurde gegen echte Ausgänge geprüft "
                  "und trennt nicht — die Angabe steht im Eintrag, weil sie sagt, worüber man "
                  "reden kann, aber sie schiebt niemanden nach oben. Dasselbe gilt für die "
                  "Relevanz-Schätzung: sie kommt aus einem Sprachmodell und ist nie gegen "
                  "Abschlüsse gemessen worden.", body),
    ]]], colWidths=[B])
    reihen.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_SOFT),
        ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    leiter = [[Paragraph("<b>Stufe</b>", kzeile), Paragraph("<b>heißt</b>", kzeile),
               Paragraph("<b>in dieser Liste</b>", kzeile)]]
    verteilung = Counter((z["stufe"] or 0) for z in daten["zeilen"])
    for stufe, was in (
            (5, "gemeinsames Objekt gewonnen"),
            (4, "auf einer Verkaufschance benannt"),
            (3, "Schriftverkehr vorhanden"),
            (2, "als Debitor angelegt"),
            (1, "als Lead erfasst"),
            (0, "nur Stammdaten, keine Berührung")):
        anzahl = verteilung.get(stufe, 0)
        stil = kzeile if anzahl else klabel
        leiter.append([
            Paragraph(f"<b>{stufe}</b>" if anzahl else str(stufe), stil),
            Paragraph(_esc(was), stil),
            Paragraph(f"{anzahl} Büros" if anzahl else "keines", stil),
        ])
    lt = Table(leiter, colWidths=[14 * mm, B - 52 * mm, 38 * mm])
    lt.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    beziehung = Table([[[
        Paragraph("<b>Was „Beziehung 4“ bedeutet</b>", body),
        Paragraph("Die Zahl auf jedem Eintrag ist die im CRM <b>belegte</b> Nähe zu "
                  "Solarlux — keine Einschätzung, sondern das, was an Spuren da ist. "
                  "Jede Stufe schließt die darunter ein.", body),
        lt,
        Paragraph("Die Leiter endet in dieser Liste bei 4: kein einziges der 231 Büros "
                  "hat mit uns bisher ein Objekt gewonnen. Das ist der offene Punkt und "
                  "zugleich der Grund, warum Stufe A oben steht.", body),
    ]]], colWidths=[B])
    beziehung.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG),
        ("LINEBEFORE", (0, 0), (0, -1), 3, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    S += [reihen, Spacer(1, 8), beziehung, Spacer(1, 6)]
    S.append(Paragraph(
        "Die Orte stammen von den Websites der Büros und tragen kein Datum; ein Projekt von "
        "2011 sieht aus wie eines von 2025. Adresse, Beziehung und CRM-Objekte kommen aus dem "
        "CRM. Persönliche E-Mail-Adressen sind ausgelassen — sie stehen in AdWatch.", note))

    nr = 0
    for kuerzel, titel, gruppe in stufen:
        if not gruppe:
            continue
        S.append(PageBreak())
        S.append(Paragraph(f"{kuerzel} · {titel} — {len(gruppe)} Büros", h2))
        if kuerzel == "A":
            S.append(Paragraph(
                "Diese Büros kennen uns bereits. Beziehung 4 heißt: sie wurden auf einer "
                "Verkaufschance benannt; Beziehung 3: es gab Schriftverkehr. Ein gemeinsam "
                "gewonnenes Objekt gibt es bei keinem — das ist der offene Punkt.", body))
        elif kuerzel == "B":
            S.append(Paragraph(
                "Kein bestehender Kontakt, aber Projekte genau in der Region, in der wir "
                "heute verkaufen. Die fett gesetzten Orte sind es, die sie hierher bringen.", body))
        elif kuerzel == "C":
            S.append(Paragraph(
                "Im CRM vorhanden, aber ohne belegten Schriftverkehr — meist als Lead "
                "erfasst oder als Debitor angelegt und nie weiterverfolgt.", body))
        else:
            S.append(Paragraph(
                "Ohne CRM-Berührung und ohne Projekt in der Gewinnregion, sortiert nach der "
                "Zahl der spanischen Orte. Wer hier oben steht, ist in Spanien sehr aktiv — "
                "nur eben dort, wo wir bisher nichts gewonnen haben.", body))
        for z in gruppe:
            nr += 1
            S.append(karte(z, nr))

    def fuss(canvas, dokument):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 16 * mm, 11 * mm, f"Seite {dokument.page}")
        canvas.drawString(16 * mm, 11 * mm,
                          f"Solarlux · Architekturbüros Spanien · {heute:%d.%m.%Y}")
        canvas.restoreState()

    doc.build(S, onFirstPage=fuss, onLaterPages=fuss)
    return pfad


if __name__ == "__main__":
    d = erheben()
    p = bauen(d)
    print("geschrieben:", p, Path(p).stat().st_size, "Bytes")
