"""Tiefenlauf: die GANZE Website eines Büros lesen, Projekt für Projekt.

WARUM NEBEN `laenderlauf.py` UND NICHT DARIN.
Der Länderlauf beantwortet eine Ja/Nein-Frage über 10.212 Domains: baut dieses
Büro in Spanien? Dafür reichen 14 Seiten und ein Textklumpen, und die Grenze
ist richtig — 10.212 Domains vollständig zu lesen wäre ein Tagewerk für eine
Antwort, die schon nach der Referenzseite feststeht.

Hier ist die Frage eine andere, und sie verlangt das Gegenteil:

    Wie viele Projekte hat das Büro insgesamt, wie viele davon in Spanien,
    und wo genau — mit Link auf das einzelne Projekt.

Ein Anteil braucht einen ehrlichen Nenner. Wer nach 14 Seiten aufhört, zählt
nicht „alle Projekte", sondern „die ersten 14 Seiten voll Projekte" — und der
Prozentsatz daraus ist eine Zahl ohne Bedeutung. Iheb dazu: „dont make the page
maxing, crawl the whole website to make sure the data is accurate."

Deshalb liest dieser Lauf jede erreichbare Seite einer Domain und legt JEDE
Projektseite als eigene Zeile ab, mit URL, Titel und erkanntem Ort. Er ist
für 82 Domains gebaut, nicht für 10.212.

WAS ER SPEICHERT UND WARUM EINZELN.
Ein Textklumpen je Domain kann die Frage nicht beantworten: „Barcelona" darin
sagt nicht, ob EIN Projekt in Barcelona liegt oder zwanzig. Die Projektzeile
ist die kleinste Einheit, in der die Frage überhaupt Sinn ergibt — und
zugleich die, die den Link trägt, den Iheb haben will.

KEINE PERSONENDATEN IM HAUPTPFAD. Gefundene `mailto:`-Adressen und die Namen
daneben landen in einer EIGENEN Tabelle (`arch_web_contacts`), damit die
Bürotabelle firmenbezogen bleibt und die Namen als eigenes Blatt gelöscht
werden können, bevor die Datei das Haus verlässt.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlsplit

import requests
from sqlalchemy import text as _sql

from ..db import SessionLocal
from ..identity.website_source import _UA
from . import laender, regionen, render
from .laenderlauf import _ist_projekt_pfad, _KEIN_INHALT, _startseite

logger = logging.getLogger("adwatch.tiefenlauf")

# Zwanzig gleichzeitige Domains. Die Grenze ist NICHT die Hoeflichkeit -- jeder
# Arbeiter haelt einen anderen Host, und innerhalb einer Site wird ohnehin
# sequenziell mit Pause gelesen. Die Grenze war der BROWSER: `render_html`
# startet je Aufruf ein eigenes Chromium, und zwanzig davon gleichzeitig sind
# mehrere Gigabyte. Deshalb haengt das Rendern jetzt an einer eigenen,
# kleineren Schranke (`_BROWSER`) und nicht mehr an der Zahl der Arbeiter.
# 14 Kerne, I/O-gebundene Arbeit, gemessen 0 Fehler bei 6 Arbeitern.
ARBEITER = 20

# Wie viele Chromium-Instanzen gleichzeitig laufen duerfen. Wer keinen Platz
# bekommt, rendert nicht -- der einfache Abruf steht ja schon da. Ein duenner
# Text ist ein kleiner Verlust, ein erschoepfter Arbeitsspeicher ein grosser.
# Gemessen auf Ihebs Rechner: ein Browser sind VIER Prozesse und 349 MB.
# Drei Browser waren mit 1,7 GB Spitze weit unter den 33,8 GB der Maschine --
# die Schranke war also zu vorsichtig, und sie war zugleich der Engpass: im
# breiten Bestand feuerte das Rendern 6,3 mal je Domain (duenne und
# JavaScript-Seiten sind dort die Regel, nicht die Ausnahme). Acht Browser
# sind knapp 3 GB und heben die Obergrenze des ganzen Laufs deutlich.
_BROWSER = threading.BoundedSemaphore(8)
_BROWSER_WARTEN = 20        # Sekunden, dann ohne Browser weiter

# Die Notbremse, nicht das Ziel. „Die ganze Website" heißt in der Praxis: bis
# hierhin. Ob sie gegriffen hat, steht in `abgeschnitten` und wandert bis in
# die Excel — eine Zahl, die an eine Grenze gestoßen ist, muss sich als solche
# zu erkennen geben, sonst liest sie sich wie ein Ergebnis.
MAX_SEITEN = 900
MAX_PROJEKTSEITEN = 600
_PAUSE = 0.20                # Höflichkeit gegenüber dem einzelnen Host
_ZEICHEN_JE_SEITE = 12000

_fortschritt = {"gesamt": 0, "fertig": 0, "projekte": 0, "spanien": 0,
                "fehler": 0, "laeuft": False, "start": None, "aktuell": None,
                "gerendert": 0, "render_uebersprungen": 0}
_lock = threading.Lock()


def stand() -> dict:
    with _lock:
        d = dict(_fortschritt)
    if d["start"] and d["fertig"]:
        weg = (dt.datetime.now() - d["start"]).total_seconds()
        d["pro_minute"] = round(d["fertig"] / max(weg / 60, 0.01), 2)
        d["rest_minuten"] = round((d["gesamt"] - d["fertig"])
                                  / max(d["fertig"] / (weg / 60), 0.01))
    return d


# --- Seitenarten -----------------------------------------------------------
_KONTAKT_WORT = ("kontakt", "contact", "contacto", "contatti", "impressum",
                 "aviso-legal", "legal", "imprint", "standort", "office",
                 "oficina", "buero", "büro", "locations", "ansprechpartner")
_TEAM_WORT = ("team", "equipo", "people", "menschen", "mitarbeiter", "studio",
              "about", "ueber-uns", "über-uns", "quienes", "nosotros", "wir")

# Spanische Postleitzahl: 01000–52999. Bewusst mit Wortgrenzen und ohne
# Jahreszahlen-Falle (2024 ist keine PLZ, weil 20 eine Provinz ist — deshalb
# muss ein spanischer Ortsname oder „España" in der Nähe stehen).
_ES_PLZ = re.compile(r"\b(0[1-9]|[1-4]\d|5[0-2])\d{3}\b")
_ES_VORWAHL = re.compile(r"\+\s?34[\s\-/.]?\d")
_ES_WORT = re.compile(r"\b(espa[nñ]a|spanien|spain|espagne|spagna)\b", re.I)


# Archiv- und Kategorieseiten. Sie bestehen den Pfadtest, weil "project" darin
# vorkommt -- cruzyortiz.com fuehrt /project-year/1999-en und
# /project-category/museums-galleries-en. Das sind KATALOGE: eine Seite, viele
# Projekte, viele Orte. Fuenf von vierzehn Stichproben kamen daher.
_INDEX_WORT = ("project-year", "project-category", "projekt-kategorie",
               "/category/", "/categoria/", "/kategorie/", "/tag/", "/etiqueta/",
               "/year/", "/jahr/", "/ano/", "/archive", "/archiv", "/archivo",
               "/page/", "/seite/", "/author/", "/autor/", "/search",
               "/project-type", "/project-status", "/proyecto-tipo")
_NUR_JAHR = re.compile(r"^(19|20)\d\d( archivos?| archive)?$", re.I)


def _ist_archivseite(url: str, titel: str = "") -> bool:
    pfad = unquote(urlsplit(url).path).lower()
    if any(w in pfad for w in _INDEX_WORT):
        return True
    return bool(_NUR_JAHR.match((titel or "").strip()))


def _art(url: str) -> str:
    pfad = unquote(urlsplit(url).path).lower()
    if _ist_archivseite(url):
        return "sonstige"
    if _ist_projekt_pfad(url):
        return "projekt"
    if any(w in pfad for w in _KONTAKT_WORT):
        return "kontakt"
    if any(w in pfad for w in _TEAM_WORT):
        return "team"
    return "sonstige"


def _titel(html: str) -> str:
    """Der Projekttitel: <h1> zuerst, dann <title> ohne den Sitenamen."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", re.S | re.I)
    roh = m.group(1) if m else ""
    if not roh.strip():
        m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
        roh = m.group(1) if m else ""
    text = re.sub(r"<[^>]+>", " ", roh)
    text = re.sub(r"\s+", " ", text).strip()
    for trenner in ("|", "–", "—", " - ", "::"):
        if trenner in text:
            teile = [t.strip() for t in text.split(trenner) if t.strip()]
            if teile:
                text = max(teile, key=len)
            break
    return text[:200]


