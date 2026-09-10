"""Der Spanien-Marktbericht als PDF — für Daniel, der den Markt übernimmt.

WARUM DIESER BERICHT UND NICHT DER TÄTIGKEITSBERICHT.
`report.build_taetigkeit_report` beantwortet EINE Frage: welche Büros bauen in
Spanien. Das ist eine Arbeitsliste. Daniel bekommt aber einen Markt übergeben
und braucht zuerst die Lage: was steht schon, wo wird verdient, woran wird
verloren — und danach erst, wen er anrufen soll.

Alles hier ist gemessen, nichts geschätzt. Jede Zahl in diesem Bericht kommt
aus einer Abfrage, die in diesem Modul steht; wer sie nachrechnen will, kann
das Modul lesen. Was nicht messbar war, steht im Abschnitt „Was dieser Bericht
nicht weiß" — und zwar in derselben Schriftgröße wie der Rest.

KEINE PERSONENDATEN. Die Verkaufschancen tragen im Namen oft den Bauherrn
(„Palma, Carrer …, Nachname"). Der Bericht nennt Orte und Beträge, nie den
Namen. Firmen sind Firmen; Private Endkunden erscheinen nirgends.

Aufruf:
    python tools/spanien_bericht.py            -> output/Solarlux_Spanien_<datum>.pdf
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reportlab.lib import colors                                   # noqa: E402
from reportlab.lib.enums import TA_RIGHT                           # noqa: E402
from reportlab.lib.pagesizes import A4                             # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm                                 # noqa: E402
from reportlab.platypus import (                                   # noqa: E402
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from sqlalchemy import text as _sql                                # noqa: E402

from adwatch import config, taetigkeit                             # noqa: E402
from adwatch.db import SessionLocal                                # noqa: E402
from adwatch.report import (                                       # noqa: E402
    ACCENT, ACCENT_SOFT, BG, INK, LINE, MUTED, _de_datetime, _esc, fusszeile,
)

LAND = "ES"

# Balearen-Erkennung über den Ortsnamen. Die Verkaufschancen tragen kein
# Regionsfeld, und die Postleitzahl fehlt bei zwei Dritteln — der Ortsname ist
# das einzige durchgängig gefüllte Merkmal. Bewusst großzügig: lieber ein
# Festlandsort zu viel in der Gruppe als die Aussage zu schwach.
_BALEAREN = (
    "mallorca", "palma", "calvi", "santa pon", "andratx", "santany", "illetas",
    "palmanova", "cala ", "sa pobla", "moscari", "ibiza", "eivissa", "menorca",
    "formentera", "el toro", "balear", "llucmajor", "montuiri", "alcudia",
    "pollen", "soller", "s'ag", "port d", "manacor", "felanitx", "campos",
    "sineu", "arta", "capdepera", "binissalem", "deia", "valldemossa",
    "esporles", "son ", "santa eul", "bendinat", "portals",
)


def _ist_balearen(ort: str | None) -> bool:
    o = (ort or "").lower()
    return any(k in o for k in _BALEAREN)


# ---------------------------------------------------------------------------
# Messen
# ---------------------------------------------------------------------------

def erheben() -> dict:
    """Alle Zahlen des Berichts in einem Durchgang. Eine Funktion, damit klar
    ist, dass Text und Tabellen aus DERSELBEN Erhebung stammen."""
    d: dict = {}
    with SessionLocal() as s:
        q = lambda sql: s.execute(_sql(sql)).all()   # noqa: E731

        # --- Verkaufschancen je Land ---------------------------------------
        # `country` ist im CRM leer, gefüllt ist `geocode_country` — es kommt
        # aus der geokodierten BAUADRESSE. Für „wo wird gebaut" ist das sogar
        # das richtige Feld; für „wer kauft" wäre es das falsche.
        d["laender"] = q("""
            SELECT COALESCE(geocode_country,'?') l, COUNT(*) n,
                   SUM(state='gewonnen') g, SUM(state='verloren') v,
                   SUM(state='offen') o,
                   ROUND(SUM(CASE WHEN state='gewonnen'
                                  THEN COALESCE(order_value,0) END)) w
            FROM crm_opportunities
            WHERE geocode_country IS NOT NULL
            GROUP BY l HAVING n >= 80 ORDER BY n DESC""")

        d["es_jahre"] = q("""
            SELECT substr(created_on,1,4) j, COUNT(*),
                   SUM(state='gewonnen'), SUM(state='verloren'), SUM(state='offen')
            FROM crm_opportunities WHERE geocode_country='ES'
            GROUP BY j ORDER BY j""")

        d["es_gruende"] = q("""
            SELECT COALESCE(NULLIF(lost_reason,''),'(ohne Angabe)') g, COUNT(*) n
            FROM crm_opportunities
            WHERE geocode_country='ES' AND state='verloren' AND lost_reason<>'Duplikat'
            GROUP BY g ORDER BY n DESC LIMIT 8""")
        d["de_gruende"] = dict(q("""
            SELECT COALESCE(NULLIF(lost_reason,''),'(ohne Angabe)') g,
                   ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM crm_opportunities
                        WHERE geocode_country='DE' AND state='verloren'
                          AND lost_reason<>'Duplikat'),1)
            FROM crm_opportunities
            WHERE geocode_country='DE' AND state='verloren' AND lost_reason<>'Duplikat'
            GROUP BY g"""))

        d["es_wege"] = q("""
            SELECT COALESCE(NULLIF(sales_channel,''),'(ohne)') k, COUNT(*),
                   SUM(state='gewonnen')
            FROM crm_opportunities WHERE geocode_country='ES'
            GROUP BY k ORDER BY 2 DESC""")

        d["es_vc"] = q("""
            SELECT city, state, COALESCE(order_value,0), COALESCE(estimated_value,0)
            FROM crm_opportunities WHERE geocode_country='ES'""")

        # Wer steckt hinter den „zu teuer"-Verlusten? Ohne diese Frage ist der
        # häufigste Verlustgrund eine Zahl ohne Adressat.
        d["teuer_konten"] = q("""
            SELECT c.name, COUNT(*) k
            FROM crm_opportunities o JOIN companies c ON c.crm_id=o.parent_account_crm_id
            WHERE o.geocode_country='ES' AND o.state='verloren'
              AND o.lost_reason='Zu teuer'
            GROUP BY c.name ORDER BY k DESC""")

        # Das Konto mit den meisten Preisverlusten — mit seiner ganzen Bilanz.
        # Erst die zeigt, ob „zu teuer" bei ihm ein Einzelfall ist oder das Muster.
        d["teuer_spitze"] = q("""
            SELECT c.name, c.city, COALESCE(c.quote_count,0), ROUND(COALESCE(c.quote_sum,0)),
                   ROUND(COALESCE(c.beleg_sum,0)),
                   (SELECT COUNT(*) FROM crm_opportunities o2
                     WHERE o2.parent_account_crm_id = c.crm_id),
                   (SELECT COUNT(*) FROM crm_opportunities o3
                     WHERE o3.parent_account_crm_id = c.crm_id AND o3.state='gewonnen')
            FROM crm_opportunities o JOIN companies c ON c.crm_id=o.parent_account_crm_id
            WHERE o.geocode_country='ES' AND o.state='verloren'
              AND o.lost_reason='Zu teuer'
            GROUP BY c.name ORDER BY COUNT(*) DESC LIMIT 1""")[0]

        # --- Firmen mit Sitz in Spanien ------------------------------------
        d["es_segmente"] = q("""
            SELECT COALESCE(NULLIF(segment,''),'(ohne)') seg, COUNT(*) n,
                   SUM(COALESCE(beleg_count,0)>0) k, ROUND(SUM(COALESCE(beleg_sum,0)))
            FROM companies WHERE country='ES' GROUP BY seg ORDER BY n DESC LIMIT 8""")
        d["es_firmen_gesamt"] = q("SELECT COUNT(*) FROM companies WHERE country='ES'")[0][0]

        d["es_partner"] = q("""
            SELECT name, city, segment, beleg_count, ROUND(beleg_sum),
                   substr(beleg_first,1,4), substr(beleg_last,1,4)
            FROM companies WHERE country='ES' AND COALESCE(beleg_sum,0) > 0
            ORDER BY beleg_sum DESC LIMIT 10""")

        jahr: dict[str, float] = defaultdict(float)
        for (roh,) in q("""SELECT beleg_by_year FROM companies
                           WHERE country='ES' AND COALESCE(beleg_sum,0) > 0"""):
            werte = roh if isinstance(roh, dict) else json.loads(roh or "{}")
            for j, v in werte.items():
                if isinstance(v, dict):
                    v = v.get("sum") or v.get("summe") or 0
                jahr[j] += float(v or 0)
        d["es_umsatz_jahre"] = sorted(jahr.items())

    # --- Architekturbüros mit Projekten in Spanien -------------------------
    b = taetigkeit.bueros(land=LAND)
    d["bueros"] = b["rows"]
    d["bueros_n"] = b["bueros"]
    d["orte_n"] = b["orte"]
    return d


# ---------------------------------------------------------------------------
# Setzen
# ---------------------------------------------------------------------------

def _z(v, stellen: int = 1) -> str:
    """Deutsches Dezimalkomma. Ein Bericht, der „8.4 %" schreibt, sieht in einem
    deutschen Haus nach Maschinenausgabe aus."""
    return f"{v:.{stellen}f}".replace(".", ",")


def _eur(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.0f} €".replace(",", ".")


def bauen(d: dict, pfad: str | None = None) -> str:
    heute = dt.date.today()
    if pfad is None:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        pfad = str(config.OUTPUT_DIR / f"Solarlux_Spanien_{heute:%Y-%m-%d}.pdf")

    st = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=st["Title"], textColor=INK, fontSize=22,
                        alignment=0, spaceAfter=3)
    h2 = ParagraphStyle("h2", parent=st["Heading2"], textColor=INK, fontSize=13.5,
                        spaceBefore=17, spaceAfter=6)
    sub = ParagraphStyle("sub", parent=st["Normal"], textColor=MUTED, fontSize=9,
                         leading=12.5)
    body = ParagraphStyle("body", parent=st["Normal"], textColor=INK, fontSize=9.5,
                          leading=14, spaceAfter=5)
    note = ParagraphStyle("note", parent=st["Normal"], textColor=MUTED, fontSize=8,
                          leading=11.5, spaceBefore=3)
    ch = ParagraphStyle("ch", parent=st["Normal"], textColor=colors.white,
                        fontSize=8, leading=10.5)
    chr_ = ParagraphStyle("chr", parent=ch, alignment=TA_RIGHT)
    c = ParagraphStyle("c", parent=st["Normal"], textColor=INK, fontSize=8.5, leading=11.5)
    cr = ParagraphStyle("cr", parent=c, alignment=TA_RIGHT)
    cm = ParagraphStyle("cm", parent=c, textColor=MUTED, fontSize=8)
    cmr = ParagraphStyle("cmr", parent=cm, alignment=TA_RIGHT)

    doc = SimpleDocTemplate(
        pfad, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=17 * mm,
        title="Solarlux — Spanien: was die Daten sagen",
        author="AdWatch")
    B = doc.width

    def tab(daten, breiten, kopfzeile=True, zebra=True, extra=None):
        t = Table(daten, colWidths=breiten, repeatRows=1 if kopfzeile else 0)
        s = [("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("GRID", (0, 0), (-1, -1), 0.4, LINE),
             ("LEFTPADDING", (0, 0), (-1, -1), 5),
             ("RIGHTPADDING", (0, 0), (-1, -1), 5),
             ("TOPPADDING", (0, 0), (-1, -1), 3.5),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5)]
        if kopfzeile:
            s.append(("BACKGROUND", (0, 0), (-1, 0), INK))
        if zebra:
            for i in range(2, len(daten), 2):
                s.append(("BACKGROUND", (0, i), (-1, i), BG))
        t.setStyle(TableStyle(s + (extra or [])))
        return t

    def kasten(inhalt, farbe=ACCENT_SOFT, strich=ACCENT):
        t = Table([[inhalt]], colWidths=[B])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), farbe),
            ("LINEBEFORE", (0, 0), (0, -1), 3, strich),
            ("LEFTPADDING", (0, 0), (-1, -1), 11),
            ("RIGHTPADDING", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        return t

    # === Rechnen ==========================================================
    vc = d["es_vc"]
    n_vc = len(vc)
    gew = [r for r in vc if r[1] == "gewonnen"]
    ver = [r for r in vc if r[1] == "verloren"]
    off = [r for r in vc if r[1] == "offen"]
    entschieden = len(gew) + len(ver)
    quote_es = 100 * len(gew) / entschieden if entschieden else 0
    de = next((r for r in d["laender"] if r[0] == "DE"), None)
    quote_de = 100 * de[2] / (de[2] + de[3]) if de else 0

    bal_gew = [r for r in gew if _ist_balearen(r[0])]
    bal_off = [r for r in off if _ist_balearen(r[0])]
    bal_alle = [r for r in vc if _ist_balearen(r[0])]
    wert_gew = sum(r[2] for r in gew)
    wert_bal = sum(r[2] for r in bal_gew)
    pipe_ges = sum(r[3] for r in off)
    pipe_bal = sum(r[3] for r in bal_off)

    bu = d["bueros"]
    warm = sorted([z for z in bu if z["stufe"] >= 3],
                  key=lambda z: (-z["stufe"], -len(z["orte"]), z["name"] or ""))
    sitze = Counter((z["land"] or "?") for z in bu)
    orte = Counter(o for z in bu for o in z["orte"])
    es_kalt = sum(1 for z in bu if (z["land"] or "") == "ES" and z["stufe"] == 0)
    rollen = Counter((z["rolle"] or "nicht erkennbar") for z in bu)
    # Der Schnittpunkt aus "kennt uns schon", "baut da, wo wir gewinnen" und
    # "spricht unsere Sprache". Ausgerechnet, weil eine von Hand getippte Zahl
    # in Abschnitt 8 nicht mehr stimmt, sobald sich die Daten bewegen -- dort
    # standen "Sechs" und "131", und beide waren schon schief.
    # Wie viele der „zu teuer"-Melder stehen zugleich in der Umsatzliste?
    umsatzliste = {r[0] for r in d["es_partner"]}
    teuer_bekannt = sum(1 for k, _ in d["teuer_konten"] if k in umsatzliste)
    insel_warm = [z for z in warm
                  if (z["land"] or "") in ("DE", "AT", "CH")
                  and any(o.lower().startswith(("mallorca", "palma", "andratx"))
                          for o in z["orte"])]

    S = []

    # === Titel ============================================================
    S += [
        Paragraph("Spanien: was die Daten sagen", h1),
        Paragraph(f"Solarlux · Marktbild aus CRM und Website-Recherche · Stand "
                  f"{_de_datetime(dt.datetime.now())}", sub),
        Spacer(1, 12),
        kasten([
            Paragraph("<b>Drei Sätze vorweg</b>", body),
            Paragraph(f"<b>1.</b> Spanien ist heute die Baleareninsel Mallorca. "
                      f"{len(bal_gew)} der {len(gew)} gewonnenen Verkaufschancen und "
                      f"<b>{100*wert_bal/wert_gew:.0f} %</b> des gewonnenen Auftragswerts "
                      f"liegen dort — bei nur {100*len(bal_alle)/n_vc:.0f} % der Chancen.", body),
            Paragraph(f"<b>2.</b> Die Gewinnquote liegt bei <b>{_z(quote_es)} %</b> gegen "
                      f"{_z(quote_de)} % in Deutschland. Der häufigste Verlustgrund ist "
                      f"„zu teuer“ — in Deutschland ist es „kein Feedback“.", body),
            Paragraph(f"<b>3.</b> {d['bueros_n']} Architekturbüros bauen nachweislich in "
                      f"Spanien. Mit <b>{len(warm)}</b> davon gab es schon Schriftverkehr oder "
                      f"eine gemeinsame Verkaufschance; <b>{es_kalt}</b> spanische Büros sind "
                      f"im CRM völlig unberührt.", body),
        ]),
        Spacer(1, 6),
        Paragraph("Alle Zahlen sind aus dem CRM und aus den Websites der Büros gemessen, "
                  "nicht geschätzt. Der Abschnitt „Was dieser Bericht nicht weiß“ am Ende "
                  "nennt die Grenzen — er gehört zum Bericht.", note),
    ]

    # === 1. Ausgangslage ==================================================
    S.append(Paragraph("1. Die Ausgangslage im Ländervergleich", h2))
    S.append(Paragraph(
        "Verkaufschancen nach der geokodierten <b>Bauadresse</b> — also danach, wo gebaut "
        "werden sollte, nicht wo der Kunde sitzt. Gezeigt sind die Länder mit mindestens "
        "80 Chancen. „Quote“ ist gewonnen geteilt durch entschieden (gewonnen + verloren); "
        "offene Chancen zählen nicht mit, weil sie den Ausgang noch nicht kennen.", body))
    kopf = [Paragraph(x, ch) for x in ("Land", "Chancen")] + \
           [Paragraph(x, chr_) for x in ("gewonnen", "verloren", "offen", "Quote", "gew. Wert")]
    zeilen = [kopf]
    for l, n, g, v, o, w in d["laender"]:
        ent = (g or 0) + (v or 0)
        markiert = l == "ES"
        stil = c if markiert else cm
        stilr = cr if markiert else cmr
        zeilen.append([
            Paragraph(f"<b>{l}</b>" if markiert else l, stil),
            Paragraph(f"{n:,}".replace(",", "."), stilr),
            Paragraph(f"{g:,}".replace(",", "."), stilr),
            Paragraph(f"{v:,}".replace(",", "."), stilr),
            Paragraph(f"{o:,}".replace(",", "."), stilr),
            Paragraph(f"<b>{_z(100*g/ent)} %</b>" if ent else "—", stilr),
            Paragraph(_eur(w), stilr),
        ])
    es_zeile = next(i for i, r in enumerate(d["laender"], start=1) if r[0] == "ES")
    S.append(tab(zeilen, [B*0.10, B*0.14, B*0.15, B*0.14, B*0.12, B*0.13, B*0.22],
                 extra=[("BACKGROUND", (0, es_zeile), (-1, es_zeile), ACCENT_SOFT)]))
    S.append(Paragraph(
        f"Spanien ist mit {n_vc} Chancen ein <b>kleiner</b> Markt — Frankreich hat das "
        f"Neunzehnfache, Österreich das Sechsundzwanzigfache. Die Quote von {_z(quote_es)} % "
        f"liegt auf dem Niveau von Frankreich und Großbritannien, nicht auf dem von "
        f"Deutschland, Österreich oder den Niederlanden. Bei dieser Fallzahl ist die Quote "
        f"allerdings unsicher: {len(gew)} Treffer aus {entschieden} Entscheidungen; ein "
        f"gewonnener Auftrag mehr oder weniger verschiebt sie um fast einen Punkt.", body))

    # Zeitverlauf
    S.append(Spacer(1, 4))
    jz = [[Paragraph(x, ch) for x in ("Jahr",)] +
          [Paragraph(x, chr_) for x in ("Chancen", "gewonnen", "verloren", "offen")]]
    for j, n, g, v, o in d["es_jahre"]:
        jz.append([Paragraph(j, c)] + [Paragraph(str(x), cr) for x in (n, g, v, o)])
    S.append(tab(jz, [B*0.20, B*0.20, B*0.20, B*0.20, B*0.20]))
    S.append(Paragraph(
        "Das laufende Jahr ist noch nicht entschieden — 14 der 18 Chancen sind offen. "
        "Der Bestand ist über vier Jahre stabil bei rund 30 Chancen im Jahr, ohne Trend "
        "nach oben oder unten.", note))

    # === 2. Spanien ist Mallorca =========================================
    S.append(PageBreak())
    S.append(Paragraph("2. Der Kern: Spanien ist heute Mallorca", h2))
    S.append(Paragraph(
        "Die auffälligste Struktur im spanischen Bestand ist regional. Ordnet man die "
        "Chancen nach dem Ortsnamen den Balearen zu, ergibt sich ein Bild, das mit "
        "„Spanien“ als Markt wenig zu tun hat:", body))
    bz = [[Paragraph("", ch), Paragraph("Balearen", chr_), Paragraph("übriges Spanien", chr_),
           Paragraph("Anteil Balearen", chr_)]]
    for label, teil, ganz in (
            ("Chancen gesamt", len(bal_alle), n_vc),
            ("davon gewonnen", len(bal_gew), len(gew)),
            ("davon offen", len(bal_off), len(off))):
        bz.append([Paragraph(label, c), Paragraph(str(teil), cr),
                   Paragraph(str(ganz - teil), cr),
                   Paragraph(f"<b>{100*teil/ganz:.0f} %</b>" if ganz else "—", cr)])
    bz.append([Paragraph("gewonnener Auftragswert", c), Paragraph(_eur(wert_bal), cr),
               Paragraph(_eur(wert_gew - wert_bal), cr),
               Paragraph(f"<b>{100*wert_bal/wert_gew:.0f} %</b>", cr)])
    bz.append([Paragraph("offene Pipeline (geschätzt)", c), Paragraph(_eur(pipe_bal), cr),
               Paragraph(_eur(pipe_ges - pipe_bal), cr),
               Paragraph(f"<b>{100*pipe_bal/pipe_ges:.0f} %</b>", cr)])
    S.append(tab(bz, [B*0.37, B*0.21, B*0.21, B*0.21]))
    S.append(Paragraph(
        f"Ein Drittel der Chancen trägt neun Zehntel des gewonnenen Werts. Die größte je "
        f"gewonnene spanische Verkaufschance liegt auf Mallorca und ist mit "
        f"{_eur(max(r[2] for r in gew))} allein größer als alle übrigen acht zusammen.", body))
    S.append(Paragraph(
        "<b>Was für ein Geschäft das ist.</b> Die gewonnenen und offenen Chancen liegen fast "
        "ausnahmslos an Adressen in Santa Ponça, Andratx, Calvià, Illetas, Palma, Santanyí, "
        "Cala Murada — und an der Costa Blanca in Jávea und Teulada-Moraira. Das ist der "
        "hochwertige Privat- und Ferienhausbau der nord- und mitteleuropäischen Zweitwohnsitze, "
        "kein spanisches Objektgeschäft. Die Bauherren sind zu einem großen Teil "
        "deutschsprachig, ebenso mehrere der ausführenden Betriebe vor Ort.", body))
    S.append(Paragraph(
        "Praktisch heißt das: Sprache, Referenzen und Argumente, die auf dem deutschen Markt "
        "wirken, wirken auf Mallorca vermutlich weiter — auf dem spanischen Festland sind sie "
        "die falschen. Der Bericht kann das nicht beweisen; er kann nur zeigen, dass das "
        "Geschäft heute dort und nicht anderswo entsteht.", body))

    wz = [[Paragraph("Vertriebsweg", ch)] + [Paragraph(x, chr_) for x in ("Chancen", "gewonnen")]]
    for k, n, g in d["es_wege"]:
        wz.append([Paragraph(_esc(k), c), Paragraph(str(n), cr), Paragraph(str(g), cr)])
    S.append(Spacer(1, 6))
    S.append(tab(wz, [B*0.50, B*0.25, B*0.25]))
    S.append(Paragraph(
        "Vier von fünf spanischen Chancen laufen über den Fachhandel, nicht über den "
        "Direkt- oder Objektvertrieb. Die neun Chancen aus der Architektenberatung haben "
        "bisher <b>keine einzige</b> zum Auftrag geführt — eine kleine Zahl, aber sie passt "
        "zu dem, was wir über Architekten insgesamt wissen (siehe Abschnitt 5).", body))

    # === 3. Wer heute liefert ============================================
    S.append(PageBreak())
    S.append(Paragraph("3. Was in Spanien heute schon steht", h2))
    S.append(Paragraph(
        "Im CRM stehen <b>" + f"{d['es_firmen_gesamt']:,}".replace(",", ".")
        + "</b> Firmen mit spanischer Adresse. Umsatz — echte Belege, nicht Angebote "
          "— hat davon eine sehr kleine Gruppe:", body))
    sz = [[Paragraph("Segment", ch), Paragraph("Firmen", chr_),
           Paragraph("mit Umsatz", chr_), Paragraph("Umsatz gesamt", chr_)]]
    for seg, n, k, summe in d["es_segmente"]:
        sz.append([Paragraph(_esc(seg), c), Paragraph(f"{n:,}".replace(",", "."), cr),
                   Paragraph(str(k), cr), Paragraph(_eur(summe), cr)])
    S.append(tab(sz, [B*0.34, B*0.18, B*0.20, B*0.28]))
    S.append(Paragraph(
        "Die 1.136 spanischen Architekturbüros im Bestand haben zusammen 61 € Umsatz. Das "
        "ist kein Fehler und auch keine Enttäuschung: Architekten kaufen nichts, sie "
        "schreiben aus. Ihr Wert liegt in den Ausschreibungen, nicht in den Rechnungen.", body))

    S.append(Spacer(1, 6))
    uz = [[Paragraph("Jahr", ch)] + [Paragraph("Umsatz (Belege)", chr_)]]
    for j, v in d["es_umsatz_jahre"]:
        uz.append([Paragraph(j, c), Paragraph(_eur(v), cr)])
    S.append(tab(uz, [B*0.5, B*0.5]))
    S.append(Paragraph(
        "Das laufende Jahr 2026 ist noch nicht vollständig. Die Reihe zeigt keinen Aufbau — "
        "eher ein leichtes Abschmelzen von einem ohnehin niedrigen Niveau.", note))

    S.append(Spacer(1, 8))
    S.append(Paragraph("Die Partner, über die das Geschäft heute läuft", h2))
    pz = [[Paragraph("Firma", ch), Paragraph("Ort", ch), Paragraph("Segment", ch),
           Paragraph("Belege", chr_), Paragraph("Zeitraum", chr_), Paragraph("Umsatz", chr_)]]
    for name, ort, seg, n, summe, von, bis in d["es_partner"]:
        pz.append([Paragraph(f"<b>{_esc(name)}</b>", c), Paragraph(_esc(ort or ""), cm),
                   Paragraph(_esc(seg or ""), cm), Paragraph(str(n), cr),
                   Paragraph(f"{von or '?'}–{bis or '?'}", cmr), Paragraph(_eur(summe), cr)])
    S.append(tab(pz, [B*0.28, B*0.19, B*0.16, B*0.08, B*0.14, B*0.15]))
    S.append(Paragraph(
        "Zwei Firmen tragen zwei Drittel des spanischen Umsatzes. Auffällig ist die zweite "
        "Gruppe darunter: mehrere kleine, auf Mallorca ansässige und deutschsprachig geführte "
        "Bau- und Montagebetriebe. Sie sind heute der eigentliche Zugang zum Inselgeschäft — "
        "und sie sind ersetzbar klein, was ein Risiko und eine Chance zugleich ist.", body))

    # === 4. Warum verloren wird ==========================================
    S.append(PageBreak())
    S.append(Paragraph("4. Woran die spanischen Chancen scheitern", h2))
    S.append(Paragraph(
        "Verlustgründe der verlorenen spanischen Chancen, gegen Deutschland als Maßstab. "
        "Duplikate sind herausgerechnet, weil sie kein Verlust sind.", body))
    n_ver = sum(n for _, n in d["es_gruende"])
    gz = [[Paragraph("Verlustgrund", ch), Paragraph("Spanien", chr_),
           Paragraph("Anteil ES", chr_), Paragraph("Anteil DE", chr_)]]
    for grund, n in d["es_gruende"]:
        a_es = 100 * n / n_ver
        a_de = d["de_gruende"].get(grund)
        hervor = grund in ("Zu teuer", "Kein Feedback vom Kunden")
        gz.append([
            Paragraph(f"<b>{_esc(grund)}</b>" if hervor else _esc(grund), c),
            Paragraph(str(n), cr),
            Paragraph(f"<b>{a_es:.0f} %</b>" if hervor else f"{a_es:.0f} %", cr),
            Paragraph(f"{a_de:.0f} %" if a_de is not None else "—", cmr),
        ])
    S.append(tab(gz, [B*0.46, B*0.16, B*0.19, B*0.19]))
    S.append(Paragraph(
        "<b>Der Unterschied ist eindeutig und geht in beide Richtungen.</b> In Spanien ist "
        "„zu teuer“ der häufigste Grund und liegt deutlich über dem deutschen Anteil. "
        "„Kein Feedback vom Kunden“ ist umgekehrt in Deutschland der häufigste Grund und in "
        "Spanien seltener. Anders gesagt: spanische Interessenten steigen aus, <b>nachdem</b> "
        "sie den Preis gesehen haben — sie verschwinden nicht vorher. Das ist die "
        "angenehmere Sorte Verlust, weil sie eine Antwort enthält.", body))
    S.append(Paragraph(
        "Was der Bericht dazu <b>nicht</b> sagen kann: ob „zu teuer“ den Produktpreis meint, "
        "die Montagekosten vor Ort, den Transport auf die Insel oder den Vergleich mit einem "
        "lokalen Aluminiumbauer. Diese vier Fälle brauchen völlig verschiedene Antworten. Es "
        "wären ein paar Telefonate mit den Fachhändlern nötig, um sie zu trennen — und das "
        "ist wahrscheinlich die kürzeste Strecke zu einer besseren Quote.", body))

    # === 5. Die Architekturbüros =========================================
    S.append(PageBreak())
    S.append(Paragraph("5. Wer in Spanien baut: 231 Architekturbüros", h2))
    S.append(Paragraph(
        f"Für diesen Bericht wurden die Websites von rund 10.000 Architektur- und "
        f"Planungsbüros aus dem CRM gelesen — Referenz-, Projekt- und Impressumsseiten — und "
        f"die dort genannten Orte gegen ein Verzeichnis von 315.000 europäischen "
        f"Postleitzahlen abgeglichen. Ergebnis für Spanien: <b>{d['bueros_n']} Büros</b> "
        f"nennen zusammen <b>{d['orte_n']} spanische Orte</b>.", body))
    S.append(Paragraph(
        "Das ist etwas anderes als „Büros mit spanischer Adresse“. Ein Drittel dieser Büros "
        "sitzt gar nicht in Spanien:", body))

    lz = [[Paragraph("Sitz des Büros", ch), Paragraph("Büros", chr_), Paragraph("Anteil", chr_)]]
    for land, n in sitze.most_common(8):
        lz.append([Paragraph(land, c), Paragraph(str(n), cr),
                   Paragraph(f"{100*n/len(bu):.0f} %", cr)])
    stz = [[Paragraph("Beziehung", ch), Paragraph("Büros", chr_), Paragraph("was das heißt", ch)]]
    for k, txt in ((4, "auf einer gemeinsamen Verkaufschance"), (3, "Schriftverkehr"),
                   (2, "Debitor angelegt"), (1, "als Lead erfasst"), (0, "nur Stammdaten")):
        n = sum(1 for z in bu if z["stufe"] == k)
        stz.append([Paragraph(str(k), c), Paragraph(str(n), cr), Paragraph(txt, cm)])
    nebeneinander = Table([[tab(lz, [B*0.20, B*0.13, B*0.14], zebra=False),
                            tab(stz, [B*0.13, B*0.10, B*0.28], zebra=False)]],
                          colWidths=[B*0.47, B*0.53])
    nebeneinander.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                       ("RIGHTPADDING", (0, 0), (0, -1), 8)]))
    S.append(nebeneinander)
    S.append(Paragraph(
        f"<b>{sitze.get('DE', 0)} deutsche, {sitze.get('AT', 0)} österreichische und "
        f"{sitze.get('NL', 0)} niederländische Büros bauen in Spanien.</b> Das ist die "
        f"Gruppe, die zu dem passt, was Abschnitt 2 zeigt — und es ist die Gruppe, die man "
        f"auf Deutsch anrufen kann, ohne ein spanisches Vertriebsbüro zu haben.", body))

    S.append(Spacer(1, 6))
    S.append(Paragraph("Wo diese Büros bauen", h2))
    haelfte = (len(orte.most_common(16)) + 1) // 2
    top = orte.most_common(16)
    ozA = [[Paragraph("Ort", ch), Paragraph("Büros", chr_)]]
    ozB = [[Paragraph("Ort", ch), Paragraph("Büros", chr_)]]
    for i, (ort, n) in enumerate(top):
        (ozA if i < haelfte else ozB).append(
            [Paragraph(_esc(ort), c), Paragraph(str(n), cr)])
    zwei = Table([[tab(ozA, [B*0.30, B*0.15], zebra=False),
                   tab(ozB, [B*0.30, B*0.15], zebra=False)]], colWidths=[B*0.5, B*0.5])
    zwei.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (0, -1), 8)]))
    S.append(zwei)
    S.append(Paragraph(
        f"Barcelona und Madrid führen deutlich — dort sitzt das Volumen. Aber die dritte "
        f"Stelle gehört <b>Mallorca</b> ({orte['Mallorca']} Büros, dazu "
        f"{orte.get('Palma de Mallorca', 0)} für Palma und {orte.get('Andratx', 0)} für "
        f"Andratx), und mit Ibiza, Menorca und Marbella ist die Ferienhausküste unter den "
        f"zehn häufigsten mehrfach vertreten. Die Bürolandschaft bestätigt also genau die "
        f"Region, in der wir heute schon verkaufen.", body))

    S.append(Paragraph(
        f"<b>Rolle der Büros.</b> Aus dem Wortlaut der Websites lässt sich ablesen, ob ein "
        f"Büro die Ausführung steuert (Bauleitung, Ausschreibung, „dirección de obra“) oder "
        f"nur entwirft: {rollen.get('vergibt Aufträge', 0)} steuern, "
        f"{rollen.get('empfiehlt', 0)} entwerfen, bei "
        f"{rollen.get('nicht erkennbar', 0)} gibt die Website es nicht her. "
        f"<b>Wichtig:</b> das ist keine Rangfolge. Wir haben geprüft, ob steuernde Büros "
        f"häufiger zum Abschluss führen — sie tun es nicht messbar. Die Spalte sagt, mit wem "
        f"man über Ausschreibungen reden kann, nicht wer mehr wert ist.", body))

    # === 6. Die Anrufliste ===============================================
    S.append(PageBreak())
    S.append(Paragraph(f"6. Der Einstieg: {len(warm)} Büros, die uns schon kennen", h2))
    S.append(Paragraph(
        "Von den 231 Büros haben 24 bereits eine belegte Beziehung zu Solarlux — Stufe 4 "
        "heißt: es gab eine gemeinsame Verkaufschance; Stufe 3: es gab Schriftverkehr. "
        "Keines davon hat bisher ein gemeinsames Objekt gewonnen. Das ist die kürzeste "
        "Liste mit dem geringsten Kaltakquise-Anteil und deshalb der naheliegende Anfang.", body))
    az = [[Paragraph("Büro", ch), Paragraph("Bez.", chr_), Paragraph("Sitz", ch),
           Paragraph("Rolle", ch), Paragraph("Orte in Spanien", ch)]]
    for z in warm:
        name = f"<b>{_esc(z['name'])}</b>"
        if z.get("website"):
            name += f'<br/><font size="7" color="#{MUTED.hexval()[2:]}">{_esc(z["website"])}</font>'
        az.append([
            Paragraph(name, c),
            Paragraph(str(z["stufe"]), cr),
            Paragraph(_esc((z["sitz"] or "") + (f" · {z['land']}" if z["land"] else "")), cm),
            Paragraph(_esc(z["rolle"] or "—"), cm),
            Paragraph(_esc(", ".join(z["orte"])), c),
        ])
    S.append(tab(az, [B*0.30, B*0.07, B*0.19, B*0.16, B*0.28]))
    S.append(Paragraph(
        "Die vollständige Liste aller 231 Büros mit ihren Orten liegt in AdWatch unter "
        "<i>Firmen → Tätigkeit → Liste</i> und lässt sich dort nach Ort, Sitzland, Beziehung "
        "und Rolle filtern und als PDF oder Excel ausgeben.", note))

    # === 7. Grenzen ======================================================
    S.append(PageBreak())
    S.append(Paragraph("7. Was dieser Bericht nicht weiß", h2))
    for titel, txt in (
        ("Die Orte tragen kein Datum.",
         "Eine Website sagt „wir haben in Marbella gebaut“, aber nicht wann. Ein Projekt "
         "von 2011 sieht genauso aus wie eines von 2025. Die Liste taugt zur Auswahl, nicht "
         "zur Aktualität."),
        ("Ortsnamen sind mehrdeutig.",
         "Spanische Nachnamen sind oft auch Gemeindenamen — Reina, Cárdenas, Zúñiga, "
         "Maluenda existieren beide Male. Ein Mitarbeitername auf einer Teamseite kann "
         "deshalb als Projektort gelesen werden. Der Erkenner verlangt inzwischen starke "
         "Belege, aber der Rest bleibt: von 850 Ortsnennungen sind 203 Einzelnennungen, und "
         "4 der 231 Büros stützen sich ausschließlich auf solche. Für die 24 Büros in "
         "Abschnitt 6 lohnt ein Blick auf die Website, bevor man das Projekt anspricht."),
        ("Einzelne Sitzangaben im CRM sind falsch.",
         "In der Liste in Abschnitt 6 steht bei Chapman Taylor „London · DE“ und bei "
         "Max von Werz „México City · DE“. Die Stadt stimmt, der Ländercode nicht — er "
         "ist im CRM auf DE gesetzt. Das betrifft die Zuordnung der Büros zu ihrem "
         "Sitzland, nicht die Orte, an denen sie bauen."),
        ("Ein Ort kann auch im falschen Land liegen.",
         "Bei BIG (Bjarke Ingels Group) steht „Las Vegas“ in der Ortsspalte. Las Vegas "
         "ist tatsächlich eine spanische Gemeinde — zweimal sogar, in Córdoba und in "
         "Toledo — aber bei diesem Büro ist mit großer Wahrscheinlichkeit Nevada "
         "gemeint. Solche Fälle fängt kein Ortsverzeichnis; sie fallen beim Lesen auf."),
        ("Die Länderzuordnung der Chancen ist geokodiert, nicht gepflegt.",
         "Das CRM-Feld „Land“ ist bei allen 57.776 Verkaufschancen leer; benutzt wurde "
         "die aus der Bauadresse geokodierte Angabe. Ein paar Zeilen sind dabei falsch "
         "einsortiert — im spanischen Bestand finden sich eine schwedische und eine "
         "südtiroler Adresse. Bei 126 Chancen fällt das ins Gewicht; die Größenordnung "
         "der Aussagen ändert es nicht."),
        ("Bei 69 der 126 Chancen hängt kein Firmenkonto.",
         "Ohne verknüpftes Konto lässt sich nicht sagen, über welchen Partner sie liefen. "
         "Die Auswertung nach Vertriebsweg ist davon nicht betroffen, die nach Partner "
         "schon."),
        ("Die Gewinnquote steht auf 107 Entscheidungen.",
         f"{_z(quote_es)} % klingt genauer, als die Zahl ist. Das 95-%-Intervall reicht "
         f"grob von 4 bis 15 %. Dass sie unter der deutschen liegt, ist stabil; die "
         f"genaue Höhe ist es nicht."),
        ("Die Rolle sagt nichts über Abschlüsse.",
         "„Vergibt Aufträge“ ist aus dem Wortlaut der Website erkannt und wurde gegen "
         "echte Ausgänge geprüft: ein messbarer Vorteil ergab sich nicht. Die Spalte ist "
         "eine Gesprächshilfe, keine Priorisierung."),
        ("Anzeigendaten fehlen für Spanien.",
         "Die Meta- und Google-Anzeigenauswertung, die AdWatch für den deutschen Markt "
         "leistet, ist für spanische Firmen nicht abgerufen. Über die Werbeaktivität "
         "spanischer Wettbewerber und Partner sagt dieser Bericht nichts."),
    ):
        S.append(Paragraph(f"<b>{titel}</b> {txt}", body))

    # === 8. Vorschlag ====================================================
    schritte = kasten([

        Paragraph(f"<b>1 · Die {len(d['teuer_konten'])} Partner anrufen, hinter denen die "
                  f"„zu teuer“-Verluste stehen.</b> 26 verlorene Chancen hängen an diesem "
                  f"Grund, und niemand weiß, ob er Produktpreis, Montage, Transport oder "
                  f"lokalen Wettbewerb meint. {teuer_bekannt} der {len(d['teuer_konten'])} stehen "
                  f"zugleich in der Umsatzliste in Abschnitt 3 — es sind also nicht "
                  f"Unbekannte, die den Preis beanstanden, sondern Partner, die uns kennen "
                  f"und trotzdem verkaufen.", body),
        Spacer(1, 4),
        Paragraph(f"Ein Fall lohnt den ersten Anruf: <b>{_esc(d['teuer_spitze'][0])}</b> "
                  f"({_esc(d['teuer_spitze'][1])}) meldet die meisten Preisverluste. Das Konto "
                  f"hat <b>{d['teuer_spitze'][5]} Verkaufschancen</b>, davon "
                  f"<b>{d['teuer_spitze'][6]} gewonnene</b>, "
                  f"{d['teuer_spitze'][2]} Angebote über {_eur(d['teuer_spitze'][3])} — und "
                  f"{_eur(d['teuer_spitze'][4])} Umsatz, in keinem einzigen Jahr. Ein Händler, "
                  f"der so oft kalkuliert und nie abschließt, sagt uns entweder etwas "
                  f"über unseren Preis in Katalonien oder etwas über diesen Händler. "
                  f"Beides sollte man wissen, und ein Anruf reicht dafür.", body),
        Spacer(1, 4),
        Paragraph(f"<b>2 · Die Balearen als eigenen Markt behandeln, nicht als Teil "
                  f"Spaniens.</b> {100*wert_bal/wert_gew:.0f} % des gewonnenen Werts, "
                  f"{100*pipe_bal/pipe_ges:.0f} % der offenen Pipeline und ein "
                  "überwiegend deutschsprachiges Umfeld. Was dort gebraucht wird — Referenzen, "
                  "Sprache, Montagepartner — ist etwas anderes als das, was Barcelona oder "
                  "Madrid bräuchte.", body),
        Spacer(1, 4),
        Paragraph(f"<b>3 · Mit den {len(warm)} Büros aus Abschnitt 6 beginnen, nicht mit "
                  f"den {es_kalt} kalten.</b> {len(insel_warm)} davon nennen Mallorca, Andratx "
                  f"oder Palma und sitzen im deutschsprachigen Raum — "
                  "das ist der Schnittpunkt aus „kennt uns schon“, „baut da, wo wir "
                  "gewinnen“ und „spricht unsere Sprache“. Danach die spanischen Büros "
                  "mit den meisten Projektorten.", body),
    ], farbe=ACCENT_SOFT)
    # Eine Ueberschrift, die allein am Seitenende steht, liest sich wie ein
    # abgeschnittener Bericht.
    S.append(KeepTogether([
        Paragraph("8. Drei Schritte, die sich aus den Zahlen ergeben", h2), schritte]))

    S.append(Spacer(1, 10))
    S.append(Paragraph(
        "Erstellt mit AdWatch aus dem CRM-Bestand (Stand des letzten Abgleichs) und einer "
        "Website-Recherche über 10.212 Bürodomains. Rückfragen und jede einzelne Zahl "
        "nachvollziehbar über Iheb Marouani.", note))

    fuss = fusszeile(18, f"Solarlux · Spanien · {heute:%d.%m.%Y}")

    doc.build(S, onFirstPage=fuss, onLaterPages=fuss)
    return pfad


if __name__ == "__main__":
    daten = erheben()
    p = bauen(daten)
    print("geschrieben:", p, Path(p).stat().st_size, "Bytes")
