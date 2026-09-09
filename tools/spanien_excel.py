"""Die Excel: europäische Architekturbüros mit Projekten in Spanien.

WAS DRIN IST UND WARUM SO.
Iheb hat fünf Dinge bestellt, und jedes davon bestimmt ein Blatt:

    Büros        eine Zeile je Büro, mit Niederlassung, Projektzahlen und Anteil
    Projekte     eine Zeile je Projekt, mit Link, Ort, Provinz und Region
    Regionen     Kreuztabelle Büro × Region, zum Filtern nach Katalonien usw.
    Kontakte     die gefundenen Personen — LÖSCHBAR, siehe unten
    Methodik     woher jede Spalte kommt und was sie nicht weiß

SPANISCHE BÜROS SIND NICHT DABEI. „we have to take out the spanish
architekturbüros out because we will be in contact with them anyway." Übrig
bleiben die europäischen Büros, die von außen in Spanien bauen.

DER ANTEIL RECHNET AUF DEM VERORTETEN TEIL. Iheb hat sich für „only projects
with a readable location" entschieden: Zähler und Nenner zählen beide nur
Projekte, deren Ort erkannt wurde. Ein Büro, das seine Projekte ohne Ort
zeigt, verzerrt damit niemanden — es fällt nur aus der Rechnung. Wie viele
das je Büro sind, steht als eigene Spalte daneben, damit man sieht, auf
welcher Grundlage der Prozentsatz steht.

DIE KONTAKTE LIEGEN SEPARAT. Auch das war eine Entscheidung von Iheb: „Both
columns, names in a separate sheet." Das Hauptblatt bleibt firmenbezogen und
teilbar; wer die Datei weitergibt, löscht ein Blatt und ist fertig.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook                                   # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter                    # noqa: E402
from sqlalchemy import text as _sql                             # noqa: E402

from adwatch import config                                      # noqa: E402
from adwatch.db import SessionLocal                             # noqa: E402
from adwatch.enrich import regionen                             # noqa: E402

KOPF = PatternFill("solid", fgColor="1F2933")
WARM = PatternFill("solid", fgColor="EBF2FB")
GRAU = PatternFill("solid", fgColor="F7F9FB")
WEISS = Font(color="FFFFFF", bold=True, size=10)
LINK = Font(color="2B6CB0", underline="single", size=10)
RAND = Border(bottom=Side("thin", color="D9E2EC"))

STUFENTEXT = {5: "gemeinsames Objekt gewonnen", 4: "auf einer Verkaufschance benannt",
              3: "Schriftverkehr vorhanden", 2: "als Debitor angelegt",
              1: "als Lead erfasst", 0: "nur Stammdaten"}


# ---------------------------------------------------------------------------
# Messen
# ---------------------------------------------------------------------------

def erheben() -> dict:
    with SessionLocal() as s:
        scans = {r[0]: r for r in s.execute(_sql("""
            SELECT domain, company_ids, seiten_gelesen, abgeschnitten,
                   projekte_gesamt, projekte_mit_ort, projekte_es,
                   niederlassung_es, gescannt_am, fehler,
                   COALESCE(projekt_urls_bekannt, 0)
            FROM arch_web_scan"""))}
        projekte = s.execute(_sql("""
            SELECT domain, url, titel, ort, provinz, region, region_de,
                   region_eindeutig, land, beleg FROM arch_web_projects
            ORDER BY domain, land, region, ort""")).all()
        kontakte = s.execute(_sql("""
            SELECT domain, url, email, name, spanien_bezug
            FROM arch_web_contacts ORDER BY domain, spanien_bezug DESC""")).all()
        if not scans:
            return {"scans": {}, "projekte": [], "kontakte": [], "firmen": {}}
        doms = sorted(scans)
        firmen = {}
        for r in s.execute(
                _sql("SELECT website_domain, id, name, city, country, phone, email, "
                     "relation_level, relation_why, decision_role, solarlux_relevance, "
                     "office_type, project_focus, sap_number, linkedin_url "
                     "FROM companies WHERE website_domain IN :d")
                .bindparams(__import__("sqlalchemy").bindparam("d", expanding=True)),
                {"d": doms}):
            dom = (r[0] or "").strip().lower()
            alt = firmen.get(dom)
            # Mehrere CRM-Zeilen je Domain: die mit der engsten Beziehung gewinnt.
            if alt is None or (r[7] or 0) > (alt[7] or 0):
                firmen[dom] = r
    return {"scans": scans, "projekte": projekte, "kontakte": kontakte,
            "firmen": firmen}


def _bewerten(es: int, anteil: float | None, stufe: int, niederlassung: bool,
              balearen: int) -> tuple[int, str]:
    """Die Relevanz — als Summe sichtbarer Teile, nicht als schwarze Zahl.

    Iheb: die Zahl der Spanien-Projekte UND ihr Anteil sollen beide zählen.
    Beides steht deshalb hier, und zwar getrennt: eine absolute Zahl belohnt
    das Büro, das viel in Spanien baut; der Anteil belohnt das, für das
    Spanien wichtig ist. Ein Londoner Großbüro mit 12 spanischen von 800
    Projekten und ein Hamburger mit 4 von 9 sind beide interessant, aus
    verschiedenen Gründen.

    Die Begründung wandert als Text in die Zelle daneben. Eine Punktzahl,
    deren Zustandekommen man nicht sieht, ist im Audit dieses Projekts schon
    einmal als wertlos aufgefallen (`fit_score`); dieser Fehler wird hier
    nicht wiederholt.
    """
    punkte, warum = 0, []
    if stufe >= 4:
        punkte += 30; warum.append("Verkaufschance (30)")
    elif stufe == 3:
        punkte += 20; warum.append("Schriftverkehr (20)")
    elif stufe >= 1:
        punkte += 5; warum.append("im CRM (5)")
    if niederlassung:
        punkte += 20; warum.append("Niederlassung in ES (20)")
    if es >= 10:
        punkte += 20; warum.append(f"{es} ES-Projekte (20)")
    elif es >= 5:
        punkte += 14; warum.append(f"{es} ES-Projekte (14)")
    elif es >= 2:
        punkte += 8; warum.append(f"{es} ES-Projekte (8)")
    elif es == 1:
        punkte += 4; warum.append("1 ES-Projekt (4)")
    if anteil is not None:
        if anteil >= 0.30:
            punkte += 20; warum.append(f"{anteil:.0%} Anteil (20)")
        elif anteil >= 0.15:
            punkte += 13; warum.append(f"{anteil:.0%} Anteil (13)")
        elif anteil >= 0.05:
            punkte += 7; warum.append(f"{anteil:.0%} Anteil (7)")
    if balearen:
        punkte += 10; warum.append(f"{balearen} auf den Balearen (10)")
    return punkte, "; ".join(warum) or "keine Merkmale"


# ---------------------------------------------------------------------------
# Setzen
# ---------------------------------------------------------------------------

def _vollstaendig(sc) -> str:
    """War der Scan vollstaendig — und wenn nicht, woran lag es?

    Zwei verschiedene Gruende, und sie duerfen nicht dieselbe Zelle fuellen:
    die Notbremse bei 900 Seiten ist unsere Entscheidung, ein abweisender Host
    ist seine. Gemessen an acme.ac: 73 Projektadressen in der Sitemap, 0
    gelesen — „0 Projekte" waere dort eine Aussage ueber unsere Verbindung
    gewesen, nicht ueber das Buero.
    """
    if sc[9]:
        return "nein (Fehler)"
    if sc[3]:
        return "nein (Grenze 900 Seiten)"
    bekannt, gelesen = sc[10] or 0, sc[4] or 0
    if bekannt and gelesen < bekannt * 0.6:
        return f"nein (nur {gelesen} von {bekannt} Projektseiten erreichbar)"
    return "ja"


def _kopfzeile(ws, spalten: list[tuple[str, int]]) -> None:
    for i, (name, breite) in enumerate(spalten, start=1):
        z = ws.cell(row=1, column=i, value=name)
        z.fill, z.font = KOPF, WEISS
        z.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = breite
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(spalten))}1"


def bauen(d: dict, pfad: str | None = None) -> str:
    heute = dt.date.today()
    if pfad is None:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        pfad = str(config.OUTPUT_DIR / f"Solarlux_Spanien_Bueros_{heute:%Y-%m-%d}.xlsx")

    scans, firmen = d["scans"], d["firmen"]
    je_domain: dict[str, list] = defaultdict(list)
    for p in d["projekte"]:
        je_domain[p[0]].append(p)

    wb = Workbook()

    # ---------------- Blatt 1: Büros ---------------------------------------
    ws = wb.active
    ws.title = "Büros"
    spalten = [
        ("Büro", 34), ("Website", 26), ("Sitz", 16), ("Land", 6),
        ("Niederlassung in ES", 15), ("Beleg Niederlassung", 30),
        ("Projekte mit Ort", 12), ("Projekte in ES", 11), ("Anteil ES", 9),
        ("davon Balearen", 12), ("Regionen in ES", 26), ("Städte in ES", 34),
        ("Beziehung", 9), ("Beziehung heißt", 24), ("Punkte", 8),
        ("Punkte woraus", 42), ("Rolle", 15), ("Telefon", 16),
        ("Kontaktseite", 28), ("Projekte gesamt (Seiten)", 13),
        ("ohne erkennbaren Ort", 13), ("Seiten gelesen", 11),
        ("Scan vollständig", 12), ("gescannt am", 17),
    ]
    _kopfzeile(ws, spalten)

    zeilen = []
    for dom, sc in scans.items():
        f = firmen.get(dom)
        if not f:
            continue
        ps = je_domain.get(dom, [])
        es_p = [p for p in ps if p[8] == "ES"]
        es_urls = {p[1] for p in es_p}
        regionen_hier = Counter(p[6] or p[5] for p in es_p if (p[5] or p[6]))
        staedte = sorted({regionen.ort_schoen(p[3]) for p in es_p if p[3]})
        balearen = sum(1 for p in es_p if (p[5] or "") == "Illes Balears")
        mit_ort, in_es = sc[5] or 0, len(es_urls)
        anteil = (in_es / mit_ort) if mit_ort else None
        nl = json.loads(sc[7] or "null")
        stufe = f[7] or 0
        punkte, warum = _bewerten(in_es, anteil, stufe, bool(nl), balearen)
        kontaktseite = next((k[1] for k in d["kontakte"]
                             if k[0] == dom and k[4]), None)
        zeilen.append((punkte, [
            f[2], dom, f[3], f[4],
            "ja" if nl else "nein",
            (nl or {}).get("zeile") or (nl or {}).get("plz_mit_ort") or "",
            mit_ort, in_es, anteil, balearen,
            ", ".join(f"{r} ({n})" for r, n in regionen_hier.most_common()),
            ", ".join(staedte[:14]) + (f" … +{len(staedte)-14}" if len(staedte) > 14 else ""),
            stufe, STUFENTEXT.get(stufe, ""), punkte, warum,
            f[9] or "", f[5] or "", kontaktseite or "",
            sc[4] or 0, (sc[4] or 0) - mit_ort, sc[2] or 0,
            _vollstaendig(sc), sc[8],
        ]))
    zeilen.sort(key=lambda x: (-x[0], -(x[1][7] or 0)))

    for r, (_, werte) in enumerate(zeilen, start=2):
        for c, v in enumerate(werte, start=1):
            z = ws.cell(row=r, column=c, value=v)
            z.border = RAND
            if c == 2 and v:
                z.value, z.font = v, LINK
                z.hyperlink = f"https://{v}"
            if c == 9 and v is not None:
                z.number_format = "0 %"
            if c == 19 and v:
                z.font = LINK
                z.hyperlink = v
        if (werte[12] or 0) >= 3:
            for c in range(1, len(spalten) + 1):
                ws.cell(row=r, column=c).fill = WARM

    # ---------------- Blatt 2: Projekte ------------------------------------
    wp = wb.create_sheet("Projekte")
    sp2 = [("Büro", 32), ("Website", 22), ("Projekt", 46), ("Link", 52),
           ("Ort", 20), ("Provinz", 18), ("Region", 20), ("Region (dt.)", 18),
           ("Region eindeutig", 13), ("Land", 6), ("Ort erkannt an", 20)]
    _kopfzeile(wp, sp2)
    r = 2
    for dom, sc in scans.items():
        f = firmen.get(dom)
        if not f:
            continue
        for p in sorted(je_domain.get(dom, []), key=lambda x: (x[8] != "ES", x[6] or "", x[3] or "")):
            werte = [f[2], dom, p[2] or "", p[1],
                     regionen.ort_schoen(p[3]) if p[3] else "", p[4] or "",
                     p[5] or "", p[6] or "",
                     "" if p[7] is None else ("ja" if p[7] else "nein"),
                     p[8] or "", p[9] or ""]
            for c, v in enumerate(werte, start=1):
                z = wp.cell(row=r, column=c, value=v)
                z.border = RAND
                if c == 4 and v:
                    z.font, z.hyperlink = LINK, v
                    z.value = "Projekt öffnen"
            if p[8] == "ES":
                for c in range(1, len(sp2) + 1):
                    wp.cell(row=r, column=c).fill = WARM
            r += 1

    # ---------------- Blatt 3: Regionen ------------------------------------
    wr = wb.create_sheet("Regionen")
    alle_regionen = sorted({(p[6] or p[5]) for p in d["projekte"]
                            if p[8] == "ES" and (p[5] or p[6])})
    sp3 = [("Büro", 34), ("Website", 24), ("Projekte in ES", 12)] + \
          [(reg, 15) for reg in alle_regionen]
    _kopfzeile(wr, sp3)
    r = 2
    for punkte, werte in zeilen:
        dom = werte[1]
        es_p = [p for p in je_domain.get(dom, []) if p[8] == "ES"]
        if not es_p:
            continue
        zaehl = Counter(p[6] or p[5] for p in es_p if (p[5] or p[6]))
        reihe = [werte[0], dom, werte[7]] + [zaehl.get(reg, 0) or "" for reg in alle_regionen]
        for c, v in enumerate(reihe, start=1):
            z = wr.cell(row=r, column=c, value=v)
            z.border = RAND
            if c > 3 and v:
                z.fill = WARM
        r += 1

    # ---------------- Blatt 4: Kontakte (löschbar) -------------------------
    wk = wb.create_sheet("Kontakte (personenbezogen)")
    wk.cell(row=1, column=1,
            value="PERSONENBEZOGENE DATEN — dieses Blatt löschen, bevor die Datei "
                  "weitergegeben wird. Gefunden auf den öffentlichen Kontakt- und "
                  "Teamseiten der Büros. „Spanien-Bezug\" heißt: auf DERSELBEN Seite "
                  "stand España/Spanien oder eine +34-Nummer.")
    wk.cell(row=1, column=1).font = Font(bold=True, color="9B2C2C", size=10)
    wk.merge_cells(start_row=1, start_column=1, end_row=1, end_column=6)
    wk.row_dimensions[1].height = 42
    wk.cell(row=1, column=1).alignment = Alignment(wrap_text=True, vertical="center")
    sp4 = [("Büro", 32), ("Website", 22), ("Name", 26), ("E-Mail", 34),
           ("Spanien-Bezug", 12), ("gefunden auf", 52)]
    for i, (name, breite) in enumerate(sp4, start=1):
        z = wk.cell(row=2, column=i, value=name)
        z.fill, z.font = KOPF, WEISS
        wk.column_dimensions[get_column_letter(i)].width = breite
    wk.freeze_panes = "A3"
    wk.auto_filter.ref = f"A2:{get_column_letter(len(sp4))}2"
    r = 3
    for k in d["kontakte"]:
        f = firmen.get(k[0])
        if not f:
            continue
        werte = [f[2], k[0], k[3] or "", k[2] or "", "ja" if k[4] else "nein", k[1]]
        for c, v in enumerate(werte, start=1):
            z = wk.cell(row=r, column=c, value=v)
            z.border = RAND
            if c == 6 and v:
                z.font, z.hyperlink, z.value = LINK, v, "Seite öffnen"
        if k[4]:
            for c in range(1, len(sp4) + 1):
                wk.cell(row=r, column=c).fill = WARM
        r += 1

    # ---------------- Blatt 5: Methodik ------------------------------------
    wm = wb.create_sheet("Methodik")
    wm.column_dimensions["A"].width = 30
    wm.column_dimensions["B"].width = 110
    gescannt = len(scans)
    fehler = sum(1 for s in scans.values() if s[9])
    abgeschnitten = sum(1 for s in scans.values() if s[3])
    seiten = sum(s[2] or 0 for s in scans.values())
    texte = [
        ("Was diese Datei ist",
         "Europäische Architektur- und Planungsbüros, die in Spanien bauen, aber "
         "NICHT in Spanien sitzen. Spanische Büros sind bewusst nicht enthalten."),
        ("Woher die Büros kommen",
         "Aus dem CRM-Bestand: Segment Architekten, Untersegment Architekturbüro, "
         "Sitz in einem europäischen Land außer Spanien, mit Website. Die "
         "Vorauswahl „baut in Spanien\" stammt aus einem früheren, flachen "
         "Durchgang über 10.212 Domains."),
        ("Wie tief gelesen wurde",
         f"Jede erreichbare Seite jeder Website — Sitemap zuerst, dann den Links "
         f"folgend. {gescannt} Domains, {seiten:,} Seiten gelesen. Keine "
         f"Seitenbegrenzung außer einer Notbremse bei 900 Seiten je Domain; wo "
         f"sie gegriffen hat, steht in der Spalte „Scan vollständig\" ein „nein\" "
         f"({abgeschnitten} Fälle).".replace(",", ".")),
        ("Woran ein spanischer Ort erkannt wird",
         "Ein spanischer Ortsname zählt nur mit Grund: er steht im Projekttitel, "
         "er ist eine Insel oder Region, er ist eine Stadt mit mindestens fünf "
         "Postleitzahlen, oder der Landesname steht daneben. Ohne diese Regel "
         "meldete ein Londoner Büro neun spanische Projekte — die Namen seiner "
         "Projektteams: María, Borja, Cristóbal, Javier. Spanische Vor- und "
         "Nachnamen sind fast immer auch Gemeindenamen, und ihre Größe hilft "
         "nicht weiter: Andratx, Calvià und Sitges haben genauso eine einzige "
         "Postleitzahl. Der Grund steht je Projektzeile in „Ort erkannt an\"."),
        ("Was als Projekt zählt",
         "Eine eigene Projekt- oder Referenzseite der Website. Dieselbe Seite "
         "unter mehreren Adressen zählt einmal. Übersichtsseiten zählen nicht."),
        ("Wie der Ort gefunden wird",
         "Titel und Fließtext der Projektseite werden gegen ein Verzeichnis von "
         "315.000 europäischen Postleitzahlen abgeglichen. Das Navigationsmenü "
         "wird vorher entfernt — ohne das erbt jede Projektseite die Orte aller "
         "anderen Projekte, was im Probelauf ein bayerisches Wohnhaus nach "
         "Mallorca verlegte."),
        ("Warum der Anteil auf „Projekte mit Ort\" rechnet",
         "Nicht jedes Büro schreibt den Ort auf die Projektseite. Zähler und "
         "Nenner zählen deshalb beide nur Projekte mit erkanntem Ort. Wie viele "
         "Projekte je Büro dabei herausfallen, steht in „ohne erkennbaren Ort\"."),
        ("Provinz und Region",
         "Über die spanische Postleitzahl: die ersten zwei Ziffern sind die "
         "Provinz, die Provinz gehört zu einer Autonomen Gemeinschaft. Kommt ein "
         "Ortsname in mehreren Provinzen vor, gewinnt die häufigere und die "
         "Spalte „Region eindeutig\" steht auf „nein\"."),
        ("Niederlassung in Spanien",
         "„ja\" nur bei einer +34-Nummer oder einer spanischen Postleitzahl, "
         "deren Ort daneben steht. Das Wort „España\" allein reicht nicht: es "
         "steht auf jeder Seite eines Büros, das in Spanien baut. Der Beleg "
         "steht in der Nachbarspalte."),
        ("Punkte",
         "Beziehung (max. 30) + Niederlassung (20) + Zahl der ES-Projekte "
         "(max. 20) + Anteil (max. 20) + Balearen (10). Jede Zeile trägt in "
         "„Punkte woraus\" ihre eigene Zusammensetzung — die Zahl ist eine "
         "Sortierhilfe, keine Prognose."),
        ("Was hier NICHT eingeht",
         "Die Rolle („vergibt Aufträge\") wurde gegen echte Ausgänge geprüft und "
         "trennt nicht; sie steht als Information da, nicht als Punkte. Die "
         "Relevanz-Schätzung aus dem Sprachmodell ebenso wenig."),
        ("Grenzen",
         f"Die Orte tragen kein Datum — ein Projekt von 2011 sieht aus wie eines "
         f"von 2025. {fehler} Domains waren beim Scan nicht erreichbar. Büros, "
         f"deren spanische Projekte der frühere flache Durchgang nicht gefunden "
         f"hat, fehlen hier noch; der tiefe Durchgang über alle europäischen "
         f"Nicht-Spanier läuft danach."),
        ("Erstellt", f"{dt.datetime.now():%d.%m.%Y %H:%M} mit AdWatch, "
                     f"tools/spanien_excel.py"),
    ]
    for i, (k, v) in enumerate(texte, start=1):
        a = wm.cell(row=i, column=1, value=k)
        a.font = Font(bold=True, size=10)
        a.alignment = Alignment(vertical="top")
        b = wm.cell(row=i, column=2, value=v)
        b.alignment = Alignment(wrap_text=True, vertical="top")
        wm.row_dimensions[i].height = max(30, 13 * (len(v) // 105 + 1))

    wb.save(pfad)
    return pfad


if __name__ == "__main__":
    d = erheben()
    if not d["scans"]:
        print("Noch keine Scandaten — erst tools/tiefenlauf_start.py laufen lassen.")
        raise SystemExit(1)
    p = bauen(d)
    print("geschrieben:", p, Path(p).stat().st_size, "Bytes")
    print(f"  Büros: {len(d['scans'])} | Projektzeilen: {len(d['projekte'])} | "
          f"Kontakte: {len(d['kontakte'])}")