# --- Sitemap, vollständig --------------------------------------------------

def _sitemap_alles(domain: str, grenze: int = 4000) -> list[str]:
    """JEDE URL aus allen sitemaps, nicht nur die Projektseiten.

    `laenderlauf._sitemap_projektseiten` filtert schon beim Einlesen auf
    Projektpfade und deckelt bei 40 — richtig für eine Ja/Nein-Frage, falsch
    für eine Zählung. Hier wird alles genommen und erst später sortiert; ein
    Sitemap-Index wird über alle Kinder verfolgt statt über die ersten vier.
    """
    kandidaten = [f"https://{domain}/sitemap.xml", f"https://www.{domain}/sitemap.xml",
                  f"https://{domain}/sitemap_index.xml", f"https://{domain}/wp-sitemap.xml",
                  f"https://{domain}/sitemap-index.xml", f"https://{domain}/sitemap1.xml"]
    gesehen: set[str] = set()
    aus: list[str] = []

    def hole(url: str, tiefe: int = 0) -> None:
        if url in gesehen or len(aus) >= grenze or tiefe > 2:
            return
        gesehen.add(url)
        try:
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=12)
        except Exception:                                  # noqa: BLE001
            return
        if r.status_code != 200 or "<loc>" not in r.text:
            return
        locs = re.findall(r"<loc>\s*([^<\s]+?)\s*</loc>", r.text)
        if "<sitemapindex" in r.text.lower():
            for kind in locs:
                hole(kind, tiefe + 1)
            return
        for u in locs:
            if not _KEIN_INHALT.search(u) and u not in aus:
                aus.append(u)
                if len(aus) >= grenze:
                    return

    for k in kandidaten:
        hole(k)
        if aus:
            break
    return aus



# --- Der Lauf über EINE Domain --------------------------------------------

