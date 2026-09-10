"""Die Excel: Architekturbüros mit Projekten in Spanien.

WAS IHEB BESTELLT HAT, SPALTE FÜR SPALTE:

    Link je Projekt                  Blatt „Projekte", Spalte Link
    Ort/Adresse je Projekt           Ort, Provinz, Region
    Ansprechpartner für Spanien      eigenes Blatt, löschbar
    Projekte gesamt                  Blatt „Büros"
    davon in Spanien                 dito, mit Anteil
    Hauptsitz                        Straße, PLZ, Ort, Land — zum Filtern
    Niederlassung in Spanien         ja/nein mit Beleg
    Baujahr, Gebäudeart              je Projekt, aus der Seite gelesen

SPANISCHE BÜROS SIND DABEI. Ursprünglich waren sie ausgeschlossen; Iheb hat
das geändert: „for now you can leave the spain achitekturbüros in the crawl,
because we will be able to filter them out later when you provide us with the
column hauptsitz." Sie sind also drin und über `Hauptsitz Land` in einem Klick
weg.

WOHER JEDE ORTSANGABE KOMMT, STEHT DANEBEN. Die Spalte `Quelle` sagt je
Projekt: `Haiku` (Modell hat die Seite gelesen), `gelesen` (im Chat geprüft)
oder `Regel` (nur Mustererkennung). Das ist keine Kosmetik — die
Regel-Zeilen hatten in der Stichprobe 2 von 14 richtig, und für die
spanischen Büros stehen sie noch so da. Wer die Datei benutzt, muss das
unterscheiden können.

DER ANTEIL RECHNET AUF DEM VERORTETEN TEIL. Zähler und Nenner zählen beide
nur Projekte mit erkanntem Ort; wie viele je Büro herausfallen, steht
daneben.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook                                          # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter                           # noqa: E402
from sqlalchemy import text as _sql                                    # noqa: E402

from adwatch import config                                             # noqa: E402
from adwatch.db import SessionLocal                                    # noqa: E402
from adwatch.enrich import regionen                                    # noqa: E402

KOPF = PatternFill("solid", fgColor="1F2933")
WARM = PatternFill("solid", fgColor="EBF2FB")
ROT = PatternFill("solid", fgColor="FFF5F5")
WEISS = Font(color="FFFFFF", bold=True, size=10)
LINK = Font(color="2B6CB0", underline="single", size=10)
RAND = Border(bottom=Side("thin", color="D9E2EC"))

# Portale und Fachzeitschriften. Ihre „Projekte" gehören fremden Architekten.
#
# Zwei Wege hierher, beide gemessen: aju.at LEITET AUF archilovers.com UM — im
# CRM steht die eigene Domain, gelesen wurden 23 fremde Projekte. Und
# architektur-aktuell.at ist eine Zeitschrift, deren „Projekte" Artikel über
# andere Büros sind (Reina Sofía, Bernabéu). Beide standen ungefiltert unter
# den ersten acht Treffern. Geprüft wird deshalb der HOST DER PROJEKT-URL, nicht
# nur die CRM-Domain; die übrigen 14 abweichenden Hosts im Bestand sind echte
# Umbenennungen (esteva.es → esteva.eu, oma.eu → oma.com) und bleiben drin.
# Dazu Plattformen, die im CRM als „Website" eines Büros stehen, weil das Büro
# keine eigene hat: RBA Mueller Ltd hat linkedin.com, Rapp+Bihlmaier hat
# sites.google.com, EMT hat german-architects.com. Was dort gecrawlt wurde,
# gehört der Plattform, nicht dem Büro.
PORTALE = {"archilovers.com", "architonic.com", "archdaily.com",
           "world-architects.com", "german-architects.com",
           "austria-architects.com", "swiss-architects.com",
           "dezeen.com", "divisare.com", "architektur-aktuell.at",
           "baunetz.de", "detail.de", "competitionline.com", "houzz.de",
           "houzz.com", "linkedin.com", "facebook.com", "instagram.com",
           "sites.google.com", "wixsite.com", "jimdosite.com"}


def _ist_portal(url_oder_domain: str) -> bool:
    host = (url_oder_domain or "").lower()
    host = host.split("//")[-1].split("/")[0].split("@")[-1]
    host = host.split(":")[0].removeprefix("www.")
    return any(host == p or host.endswith("." + p) for p in PORTALE)

STUFENTEXT = {5: "gemeinsames Objekt gewonnen", 4: "auf einer Verkaufschance benannt",
              3: "Schriftverkehr vorhanden", 2: "als Debitor angelegt",
              1: "als Lead erfasst", 0: "nur Stammdaten"}


def erheben() -> dict:
    with SessionLocal() as s:
        scans = {r[0]: r for r in s.execute(_sql("""
            SELECT domain, seiten_gelesen, abgeschnitten, projekte_gesamt,
                   projekte_mit_ort, niederlassung_es, gescannt_am, fehler,
                   COALESCE(projekt_urls_bekannt,0), COALESCE(chrome_orte,'')
            FROM arch_web_scan"""))}
        projekte = s.execute(_sql("""
            SELECT domain, url, titel, ort, provinz, region, region_de,
                   region_eindeutig, beleg, quelle, sicherheit, baujahr,
                   baujahr_art, gebaeudeart
            FROM arch_web_projects WHERE land='ES'
            ORDER BY domain, region, ort""")).all()
        kontakte = s.execute(_sql("""
            SELECT domain, url, email, name, spanien_bezug FROM arch_web_contacts
            ORDER BY domain, spanien_bezug DESC""")).all()
        firmen, kv = {}, {}
        for r in s.execute(_sql("""
                SELECT website_domain, name, street, postal_code, city, country,
                       phone, relation_level, relation_why, decision_role,
                       decision_role_evidence, sap_number, COALESCE(arch_projects,0),
                       COALESCE(NULLIF(TRIM(crm_owner),''), kv)
                FROM companies WHERE website_domain <> ''""")):
            dom = (r[0] or "").strip().lower()
            alt = firmen.get(dom)
            if alt is None or (r[7] or 0) > (alt[7] or 0):
                firmen[dom] = r
            # Der KV wird getrennt gesammelt: unter einer Domain liegen oft
            # mehrere CRM-Sätze, und der mit der höchsten Beziehungsstufe ist
            # nicht zwangsläufig der, bei dem ein Kundenverantwortlicher
            # eingetragen ist.
            #
            # Genommen wird `crm_owner` (der Besitzer des CRM-Datensatzes, also
            # der KV im Formularkopf) und nur ersatzweise `kv` aus einem alten
            # Excel-Import. Andersherum stand hier 66 mal „Gimenez, Juan" und
            # 126 mal nichts — weil `kv` nur füllt, wer einen Export hochlädt.
            # Wo beide Werte existieren, sind sie identisch: 0 Abweichungen.
            if (r[13] or "").strip() and dom not in kv:
                kv[dom] = r[13].strip()
    return {"scans": scans, "projekte": projekte, "kontakte": kontakte,
            "firmen": firmen, "kv": kv}


def _punkte(es, anteil, stufe, niederlassung, balearen, jung) -> tuple[int, str]:
    """Sichtbare Summanden statt einer schwarzen Zahl.

    Der `fit_score` dieses Projekts ist im Audit als wertlos aufgefallen, weil
    niemand sagen konnte, woraus er entsteht. Hier steht es daneben.
    """
    p, w = 0, []
    if stufe >= 4:
        p += 30; w.append("Verkaufschance (30)")
    elif stufe == 3:
        p += 20; w.append("Schriftverkehr (20)")
    elif stufe >= 1:
        p += 5; w.append("im CRM (5)")
    if niederlassung:
        p += 20; w.append("Niederlassung in ES (20)")
    for grenze, punkte in ((10, 20), (5, 14), (2, 8), (1, 4)):
        if es >= grenze:
            p += punkte; w.append(f"{es} ES-Projekte ({punkte})"); break
    if anteil is not None:
        for grenze, punkte in ((0.30, 20), (0.15, 13), (0.05, 7)):
            if anteil >= grenze:
                p += punkte; w.append(f"{anteil:.0%} Anteil ({punkte})"); break
    if balearen:
        p += 10; w.append(f"{balearen} auf den Balearen (10)")
    if jung:
        p += 8; w.append(f"{jung} seit 2018 (8)")
    return p, "; ".join(w) or "keine Merkmale"


def _beleg_nl(nl: dict | None) -> str:
    """Der Beleg für eine Niederlassung — die Signale, nicht der Seitentitel.

    Hier stand vorher `nl["zeile"]`, also Titel und Anriss der Fundseite:
    „People – Nordic Office of Architecture · We are 400 architects…". Das
    belegt gar nichts. Was zählt, ist eine +34-Nummer oder eine spanische PLZ
    mit passendem Ort; das Wort „España" allein steht auf jeder Seite eines
    Büros, das in Spanien baut, und reicht deshalb nie.
    """
    if not nl:
        return ""
    teile = []
    if nl.get("plz_mit_ort"):
        teile.append(f"Adresse {nl['plz_mit_ort']}")
    if nl.get("vorwahl_34"):
        teile.append("Telefonnummer +34")
    if not teile:
        return "kein harter Beleg"
    if nl.get("wort_spanien"):
        teile.append("Wort España/Spanien")
    return ", ".join(teile) + f" — auf: {(nl.get('zeile') or '')[:70]}"


def _vollstaendig(sc) -> str:
    if sc[7]:
        return "nein (Fehler)"
    if sc[2]:
        return "nein (Grenze 900 Seiten)"
    bekannt, gelesen = sc[8] or 0, sc[3] or 0
    if bekannt and gelesen < bekannt * 0.6:
        return f"nein (nur {gelesen} von {bekannt} Projektseiten erreichbar)"
    return "ja"


def _kopf(ws, spalten, zeile=1):
    for i, (name, breite) in enumerate(spalten, start=1):
        z = ws.cell(row=zeile, column=i, value=name)
        z.fill, z.font = KOPF, WEISS
        z.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = breite
    ws.row_dimensions[zeile].height = 30
    # Spalte A bleibt beim Scrollen stehen. Ohne das sieht man in einer breiten
    # Tabelle irgendwann Zahlen ohne Büronamen daneben.
    ws.freeze_panes = ws.cell(row=zeile + 1, column=2)
    ws.auto_filter.ref = f"A{zeile}:{get_column_letter(len(spalten))}{zeile}"


def bauen(d: dict, pfad: str | None = None) -> str:
    heute = dt.date.today()
    if pfad is None:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        pfad = str(config.OUTPUT_DIR / f"Solarlux_Spanien_Bueros_{heute:%Y-%m-%d}.xlsx")

    scans, firmen = d["scans"], d["firmen"]
    je_dom, portal_zeilen = defaultdict(list), 0
    for p in d["projekte"]:
        if _ist_portal(p[1]):
            portal_zeilen += 1
            continue
        je_dom[p[0]].append(p)

    wb = Workbook()

    # ---------------- Blatt 1: Büros ---------------------------------------
    #
    # JE REGION EINE SPALTE, nicht eine Spalte mit allen Regionen darin.
    # Vorher stand hier ein Text wie „Andalusien (2), Balearen (1), Katalonien
    # (1)". Für den Excel-Filter ist das EIN Wert: das Auswahlmenü zeigte
    # dutzende Kombinationen und keine einzige Region. Nach Katalonien filtern
    # hieß, jede Kombination anzuhaken, in der Katalonien vorkommt.
    # Jetzt: Spalte „ES: Katalonien" auf „größer als 0" — fertig.
    reg_spalten = sorted({p[6] for ps in je_dom.values() for p in ps if p[6]})

    ws = wb.active
    ws.title = "Büros"
    spalten = [
        ("Büro", 34), ("Website", 24),
        ("Hauptsitz Straße", 26), ("Hauptsitz PLZ", 11), ("Hauptsitz Ort", 18),
        ("Hauptsitz Land", 12),
        ("Niederlassung in ES", 15), ("Beleg Niederlassung", 34),
        ("Projekte gesamt", 12), ("Projekte mit Ort", 12), ("Projekte in ES", 12),
        ("Anteil ES", 10), ("seit 2018", 10),
        ("Hauptregion", 18),
    ] + [(f"ES: {x}", 13) for x in reg_spalten] + [
        ("Städte in ES", 34), ("Gebäudearten", 26),
        ("Beziehung", 10), ("Beziehung heißt", 24), ("KV", 22), ("Punkte", 8),
        ("Punkte woraus", 44), ("Rolle", 15), ("Telefon", 16),
        ("Kontaktseite", 26), ("Ortsangaben geprüft", 15),
        ("ohne erkennbaren Ort", 13), ("Seiten gelesen", 11),
        ("Scan vollständig", 20), ("gescannt am", 17),
    ]
    _kopf(ws, spalten)

    zeilen, leer, portale_raus, nur_nl = [], 0, [], []
    for dom, sc in scans.items():
        f = firmen.get(dom)
        if not f:
            continue
        if _ist_portal(dom):
            portale_raus.append(f"{f[1]} ({dom})")
            continue
        ps = je_dom.get(dom, [])
        nl = json.loads(sc[5] or "null")
        if not ps:
            # KEIN PROJEKT IN SPANIEN, ALSO NICHT IN DIESE LISTE. Bis eben
            # genügte ein Niederlassungsbefund, und weil der 20 Punkte gibt,
            # standen 21 Büros mit null spanischen Projekten ganz OBEN — Drees
            # & Sommer, Nordic Office of Architecture, Hofman Dujardin. Die
            # Liste soll Büros zeigen, die in Spanien bauen.
            #
            # Weggeworfen werden sie trotzdem nicht: die mit hartem Beleg
            # stehen im eigenen Blatt „Niederlassung ohne Projekt". Ein
            # Madrider Büro von Drees & Sommer ist eine Information, nur eben
            # keine Projektliste.
            if nl:
                nur_nl.append((f[1], dom, f[5] or "", _beleg_nl(nl),
                               sc[3] or 0, sc[4] or 0, sc[1] or 0))
            else:
                leer += 1
            continue
        urls = {p[1] for p in ps}
        bal = len({p[1] for p in ps if (p[6] or "") == "Balearen"})
        jung = len({p[1] for p in ps if p[11] and p[11] >= 2018})
        mit_ort = sc[4] or 0
        anteil = (len(urls) / mit_ort) if mit_ort else None
        stufe = f[7] or 0
        gepr = len({p[1] for p in ps if p[9] in ("Haiku", "gelesen")})
        pkt, warum = _punkte(len(urls), anteil, stufe, bool(nl), bal, jung)
        reg = Counter()
        for r in {(p[1], p[6]) for p in ps if p[6]}:
            reg[r[1]] += 1
        staedte = sorted({regionen.ort_schoen(p[3]) for p in ps if p[3]})
        arten = Counter(p[13] for p in ps if p[13])
        kontakt = next((k[1] for k in d["kontakte"] if k[0] == dom and k[4]), "")
        zeilen.append((pkt, [
            f[1], dom, f[2] or "", f[3] or "", f[4] or "", f[5] or "",
            "ja" if nl else "nein", _beleg_nl(nl),
            sc[3] or 0, mit_ort, len(urls), anteil, jung,
            # Bei Gleichstand alphabetisch, nicht nach Einlesereihenfolge:
            # sonst wechselt die Hauptregion zwischen zwei Läufen, ohne dass
            # sich an den Daten etwas geändert hat.
            (sorted(reg.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
             if reg else ""),
        ] + [reg.get(x, 0) or "" for x in reg_spalten] + [
            ", ".join(staedte[:12]) + (f" … +{len(staedte)-12}" if len(staedte) > 12 else ""),
            ", ".join(f"{a} ({n})" for a, n in arten.most_common(4)),
            stufe, STUFENTEXT.get(stufe, ""), d["kv"].get(dom, ""), pkt, warum,
            f[9] or "", f[6] or "", kontakt,
            f"{gepr} von {len(urls)}" if urls else "",
            max((sc[3] or 0) - mit_ort, 0), sc[1] or 0, _vollstaendig(sc), sc[6],
        ]))
    i = {n: k for k, (n, _) in enumerate(spalten)}
    zeilen.sort(key=lambda x: (-x[0], -(x[1][i["Projekte in ES"]] or 0)))

    for r, (_, werte) in enumerate(zeilen, start=2):
        for c, v in enumerate(werte, start=1):
            z = ws.cell(row=r, column=c, value=v)
            z.border = RAND
            if c == 2 and v:
                z.font, z.hyperlink = LINK, f"https://{v}"
            if c == i["Anteil ES"] + 1 and v is not None:
                z.number_format = "0 %"
            if c == i["Kontaktseite"] + 1 and v:
                z.font, z.hyperlink = LINK, v
        if (werte[i["Beziehung"]] or 0) >= 3:
            for c in range(1, len(spalten) + 1):
                ws.cell(row=r, column=c).fill = WARM
        # Spanische Büros getönt: Iheb filtert sie über Hauptsitz Land heraus.
        elif (werte[i["Hauptsitz Land"]] or "") == "ES":
            for c in range(1, len(spalten) + 1):
                ws.cell(row=r, column=c).fill = ROT

    # ---------------- Blatt 2: Projekte ------------------------------------
    wp = wb.create_sheet("Projekte")
    sp2 = [("Büro", 30), ("Hauptsitz Land", 12), ("Website", 20),
           ("Projekt", 42), ("Link", 52), ("Ort", 20), ("Provinz", 18),
           ("Region", 18), ("Region eindeutig", 13), ("Baujahr", 9),
           ("Jahr ist", 12), ("Gebäudeart", 15), ("Quelle", 10),
           ("Sicherheit", 10), ("Beleg", 60)]
    _kopf(wp, sp2)
    r = 2
    for dom, sc in scans.items():
        f = firmen.get(dom)
        if not f or _ist_portal(dom):
            continue
        for p in sorted(je_dom.get(dom, []), key=lambda x: (x[6] or "", x[3] or "")):
            werte = [f[1], f[5] or "", dom, p[2] or "", p[1],
                     regionen.ort_schoen(p[3]) if p[3] else "", p[4] or "",
                     p[6] or "", "" if p[7] is None else ("ja" if p[7] else "nein"),
                     p[11], p[12] or "", p[13] or "", p[9] or "Regel",
                     p[10] or "", (p[8] or "")[:300]]
            for c, v in enumerate(werte, start=1):
                z = wp.cell(row=r, column=c, value=v)
                z.border = RAND
                if c == 5 and v:
                    # Die URL bleibt als TEXT stehen und ist zusätzlich
                    # verlinkt. Ein Zellwert „Projekt öffnen" sähe hübscher
                    # aus, verliert die Adresse aber beim Kopieren, im CSV und
                    # in jeder Auswertung außerhalb von Excel.
                    z.font, z.hyperlink = LINK, v
            if (p[9] or "Regel") == "Regel":
                for c in range(1, len(sp2) + 1):
                    wp.cell(row=r, column=c).fill = ROT
            r += 1

    # ---------------- Blatt 3: Niederlassung ohne Projekt ------------------
    wn = wb.create_sheet("Niederlassung ohne Projekt")
    wn.cell(row=1, column=1, value=(
        "Büros mit einem Hinweis auf ein Büro in Spanien, aber OHNE ein "
        "einziges gefundenes Projekt dort. Sie stehen absichtlich nicht in "
        "der Hauptliste: die zeigt Büros, die in Spanien bauen. Der Hinweis "
        "kann echt sein (Drees & Sommer hat ein Büro in Madrid) oder ein "
        "Fehlalarm — eine +34-Nummer auf einer Teamseite belegt keine "
        "Niederlassung. Ohne Projekt gibt es nichts zum Nachprüfen."))
    wn.cell(row=1, column=1).font = Font(bold=True, size=10)
    wn.merge_cells(start_row=1, start_column=1, end_row=1, end_column=7)
    wn.row_dimensions[1].height = 46
    wn.cell(row=1, column=1).alignment = Alignment(wrap_text=True, vertical="center")
    _kopf(wn, [("Büro", 34), ("Website", 24), ("Hauptsitz Land", 12),
               ("Beleg", 76), ("Projekte gesamt", 12), ("Projekte mit Ort", 12),
               ("Seiten gelesen", 12)], zeile=2)
    r = 3
    for reihe in sorted(nur_nl, key=lambda x: (x[2] == "ES", x[0])):
        for c, v in enumerate(reihe, start=1):
            z = wn.cell(row=r, column=c, value=v)
            z.border = RAND
            if c == 2 and v:
                z.font, z.hyperlink = LINK, f"https://{v}"
        if reihe[2] == "ES":
            for c in range(1, 8):
                wn.cell(row=r, column=c).fill = ROT
        r += 1

    # Ein eigenes Blatt „Regionen" gab es hier bis eben. Es steht jetzt
    # als Spalten in „Büros" — dieselben Zahlen an zwei Stellen wären
    # zwei Stellen, an denen sie auseinanderlaufen können.

    # ---------------- Blatt 4: Kontakte (löschbar) -------------------------
    wk = wb.create_sheet("Kontakte (personenbezogen)")
    wk.cell(row=1, column=1, value=(
        "PERSONENBEZOGENE DATEN — dieses Blatt löschen, bevor die Datei "
        "weitergegeben wird. Gefunden auf den öffentlichen Kontakt- und "
        "Teamseiten. „Spanien-Bezug\" heißt: auf DERSELBEN Seite stand "
        "España/Spanien oder eine +34-Nummer — ein Hinweis, kein Beweis, dass "
        "die Person für Spanien zuständig ist. Je Büro höchstens 12 Adressen, "
        "Spanien-Bezug zuerst."))
    wk.cell(row=1, column=1).font = Font(bold=True, color="9B2C2C", size=10)
    wk.merge_cells(start_row=1, start_column=1, end_row=1, end_column=6)
    wk.row_dimensions[1].height = 44
    wk.cell(row=1, column=1).alignment = Alignment(wrap_text=True, vertical="center")
    _kopf(wk, [("Büro", 30), ("Website", 20), ("Name", 26), ("E-Mail", 34),
               ("Spanien-Bezug", 12), ("gefunden auf", 46)], zeile=2)
    r = 3
    mit_es = {w[1][1] for w in zeilen}
    # Höchstens 12 Adressen je Büro, Spanien-Bezug zuerst. Eine Domain hatte
    # 2.520 Treffer — ein Impressumsverzeichnis, kein Ansprechpartner. Ohne
    # Deckel bestünde das Blatt zur Hälfte aus dieser einen Website.
    je_dom_n: Counter = Counter()
    for k in d["kontakte"]:
        f = firmen.get(k[0])
        if not f or k[0] not in mit_es:
            continue
        je_dom_n[k[0]] += 1
        if je_dom_n[k[0]] > 12:
            continue
        for c, v in enumerate([f[1], k[0], k[3] or "", k[2] or "",
                               "ja" if k[4] else "nein", k[1]], start=1):
            z = wk.cell(row=r, column=c, value=v)
            z.border = RAND
            if c == 6 and v:
                z.font, z.hyperlink = LINK, v
        if k[4]:
            for c in range(1, 7):
                wk.cell(row=r, column=c).fill = WARM
        r += 1

    # ---------------- Blatt 5: Methodik ------------------------------------
    wm = wb.create_sheet("Methodik")
    wm.column_dimensions["A"].width = 30
    wm.column_dimensions["B"].width = 112
    mit_kv = sum(1 for _, w in zeilen if w[i["KV"]])
    n_ki = sum(1 for p in d["projekte"] if p[9] == "Haiku")
    n_gel = sum(1 for p in d["projekte"] if p[9] == "gelesen")
    n_reg = sum(1 for p in d["projekte"] if (p[9] or "Regel") == "Regel")
    texte = [
        ("Was diese Datei ist",
         f"Architektur- und Planungsbüros mit MINDESTENS EINEM gefundenen "
         f"Projekt in Spanien: {len(zeilen)} Büros, "
         f"{sum(len({p[1] for p in v}) for v in je_dom.values())} Projekte. "
         f"Spanische Büros sind enthalten — über „Hauptsitz Land“ filterbar. "
         f"Wer kein Projekt in Spanien hat, steht nicht hier; {len(nur_nl)} "
         f"Büros mit einem Niederlassungshinweis ohne Projekt stehen im "
         f"Blatt „Niederlassung ohne Projekt“, {leer} gescannte Büros ohne "
         f"beides gar nicht."),
        ("Die wichtigste Spalte: Quelle",
         f"Je Projekt steht, woher der Ort kommt. `Haiku` ({n_ki}): ein Modell "
         f"hat die ganze Seite gelesen. `gelesen` ({n_gel}): im Chat geprüft, "
         f"Seite für Seite. `Regel` ({n_reg}): nur Mustererkennung — DIESE "
         f"Zeilen sind rot hinterlegt und unzuverlässig. In einer Stichprobe "
         f"von 14 solchen Zeilen waren 2 richtig. Sie betreffen fast nur "
         f"spanische Büros, bei denen „liegt in Spanien\" ohnehin meist stimmt."),
        ("KV",
         f"Der Kundenverantwortliche, wie er im CRM oben rechts steht — der "
         f"Besitzer des Datensatzes (`ownerid`), live abgefragt. Gefüllt bei "
         f"{mit_kv} der {len(zeilen)} Büros. "
         f"In der ersten Fassung dieser Datei stand er nur bei 66, alle "
         f"„Gimenez, Juan“: gelesen wurde damals das Feld `kv`, und das füllt "
         f"sich nur, wenn jemand einen Excel-Export hochlädt — für Spanien war "
         f"das passiert, für den Rest nie. Wo beide Quellen einen Wert haben, "
         f"stimmen sie überein (0 Abweichungen bei 66 Vergleichen). "
         f"INTERN — Name einer Kollegin oder eines Kollegen; vor dem "
         f"Weitergeben nach außen löschen."),
        ("Nach Region und Stadt filtern",
         "REGION: im Blatt „Büros“ hat jede Region eine eigene Spalte, etwa "
         "„ES: Katalonien“. Filter auf „größer als 0“ — oder die Spalte "
         "„Hauptregion“, wenn nur die wichtigste zählt (bei Gleichstand "
         "alphabetisch). Vorher stand hier eine einzige Spalte mit "
         "„Andalusien (2), Balearen (1), Katalonien (1)“ darin: für Excel ist "
         "das EIN Wert, das Filtermenü zeigte dutzende Kombinationen und "
         "keine einzige Region. "
         "STADT: im Blatt „Projekte“, Spalte „Ort“ — dort steht je Zeile genau "
         "ein Ort, das filtert sauber. „Städte in ES“ im Blatt „Büros“ ist "
         "zum Lesen da, nicht zum Filtern."),
        ("Wie gecrawlt wurde",
         "Zwei Stufen. Erst ein billiger Vorabtest über alle 10.212 Domains "
         "(Startseite, Projektübersichten, Sitemap — 2,6 Seiten je Domain), "
         "der auf jedes Spanien-Signal anspringt. Nur die Treffer werden dann "
         "vollständig gelesen. Gemessene Trefferquote des Vorabtests: 97 % der "
         "bestätigten Büros."),
        ("Woher der Ort kommt",
         "Aus dem beschrifteten Feld der Seite („Location: Madrid, Spain\"), "
         "dem Projekttitel oder dem Kopf des Projektblocks. NICHT aus dem "
         "Fließtext: dort stehen Verlagsorte, Büroadressen, andere Projekte "
         "und Werbeprosa. Fünf solche Fehlerklassen wurden gefunden und "
         "beseitigt, jede mit einem eigenen Beispiel im Quelltext."),
        ("Baujahr",
         "Das Jahr der Fertigstellung, wenn die Seite es nennt. „Jahr ist\" "
         "sagt, ob es Fertigstellung, Baubeginn, Wettbewerb oder Planung "
         "meint — ein Wettbewerb 2011 und eine Fertigstellung 2011 bedeuten "
         "Verschiedenes. Ein Copyright-Jahr aus der Fußzeile zählt nicht."),
        ("Niederlassung in Spanien",
         "„ja\" nur bei einer +34-Nummer oder einer spanischen Postleitzahl, "
         "deren Ort daneben steht. Das Wort „España\" allein reicht nicht — es "
         "steht auf jeder Seite eines Büros, das in Spanien baut."),
        ("Punkte",
         "Beziehung (max. 30) + Niederlassung (20) + Zahl der ES-Projekte "
         "(max. 20) + Anteil (max. 20) + Balearen (10) + Projekte seit 2018 "
         "(8). Jede Zeile trägt ihre Zusammensetzung daneben. Eine "
         "Sortierhilfe, keine Prognose."),
        ("Was NICHT eingeht",
         "Die Rolle („vergibt Aufträge\") wurde gegen echte Ausgänge geprüft "
         "und trennt nicht. Sie steht als Information da, nicht als Punkte."),
        ("Bekannte Fehler in den Stammdaten",
         "aju_architekt (AT) hat archilovers.com als Website — ein Portal, "
         "nicht die eigene Seite; seine Projekte gehören anderen Büros. "
         f"Solche Seiten sind herausgenommen — {portal_zeilen} Projektzeilen "
         "auf Portalen und Fachzeitschriften"
         + (", ganze Büros: " + ", ".join(portale_raus) if portale_raus else "")
         + ". Architects Orange steht als DE und baut in Kalifornien. Chapman "
         "Taylor steht als DE und sitzt in London. Die Stammdaten liegen im "
         "CRM, das hier nur gelesen wird — korrigieren muss sie jemand dort."),
        ("Grenzen",
         "Die Orte tragen kein Datum, wo kein Baujahr steht. "
         "Eine Seite, die nirgends sagt, wo sie steht, ist für keine "
         "Methode auffindbar — solche Projekte fehlen in „Projekte in ES“ und "
         "stehen in „ohne erkennbaren Ort“."),
        ("Erstellt", f"{dt.datetime.now():%d.%m.%Y %H:%M} · tools/spanien_excel.py"),
    ]
    for i2, (k, v) in enumerate(texte, start=1):
        a = wm.cell(row=i2, column=1, value=k)
        a.font = Font(bold=True, size=10)
        a.alignment = Alignment(vertical="top")
        b = wm.cell(row=i2, column=2, value=v)
        b.alignment = Alignment(wrap_text=True, vertical="top")
        wm.row_dimensions[i2].height = max(30, 13 * (len(v) // 108 + 1))

    wb.save(pfad)
    return pfad


if __name__ == "__main__":
    d = erheben()
    if not d["scans"]:
        print("Keine Scandaten — erst tools/spanienlauf.py laufen lassen.")
        raise SystemExit(1)
    p = bauen(d)
    print("geschrieben:", p, Path(p).stat().st_size, "Bytes")