def website_lesen(domain: str, heimat: str | None = None,
                  firmenname: str | None = None) -> dict | None:
    """Die ganze Website: alle Seiten, alle Projektseiten einzeln ausgewertet.

    Rückgabe:
        {domain, seiten_gelesen, abgeschnitten, projekte: [...],
         niederlassung_es: {...} | None, kontakte: [...], fehler}

    Jede Projektzeile: {url, titel, orte_es, orte_andere, land}
    """
    from ..identity import website_source as ws

    got = _startseite(domain)
    if not got:
        return None
    heim_url = got["home_url"]
    basis = _registrierte_domain(heim_url)
    # „Holle Architekten" in Essen meldete den Ort „Holle" (Landkreis
    # Hildesheim) auf jeder seiner Projektseiten. Der eigene Name steht im Kopf
    # jeder Seite, also findet ihn der Ortsabgleich auch dort, wo er nichts
    # bedeutet. Die Wörter des eigenen Namens und der eigenen Domain zählen
    # deshalb nicht als Ortsbeleg.
    eigene_woerter = _eigene_woerter(domain, firmenname)

    # 1) Die Karte der Website: sitemap zuerst, dann Links folgen.
    bekannt: list[str] = []
    gesehen: set[str] = set()

    def merken(u: str) -> None:
        sauber = u.split("#")[0].rstrip("/") or u
        if sauber in gesehen or _KEIN_INHALT.search(sauber):
            return
        if _registrierte_domain(sauber) != basis:
            return
        gesehen.add(sauber)
        bekannt.append(sauber)

    merken(heim_url)
    for u in _sitemap_alles(domain):
        merken(u)

    # 2) Breitensuche über die Links — für Seiten, die in keiner sitemap stehen.
    #    Die Startseite ist schon geholt; ihre Links kommen gratis dazu.
    htmls: dict[str, str] = {heim_url.split("#")[0].rstrip("/"): got["home_html"]}
    for link in ws._own_links(heim_url, got["home_html"]):
        merken(link)

    seiten_gelesen = 0
    projekt_urls = sum(1 for u in bekannt if _art(u) == "projekt")
    projekte: list[dict] = []
    kontakt_text: list[str] = []
    kontakte: list[dict] = []
    warteschlange = list(bekannt)
    i = 0
    abgeschnitten = False

    while i < len(warteschlange):
        if seiten_gelesen >= MAX_SEITEN:
            abgeschnitten = True
            break
        url = warteschlange[i]
        i += 1
        art = _art(url)
        # Sonstige Seiten werden nur besucht, solange die Karte noch wächst —
        # sie tragen Links, aber selten Projekte. Projekt-, Kontakt- und
        # Teamseiten immer.
        if art == "sonstige" and seiten_gelesen > 60:
            continue
        if art == "projekt" and len(projekte) >= MAX_PROJEKTSEITEN:
            abgeschnitten = True
            continue

        html = htmls.pop(url, None)
        if html is None:
            holen = ws._fetch_url(url, timeout=12)
            if not holen:
                continue
            html = holen[0]
            time.sleep(_PAUSE)
        seiten_gelesen += 1

        # `drop_chrome` ist hier keine Feinheit, sondern der Unterschied
        # zwischen richtig und falsch. Gemessen an ab-grimm.de: die
        # Projektseite „Neubau Wohnhaus in Goldbach" (Bayern) meldete
        # „Mallorca, Andratx" — aus dem NAVIGATIONSMENÜ, das alle anderen
        # Projekte des Büros auflistet. Ohne Menü: keine spanischen Orte, nur
        # Goldbach und Aschaffenburg. Jede Projektseite hätte sonst die Orte
        # ALLER Projekte geerbt, und die Zählung wäre Unsinn gewesen.
        text = ws._page_text(html, limit=_ZEICHEN_JE_SEITE, drop_chrome=True)
        if len(text) < render.RENDER_BELOW_CHARS and art == "projekt" \
                and render.available():
            besser = _rendern(url)
            if besser and len(ws._page_text(besser, limit=_ZEICHEN_JE_SEITE,
                                            drop_chrome=True)) > len(text):
                html = besser
                text = ws._page_text(html, limit=_ZEICHEN_JE_SEITE, drop_chrome=True)

        if art == "projekt":
            projekte.append(_projekt_auswerten(url, html, text, eigene_woerter))
        elif art in ("kontakt", "team"):
            kontakt_text.append(text)
            kontakte += _kontakte_lesen(url, html, text)
        elif seiten_gelesen == 1:
            # Die Startseite: ihre Fusszeile traegt bei vielen Bueros die
            # Auslandsadressen. bfl-architekten.de fuehrt sein „Buero Valencia
            # (ES)" dort und nirgends sonst.
            kontakt_text.append(text)
            kontakte += _kontakte_lesen(url, html, text)

        # Neue Links nur von Übersichts- und Startseiten aufsammeln: eine
        # Projektdetailseite verlinkt meist nur Nachbarprojekte, die schon in
        # der Warteschlange stehen, und blaeht sie sonst um Paginierung auf.
        if art != "projekt" and len(bekannt) < MAX_SEITEN * 2:
            for link in ws._own_links(url, html):
                vorher = len(bekannt)
                merken(link)
                if len(bekannt) > vorher:
                    warteschlange.append(bekannt[-1])

    projekte = _projekte_indexseiten_raus(projekte)
    projekte = _projekte_entdoppeln(projekte)
    projekte, chrome_orte = _chrome_orte_entfernen(projekte)
    projekte, paare = _ortspaare_bereinigen(projekte)
    chrome_orte = {**chrome_orte, **paare}
    return {
        "chrome_orte": chrome_orte,
        "domain": domain,
        "seiten_gelesen": seiten_gelesen,
        # Wie viele Projektadressen die Karte kannte. Weicht die Zahl stark von
        # den gelesenen ab, hat der Host abgewiesen — und „0 Projekte" waere
        # dann eine Aussage ueber unsere Verbindung, nicht ueber das Buero.
        # Gemessen an acme.ac: 73 Projektadressen in der Sitemap, 0 gelesen,
        # weil der Host nach meinen Probelaeufen dichtmachte.
        "projekt_urls_bekannt": max(projekt_urls, len(projekte)),
        "abgeschnitten": abgeschnitten,
        "projekte": projekte,
        "niederlassung_es": _niederlassung(kontakt_text),
        "kontakte": kontakte,
    }


# Ab wie vielen Projektseiten ein Ort als Bestandteil der Seitenvorlage gilt.
# 60 % ist grosszuegig gewaehlt: ein Buero, das WIRKLICH ueberall in Mallorca
# baut, nennt den Ort auch im Titel, und Titeltreffer sind ausgenommen.
_CHROME_ANTEIL = 0.6
_CHROME_MINDEST = 5


def _chrome_orte_entfernen(projekte: list[dict]) -> tuple[list[dict], dict]:
    """Ein Ort, der auf FAST JEDER Projektseite steht, ist keine Projektadresse.

    DER FEHLER, DER DIESE REGEL ERZWUNGEN HAT.
    bfl-architekten.de meldete 169 spanische Projekte \u2014 auch \u201eDachausbau
    Berlin-K\u00f6penick" und \u201eGrundschule Berlin-Spandau". Der Grund stand im
    Seitenfu\u00df:

        B\u00fcro Valencia (ES)  E-46018 Valencia  T. +34 636508235

    big.dk dasselbe mit \u201eRonda de Sant Pere, 56 Bajos, 08010 Barcelona" \u2014 dort
    wurde zus\u00e4tzlich der STRASSENNAME \u201eRonda" als andalusische Kleinstadt
    gelesen. 268 von 268 Projekten lagen angeblich in Spanien.

    `drop_chrome` entfernt Navigationsmen\u00fcs, aber keine Fu\u00dfzeilen \u2014 und die
    Fu\u00dfzeile ist genau der Ort, an dem B\u00fcros ihre Auslandsadressen f\u00fchren.
    Statt Fu\u00dfzeilen zu erkennen (jede Seite baut sie anders) z\u00e4hlt diese Regel
    nach: was auf 60 % aller Projektseiten steht, geh\u00f6rt zur Vorlage.

    Die Adresse geht dabei NICHT verloren \u2014 im Gegenteil: sie ist der beste
    Beleg f\u00fcr eine Niederlassung und wird dort ausgewertet. Sie ist nur keine
    Projektadresse.
    """
    if not projekte:
        return projekte, {}
    zaehler: dict[str, int] = defaultdict(int)
    for p in projekte:
        for ort in p["orte_es"]:
            zaehler[ort] += 1
    grenze = max(_CHROME_MINDEST, int(len(projekte) * _CHROME_ANTEIL))
    chrome = {ort: k for ort, k in zaehler.items()
              if k >= grenze and k >= _CHROME_MINDEST}
    if not chrome:
        return projekte, {}
    for p in projekte:
        # Ein Titeltreffer bleibt: steht der Ort im Titel DIESES Projekts,
        # ist er dessen Adresse und nicht die des Buros.
        p["orte_es"] = [o for o in p["orte_es"]
                        if o not in chrome
                        or p.get("gruende", {}).get(o) == "im Projekttitel"]
        p["hat_ort"] = bool(p["orte_es"] or p["orte_andere"])
    return projekte, chrome


# Zwei Orte, die auf FAST DENSELBEN Seiten stehen, sind kein Zufall.
#
# Gemessen ueber die 82 Bueros (Jaccard der Seitenmengen):
#
#     behzadi-architekten.de   madrid  196 | barcelona 193   0,93
#     herzogdemeuron.com       tenerife 19 | santa cruz 18   0,95
#     cruzyortiz.com           seville  64 | madrid     43   0,26
#     mathes.de                mallorca 42 | ibiza       9   0,16
#
# Die ersten beiden Paare sind Artefakte, die letzten beiden echt — und die
# blosse Haeufigkeit trennt sie NICHT: behzadis Madrid steht auf 49 % der
# Seiten, Cruz y Ortiz' Sevilla auf ebenfalls 49 %. Der Unterschied ist, dass
# echte Projektorte WECHSELN und Artefakte gemeinsam auftreten.
#
# Zwei Ursachen, zwei Heilungen:
#
#   * behzadi bettet seine ganze Projektliste in jede Projektseite ein
#     ("residenz des botschafters, madrid", "hotel barcelona ... berlin" —
#     letzteres ist ein GEBAEUDENAME in Berlin). Beide Orte muessen weg.
#   * Bei Herzog sind "tenerife" und "santa cruz" derselbe Ort: Santa Cruz de
#     Tenerife, in zwei Treffer zerfallen. Die gehoeren zusammengefasst, nicht
#     geloescht — sonst faellt ein echtes Projekt heraus.
#
# Unterschieden wird ueber das Ortsverzeichnis: gibt es einen spanischen Ort,
# dessen Name BEIDE enthaelt, ist es einer. Sonst sind es zwei aus einer Liste.
_PAAR_SCHWELLE = 0.85
_PAAR_MINDEST = 5


def _gemeinsamer_ort(a: str, b: str) -> str | None:
    """Ein spanischer Ortsname, der beide Teile enthaelt — oder None."""
    from . import regionen
    for kandidat in (f"{b} de {a}", f"{a} de {b}", f"{b} {a}", f"{a} {b}"):
        if regionen._plz_index().get(kandidat):
            return kandidat
    return None


def _ortspaare_bereinigen(projekte: list[dict]) -> tuple[list[dict], dict]:
    """Orte zusammenfassen oder verwerfen, die immer gemeinsam auftreten."""
    seiten: dict[str, set] = defaultdict(set)
    for p in projekte:
        for ort in p["orte_es"]:
            seiten[ort].add(p["url"])
    gross = {o: u for o, u in seiten.items() if len(u) >= _PAAR_MINDEST}
    verwerfen: set[str] = set()
    ersetzen: dict[str, str] = {}
    namen = sorted(gross, key=lambda o: -len(gross[o]))
    for i, a in enumerate(namen):
        for b in namen[i + 1:]:
            ua, ub = gross[a], gross[b]
            if not (ua & ub):
                continue
            if len(ua & ub) / len(ua | ub) < _PAAR_SCHWELLE:
                continue
            zusammen = _gemeinsamer_ort(a, b)
            if zusammen:
                ersetzen[a] = zusammen
                ersetzen[b] = zusammen
            else:
                verwerfen.add(a)
                verwerfen.add(b)
    if not verwerfen and not ersetzen:
        return projekte, {}
    for p in projekte:
        neu_orte = []
        for o in p["orte_es"]:
            if o in verwerfen and p.get("gruende", {}).get(o) != "im Projekttitel":
                continue
            neu_orte.append(ersetzen.get(o, o))
        p["orte_es"] = sorted(set(neu_orte))
        p["hat_ort"] = bool(p["orte_es"] or p["orte_andere"])
    bericht = {o: len(seiten[o]) for o in verwerfen}
    bericht.update({f"{a} -> {b}": len(seiten[a]) for a, b in ersetzen.items()})
    return projekte, bericht


def _projekte_indexseiten_raus(projekte: list[dict]) -> list[dict]:
    """\u00dcbersichtsseiten z\u00e4hlen nicht als Projekt.

    `big.dk/projects/architecture` besteht den Pfadtest \u2014 \u201eprojects" als
    Abschnitt, ein weiterer Abschnitt dahinter \u2014 ist aber der Katalog und
    nicht ein Projekt. Erkennbar ist das strukturell: die Adresse einer
    \u00dcbersicht ist der ANFANG der Adressen ihrer Eintr\u00e4ge.
    """
    pfade = [urlsplit(p["url"]).path.rstrip("/") for p in projekte]
    aus = []
    for p, pfad in zip(projekte, pfade):
        kinder = sum(1 for anderer in pfade
                     if anderer != pfad and anderer.startswith(pfad + "/"))
        if kinder >= 3:
            continue
        aus.append(p)
    return aus


def _projekte_entdoppeln(projekte: list[dict]) -> list[dict]:
    """Dasselbe Projekt unter mehreren Adressen zaehlt einmal.

    ab-grimm.de fuehrt jedes Projekt unter `/080_port-andratx/index.htm` UND
    unter `/080_port-andratx/080_port-andratx.htm` — im Probelauf stand
    „Ausbau Ferienhaus Mallorca, Port Andratx" dreimal in der Liste. Beim
    ANTEIL faellt das nicht auf (Zaehler und Nenner wachsen mit), bei der
    absoluten Zahl schon, und die geht in die Bewertung ein.

    Zusammengefasst wird ueber die normalisierte Adresse und, wo die nichts
    hergibt, ueber Titel plus Ortsliste.
    """
    gesehen: set = set()
    aus: list[dict] = []
    for p in projekte:
        pfad = urlsplit(p["url"]).path.rstrip("/")
        pfad = re.sub(r"/(index|default|home)\.(html?|php|aspx?)$", "", pfad, flags=re.I)
        pfad = re.sub(r"\.(html?|php|aspx?)$", "", pfad, flags=re.I)
        # `/080_port-andratx/080_port-andratx` und `/080_port-andratx` sind
        # dieselbe Seite: ein wiederholter letzter Abschnitt ist Kopie.
        teile = [x for x in pfad.split("/") if x]
        if len(teile) >= 2 and teile[-1] == teile[-2]:
            teile = teile[:-1]
        schluessel = ("/".join(teile), tuple(p["orte_es"]))
        zweit = ((p["titel"] or "").strip().lower(), tuple(p["orte_es"]))
        if schluessel in gesehen or (zweit[0] and zweit in gesehen):
            continue
        gesehen.add(schluessel)
        if zweit[0]:
            gesehen.add(zweit)
        aus.append(p)
    return aus


def _rendern(url: str) -> str | None:
    """Die Seite im Browser holen — aber nur, wenn ein Browserplatz frei ist.

    Ohne diese Schranke war die Zahl der Arbeiter zugleich die Zahl der
    moeglichen Chromium-Prozesse. Das hat die Crawl-Geschwindigkeit an den
    Arbeitsspeicher gekettet, obwohl gerendert wird: selten. Jetzt sind es
    zwei getrennte Groessen — viele Arbeiter, wenige Browser.
    """
    if not render.available():
        return None
    if not _BROWSER.acquire(timeout=_BROWSER_WARTEN):
        with _lock:
            _fortschritt["render_uebersprungen"] =                 _fortschritt.get("render_uebersprungen", 0) + 1
        return None
    try:
        with _lock:
            _fortschritt["gerendert"] = _fortschritt.get("gerendert", 0) + 1
        return render.render_html(url)
    finally:
        _BROWSER.release()


def _registrierte_domain(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower().split(":")[0]
    except ValueError:
        return ""
    teile = [t for t in host.split(".") if t]
    return ".".join(teile[-2:]) if len(teile) >= 2 else host


def _eigene_woerter(domain: str, firmenname: str | None) -> set[str]:
    """Die Wörter des eigenen Namens — sie sind auf dieser Website kein Ort."""
    roh = f"{firmenname or ''} {(domain or '').rsplit('.', 1)[0]}"
    roh = re.sub(r"[^\w\s\-]", " ", roh).replace("-", " ")
    return {laender._falten(w) for w in roh.split() if len(w) >= 4}


# Ein spanischer Ortsname auf einer Projektseite braucht einen Grund, ernst
# genommen zu werden. Ohne diese Regel meldete acme.ac (London) neun spanische
# Projekte: „Canopy by Hilton, London City" mit dem Ort „Maria", „Mamsha
# Gardens" mit „Castillo" und „Javier". Das sind die Namen der Projektteams.
#
# Spanische Vor- und Nachnamen sind fast immer auch Gemeindenamen \u2014 Mar\u00eda,
# Borja, Cristóbal, Cabra, Moran, Oteiza gibt es alle als Ort. Ihre Größe
# hilft nicht weiter: die echten Projektorte Andratx, Calvià und Sitges haben
# genau eine Postleitzahl, genau wie das Rauschen.
#
# Was trennt, ist der ZUSAMMENHANG. Ein Büro, das von außen in Spanien baut,
# sagt das auf der Projektseite \u2014 im Titel, mit dem Landesnamen daneben, oder
# es ist eine Stadt, die niemand mit einem Vornamen verwechselt.
_SPANIENWORT = re.compile(
    r"\b(espa[nñ]a|spanien|spain|espagne|spagna|spanje|spansk|espanha)\b", re.I)
_INSELN = {"mallorca", "majorca", "menorca", "ibiza", "eivissa", "formentera",
           "tenerife", "gran canaria", "lanzarote", "fuerteventura", "la palma",
           "la gomera", "el hierro", "baleares", "canarias", "cataluna",
           "catalunya", "andalucia", "costa del sol", "costa brava",
           "costa blanca"}
_GROSSSTADT_PLZ = 5      # ab so vielen Postleitzahlen ist ein Ort eine Stadt

# Der Ort eines VERLAGS ist nicht der Ort des Projekts.
#
# herzogdemeuron.com fuehrt unter jedem Projekt sein Literaturverzeichnis:
#
#     In: Luis Fernandez-Galiano (Ed.). "Arquitectura Viva Proyectos."
#     Vol. No. 089, Madrid, Arquitectura Viva SL, 2018. pp. 40-47.
#     ... Vol. No. 129/130, Madrid, El Croquis, 2006.
#
# Ergebnis vor dieser Regel: 140 von 223 spanischen "Projekten" lagen in
# Madrid -- darunter "St. Jakob-Park Basel". Ernsthafte Bueros pflegen solche
# Verzeichnisse, also trifft der Fehler gerade die interessantesten Adressen.
_ZITAT_DAVOR = re.compile(
    r"(\bIn:|\(Ed\.\)|\bEd\.:|\bEds\.|\bVol\.|\bpp\.|\bISBN|\bHrsg|\bHg\.|"
    r"\bVerlag\b|\bPress\b|\bEditorial\b|\bEditions?\b|\bPublisher|\bcroquis\b|"
    r"\barquitectura viva\b|\bdomus\b)", re.I)
# "Madrid, Arquitectura Viva SL, 2018" -- Ort, Verlag, Jahr ist die Form einer
# Literaturangabe und nie die einer Bauadresse.
_ZITAT_DANACH = re.compile(r"^\s*,[^,]{2,60},\s*(19|20)\d\d\b")

# Landes- und Regionsnamen sind keine Staedte. "Espana" tauchte bei
# cruzyortiz.com 52-mal als Ort auf und wurde -- weil es eine gleichnamige
# Gemeinde gibt -- der Comunidad de Madrid zugeschlagen.
_KEIN_ORT = {"espana", "espanya", "spain", "spanien", "espagne", "spagna",
             "europa", "europe", "iberia", "peninsula"}


# Wo der Projekttext aufhoert und der Anhang anfaengt.
#
# Eine Projektseite von herzogdemeuron.com hat drei Teile:
#
#     0    - 4000   das Projekt selbst
#     4078 - 6748   das Literaturverzeichnis
#     7000 - 7988   "weitere Projekte" mit Titeln und Orten
#
# Der Ort des Projekts steht im ersten Teil. Die beiden anderen tragen die
# Orte von VERLAGEN und von ANDEREN Projekten -- so wurde "St. Jakob-Park
# Basel" zu einem Projekt in Barcelona (aus "313 nou camp nou barcelona,
# spain" in der Verwandtenliste) und zu einem in Madrid (aus "vol. no. 89,
# madrid, arquitectura viva").
#
# Beide Anhaenge auf einmal loswerden: ab dem ersten Zitatmarker wird nicht
# mehr gelesen. Die Verwandtenliste steht immer dahinter.
_ANHANG = re.compile(
    r"(\b(?:in:|vol\.|pp\.|isbn|eds?\.[:.]|hrsg|verlag)"
    r"|weitere projekte|aehnliche projekte|ähnliche projekte|related projects"
    r"|more projects|otros proyectos|proyectos relacionados|verwandte projekte)",
    re.I)
_ANHANG_FRUEHESTENS = 800     # so viel Text bleibt in jedem Fall stehen


def ohne_anhang(text_gef: str) -> str:
    """Den Projekttext ohne Literaturverzeichnis und Verwandtenliste.

    Schneidet am ersten Zitat- oder Verwandten-Marker, aber nie vor Zeichen
    800 -- eine kurze Seite, die frueh zitiert, soll nicht auf einen Satz
    zusammenschrumpfen.
    """
    m = _ANHANG.search(text_gef, _ANHANG_FRUEHESTENS)
    return text_gef[:m.start()] if m else text_gef


def _ist_literaturangabe(text_gef: str, start: int, ende: int) -> bool:
    """Steht dieser Ortstreffer in einer Literaturangabe?"""
    davor = text_gef[max(0, start - 120):start]
    danach = text_gef[ende:ende + 70]
    return bool(_ZITAT_DAVOR.search(davor) or _ZITAT_DANACH.match(danach))


def _ort_belegt(name: str, titel_gef: str, text_gef: str,
                gewicht: int) -> str | None:
    """Warum dieser Ort zählt \u2014 oder None, wenn er nicht zählt.

    Der Grund wandert als Text bis in die Excel. Ein Ort, der ohne
    nachvollziehbaren Grund in einer Liste steht, ist genau das, was diesen
    Datenbestand schon zweimal verdorben hat.
    """
    if name in _KEIN_ORT:
        return None
    if name in titel_gef:
        return "im Projekttitel"

    # Jedes Vorkommen im Text durchgehen und Literaturangaben ueberspringen.
    # Steht der Ort NUR in Zitaten, ist er der Sitz eines Verlags.
    stellen = list(re.finditer(re.escape(name), text_gef))
    echte = [m for m in stellen
             if not _ist_literaturangabe(text_gef, m.start(), m.end())]
    if stellen and not echte:
        return None

    if name in _INSELN:
        return "Insel oder Region"
    if gewicht >= _GROSSSTADT_PLZ:
        return f"Stadt ({gewicht} PLZ)"
    # "Marbella, Spanien" -- der Landesname in Sichtweite macht aus dem Namen
    # einen Ort. 60 Zeichen sind eine Zeile Adresse, nicht mehr.
    for m in echte:
        umfeld = text_gef[max(0, m.start() - 60):m.end() + 60]
        if _SPANIENWORT.search(umfeld):
            return "neben dem Landesnamen"
    return None


# WO AUF EINER PROJEKTSEITE DER PROJEKTORT STEHT.
#
# Bis hierher wurde die ganze Seite nach Ortsnamen durchsucht. Eine Stichprobe
# von 14 Projektzeilen hat gezeigt, was das taugt: 2 richtig, 12 falsch. Die
# falschen kamen aus Literaturverzeichnissen, Verwandtenlisten, eingebetteten
# Projektlisten und aus Werbeprosa -- "Vienna coffee-house meets Barcelona"
# machte ein Wiener Lokal zu einem Projekt in Barcelona.
#
# Der Ort steht in Wirklichkeit fast immer an einer von drei Stellen:
#
#   1. in einem BESCHRIFTETEN Feld    "Location City: Madrid, Spain"
#   2. im TITEL                        "Deutsche Schule Madrid"
#   3. im KOPF des Projektblocks       "226 National Stadium
#                                       Main Stadium for the 2008 Olympic
#                                       Games / Beijing, China / 2002-2008"
#
# Der Titel wiederholt sich am Anfang des Inhaltsblocks -- er dient deshalb als
# Anker fuer Fall 3. Gemessen an denselben 14 Seiten: 8 von 8 entscheidbaren
# richtig, einschliesslich aller vier, die NICHT spanisch sein durften.
_ORTSLABEL = re.compile(
    r"\b(?:location|ort|standort|ubicaci[oó]n|localizaci[oó]n|lugar"
    r"|place|site|adresse|address|land|country|stadt|city|pa[ií]s|projektort"
    r"|bauort|emplacement|luogo)\b[ ]*(?:city)?[ ]*[:\-–][ ]*",
    re.I)
_ZONE_LAENGE = 420
_ZONE_LABEL_LAENGE = 90


def ortszone(titel: str, text: str) -> str:
    """Der Ausschnitt, in dem der Projektort stehen kann.

    Beschriftete Felder gewinnen; sonst der Bereich hinter dem letzten
    Vorkommen des Titels; sonst der Anfang. Der Anhang (Literatur, verwandte
    Projekte) wird auch hier abgeschnitten -- bei mathes.de stand
    "weitere projekte ... villa, mallorca" direkt hinter einem Ibiza-Projekt.
    """
    felder = [text[m.end():m.end() + _ZONE_LABEL_LAENGE]
              for m in _ORTSLABEL.finditer(text)]
    if felder:
        return " ".join(felder[:6])
    anker = (titel or "")[:28].strip()
    roh = text
    if anker:
        pos = text.rfind(anker)
        if pos >= 0:
            roh = text[pos:pos + _ZONE_LAENGE]
        else:
            roh = text[:_ZONE_LAENGE]
    else:
        roh = text[:_ZONE_LAENGE]
    m = _ANHANG.search(roh, 40)
    return roh[:m.start()] if m else roh


def _projekt_auswerten(url: str, html: str, text: str,
                       eigene_woerter: set[str]) -> dict:
    """Eine Projektseite: Titel, spanische Orte, Orte anderswo.

    Der Ort wird über denselben Abgleich gefunden wie im Länderlauf
    (`laender._ort_treffer`), nur auf EINER Seite statt auf dem ganzen
    Textklumpen. Dadurch lässt sich das Projekt einem Ort zuordnen \u2014 genau
    das, was der Klumpen nicht kann.

    Der Titel zählt doppelt: „Casa en Cadaqués" trägt den Ort im Titel, und
    dort ist er verlässlicher als im Fließtext, in dem auch Bürostandorte,
    Auszeichnungen und Teamlisten stehen.
    """
    titel = _titel(html)
    # NUR die Ortszone, nicht die ganze Seite. Siehe den Kommentar oben:
    # die ganze Seite lieferte 2 von 14 richtig.
    text = ortszone(titel, ohne_anhang(text))
    volltext = f"{titel}\n{text}"
    treffer = laender._ort_treffer(volltext)
    titel_gef = laender._falten(titel)
    text_gef = laender._falten(volltext)

    orte_es, gruende = [], {}
    for name, laendermap in treffer.get("ES", []):
        if name in eigene_woerter:
            continue
        grund = _ort_belegt(name, titel_gef, text_gef, laendermap.get("ES", 0))
        if grund:
            orte_es.append(name)
            gruende[name] = grund
    orte_es = sorted(set(orte_es))

    # Für die ANDEREN Länder bleibt es beim rohen Treffer: sie zählen nur in
    # den Nenner („dieses Projekt hat einen erkennbaren Ort"), und dort schadet
    # ein zu großzügiger Treffer weniger, als ein zu strenger den Anteil
    # verfälschen würde.
    andere = {land: sorted({n for n, _ in v} - eigene_woerter)
              for land, v in treffer.items() if land != "ES"}
    andere = {k: v for k, v in andere.items() if v}
    return {"url": url, "titel": titel, "orte_es": orte_es,
            "orte_andere": andere, "gruende": gruende,
            "hat_ort": bool(orte_es or andere)}


def _niederlassung(texte: list[str]) -> dict | None:
    """Hat das Büro eine zweite Adresse in Spanien?

    ERSTER ANLAUF WAR FALSCH, und zwar auf eine lehrreiche Art: „zwei von drei
    Belegen" — spanische PLZ, +34, das Wort Spanien — meldete bei
    holle-architekten.de eine Niederlassung. Die Belege waren „45133 Essen"
    und das Wort „Spanien" irgendwo im Text. Der deutsche
    Postleitzahlenbereich liegt fast vollständig über dem spanischen
    (01000–52999), also ist eine fünfstellige Zahl auf einer deutschen Seite
    so gut wie nie ein Beleg. Und „Spanien" steht auf jeder Seite eines
    Büros, das in Spanien baut.

    Jetzt zählt nur, was mit Spanien nicht zu verwechseln ist:

      * die Vorwahl +34, oder
      * eine PLZ, die es in Spanien gibt UND deren spanischer Ortsname in
        derselben Zeile steht.

    Das Wort „España" allein reicht nicht mehr — es bestätigt nur.
    """
    text = "\n".join(texte)
    if not text.strip():
        return None
    vorwahl = _ES_VORWAHL.search(text)
    adresse = _plz_mit_ort(text)
    if not (vorwahl or adresse):
        return None
    zeile = None
    for kandidat in text.splitlines():
        if (adresse and adresse[0] in kandidat) or \
                (vorwahl and _ES_VORWAHL.search(kandidat)):
            zeile = kandidat.strip()[:220]
            break
    return {"vorwahl_34": bool(vorwahl),
            "plz_mit_ort": f"{adresse[0]} {adresse[1]}" if adresse else None,
            "wort_spanien": bool(_ES_WORT.search(text)),
            "zeile": zeile}


def _plz_mit_ort(text: str) -> tuple[str, str] | None:
    """Eine fünfstellige Zahl, die als spanische PLZ existiert und deren Ort
    daneben steht. Beides zusammen ist belastbar; einzeln nicht."""
    from sqlalchemy import bindparam
    kandidaten = {m.group(0) for m in re.finditer(r"\b\d{5}\b", text)}
    kandidaten = {k for k in kandidaten if _ES_PLZ.fullmatch(k)}
    if not kandidaten:
        return None
    with SessionLocal() as s:
        treffer = s.execute(
            _sql("SELECT plz, place FROM plz_geo WHERE country='ES' AND plz IN :p")
            .bindparams(bindparam("p", expanding=True)),
            {"p": sorted(kandidaten)}).all()
    for plz, ort in treffer:
        umfeld = re.search(rf"{re.escape(plz)}.{{0,60}}", text, re.S)
        if umfeld and laender._falten(ort) in laender._falten(umfeld.group(0)):
            return plz, ort
    return None


_MAILTO = re.compile(r'mailto:([^"\'?>\s]+)', re.I)
_NAME_DAVOR = re.compile(
    r"([A-ZÄÖÜÁÉÍÓÚÑ][\w'’\-]+(?:\s+[A-ZÄÖÜÁÉÍÓÚÑ][\w'’\-]+){0,2})\s*$")


def _kontakte_lesen(url: str, html: str, text: str) -> list[dict]:
    """`mailto:`-Adressen der Seite, mit dem Namen davor, wenn einer dasteht.

    PERSONENBEZOGEN. Das Ergebnis geht in eine eigene Tabelle und in ein
    eigenes Blatt der Excel, das gelöscht werden kann. `spanien_bezug` sagt,
    ob auf DIESER Seite Spanien vorkommt — nur so wird aus einer beliebigen
    Adresse ein „Ansprechpartner für Spanien".
    """
    spanien = bool(_ES_WORT.search(text) or _ES_VORWAHL.search(text))
    aus: list[dict] = []
    for m in _MAILTO.finditer(html or ""):
        adresse = m.group(1).strip().lower()
        if "@" not in adresse or len(adresse) > 120:
            continue
        umfeld = re.sub(r"<[^>]+>", " ", (html or "")[max(0, m.start() - 400):m.start()])
        umfeld = re.sub(r"\s+", " ", umfeld)
        treffer = _NAME_DAVOR.search(umfeld)
        aus.append({"url": url, "email": adresse,
                    "name": treffer.group(1) if treffer else None,
                    "spanien_bezug": spanien})
    # je Adresse einmal, die mit Namen bevorzugt
    beste: dict[str, dict] = {}
    for k in aus:
        alt = beste.get(k["email"])
        if alt is None or (k["name"] and not alt["name"]):
            beste[k["email"]] = k
    return list(beste.values())[:40]


# --- Speichern -------------------------------------------------------------

def _tabellen(s) -> None:
    s.execute(_sql("""
        CREATE TABLE IF NOT EXISTS arch_web_scan (
            domain TEXT PRIMARY KEY, company_ids TEXT, seiten_gelesen INTEGER,
            abgeschnitten INTEGER, projekte_gesamt INTEGER, projekte_mit_ort INTEGER,
            projekte_es INTEGER, niederlassung_es TEXT, gescannt_am TEXT,
            projekt_urls_bekannt INTEGER, chrome_orte TEXT, fehler TEXT)"""))
    s.execute(_sql("""
        CREATE TABLE IF NOT EXISTS arch_web_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, url TEXT,
            titel TEXT, ort TEXT, provinz TEXT, region TEXT, region_de TEXT,
            region_eindeutig INTEGER, land TEXT, beleg TEXT, gescannt_am TEXT)"""))
    s.execute(_sql("""
        CREATE TABLE IF NOT EXISTS arch_web_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, url TEXT,
            email TEXT, name TEXT, spanien_bezug INTEGER, gescannt_am TEXT)"""))
    s.execute(_sql("CREATE INDEX IF NOT EXISTS ix_awp_domain ON arch_web_projects(domain)"))
    s.execute(_sql("CREATE INDEX IF NOT EXISTS ix_awc_domain ON arch_web_contacts(domain)"))


def _speichern(dom: str, ids: list[int], res: dict | None, fehler: str | None) -> None:
    import json
    jetzt = dt.datetime.now().isoformat(timespec="seconds")
    with SessionLocal() as s:
        _tabellen(s)
        for tab in ("arch_web_projects", "arch_web_contacts"):
            s.execute(_sql(f"DELETE FROM {tab} WHERE domain = :d"), {"d": dom})
        projekte = (res or {}).get("projekte") or []
        mit_ort = [p for p in projekte if p["hat_ort"]]
        es = [p for p in projekte if p["orte_es"]]
        for p in projekte:
            if p["orte_es"]:
                for ort in p["orte_es"]:
                    r = regionen.einordnen(ort)
                    s.execute(_sql(
                        "INSERT INTO arch_web_projects (domain, url, titel, ort, "
                        "provinz, region, region_de, region_eindeutig, land, "
                        "beleg, gescannt_am) "
                        "VALUES (:d,:u,:t,:o,:p,:r,:rd,:e,'ES',:b,:z)"),
                        {"d": dom, "u": p["url"], "t": p["titel"], "o": ort,
                         "p": r["provinz"], "r": r["region"], "rd": r["region_de"],
                         "e": None if r["eindeutig"] is None else int(r["eindeutig"]),
                         "b": (p.get("gruende") or {}).get(ort), "z": jetzt})
            elif p["orte_andere"]:
                land = sorted(p["orte_andere"])[0]
                s.execute(_sql(
                    "INSERT INTO arch_web_projects (domain, url, titel, ort, land, "
                    "gescannt_am) VALUES (:d,:u,:t,:o,:l,:z)"),
                    {"d": dom, "u": p["url"], "t": p["titel"],
                     "o": (p["orte_andere"][land] or [None])[0], "l": land, "z": jetzt})
        for k in (res or {}).get("kontakte") or []:
            s.execute(_sql(
                "INSERT INTO arch_web_contacts (domain, url, email, name, "
                "spanien_bezug, gescannt_am) VALUES (:d,:u,:e,:n,:s,:z)"),
                {"d": dom, "u": k["url"], "e": k["email"], "n": k["name"],
                 "s": int(bool(k["spanien_bezug"])), "z": jetzt})
        s.execute(_sql("""
            INSERT INTO arch_web_scan (domain, company_ids, seiten_gelesen,
                abgeschnitten, projekte_gesamt, projekte_mit_ort, projekte_es,
                niederlassung_es, gescannt_am, projekt_urls_bekannt,
                chrome_orte, fehler)
            VALUES (:d,:c,:s,:a,:pg,:pm,:pe,:n,:z,:pu,:co,:f)
            ON CONFLICT(domain) DO UPDATE SET company_ids=:c, seiten_gelesen=:s,
                abgeschnitten=:a, projekte_gesamt=:pg, projekte_mit_ort=:pm,
                projekte_es=:pe, niederlassung_es=:n, gescannt_am=:z,
                projekt_urls_bekannt=:pu, chrome_orte=:co, fehler=:f"""),
            {"d": dom, "c": json.dumps(ids), "s": (res or {}).get("seiten_gelesen", 0),
             "a": int(bool((res or {}).get("abgeschnitten"))),
             "pg": len(projekte), "pm": len(mit_ort), "pe": len(es),
             "n": json.dumps((res or {}).get("niederlassung_es"), ensure_ascii=False),
             "pu": (res or {}).get("projekt_urls_bekannt", 0),
             "co": json.dumps((res or {}).get("chrome_orte") or {},
                              ensure_ascii=False),
             "z": jetzt, "f": fehler})
        s.commit()


# --- Der Lauf --------------------------------------------------------------

def grundgesamtheit(nur_spanien_aktiv: bool = True,
                    ohne_spanische: bool = True) -> dict[str, list]:
    """Domain → [(company_id, heimatland)].

    `nur_spanien_aktiv`: nur Büros, bei denen der Länderlauf Spanien gefunden
    hat — die 82 für den ersten Durchgang. Für den zweiten Durchgang auf False,
    dann kommen alle europäischen Nicht-Spanier in den Topf.
    """
    wo = ["segment='Architekten'", "sub_segment='Architekturbüro'",
          "duplicate_of IS NULL", "website_domain IS NOT NULL", "website_domain <> ''"]
    if ohne_spanische:
        wo.append("country <> 'ES'")
        wo.append("country IN ('DE','AT','CH','NL','BE','LU','FR','IT','PT','GB',"
                  "'IE','DK','SE','NO','FI','PL','CZ','SK','HU','RO','HR','SI',"
                  "'GR','LI','EE','LV','LT','BG','RS','MT','CY','IS')")
    if nur_spanien_aktiv:
        wo.append("active_cities IS NOT NULL AND active_cities <> '{}' "
                  "AND active_cities LIKE '%\"ES\"%'")
    nach: dict[str, list] = defaultdict(list)
    with SessionLocal() as s:
        for cid, dom, land in s.execute(_sql(
                f"SELECT id, website_domain, country FROM companies "
                f"WHERE {' AND '.join(wo)}")):
            nach[(dom or "").strip().lower()].append((cid, land))
    return dict(nach)


def lauf(nur_spanien_aktiv: bool = True, ohne_spanische: bool = True,
         limit: int | None = None, arbeiter: int = ARBEITER,
         neu: bool = False) -> dict:
    nach_domain = grundgesamtheit(nur_spanien_aktiv, ohne_spanische)
    domains = sorted(nach_domain)
    with SessionLocal() as s:
        namen = {(r[0] or "").strip().lower(): r[1] for r in s.execute(_sql(
            "SELECT website_domain, name FROM companies WHERE website_domain <> ''"))}
    if not neu:
        with SessionLocal() as s:
            _tabellen(s)
            fertig = {r[0] for r in s.execute(_sql(
                "SELECT domain FROM arch_web_scan WHERE fehler IS NULL"))}
        domains = [d for d in domains if d not in fertig]
    if limit:
        domains = domains[:limit]

    with _lock:
        _fortschritt.update({"gesamt": len(domains), "fertig": 0, "projekte": 0,
                             "spanien": 0, "fehler": 0, "laeuft": True,
                             "start": dt.datetime.now(), "aktuell": None})
    logger.info("Tiefenlauf startet: %d Domains, %d Arbeiter", len(domains), arbeiter)

    def eine(dom: str):
        laender_der_zeilen = [l for _, l in nach_domain[dom] if l]
        heimat = max(set(laender_der_zeilen), key=laender_der_zeilen.count) \
            if laender_der_zeilen else None
        try:
            res = website_lesen(dom, heimat=heimat, firmenname=namen.get(dom))
        except Exception as e:                              # noqa: BLE001
            return dom, None, f"{type(e).__name__}: {e}"[:200]
        if res is None:
            return dom, None, "nicht erreichbar"
        return dom, res, None

    with ThreadPoolExecutor(max_workers=arbeiter) as pool:
        futures = {pool.submit(eine, d): d for d in domains}
        for fut in as_completed(futures):
            dom, res, fehler = fut.result()
            ids = [cid for cid, _ in nach_domain[dom]]
            try:
                _speichern(dom, ids, res, fehler)
            except Exception as e:                          # noqa: BLE001
                logger.warning("speichern %s: %s", dom, e)
            with _lock:
                _fortschritt["fertig"] += 1
                _fortschritt["aktuell"] = dom
                if fehler:
                    _fortschritt["fehler"] += 1
                elif res:
                    _fortschritt["projekte"] += len(res["projekte"])
                    _fortschritt["spanien"] += sum(1 for p in res["projekte"]
                                                   if p["orte_es"])

    with _lock:
        _fortschritt["laeuft"] = False
        aus = dict(_fortschritt)
    logger.info("Tiefenlauf fertig: %s", aus)
    return aus
