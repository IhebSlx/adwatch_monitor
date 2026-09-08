"""Wo ist dieses Büro tätig? — Länder aus Website-Text, ohne API und ohne Modell.

WARUM DETERMINISTISCH UND NICHT PER LLM.
Die Frage „in welchen Ländern baut dieses Büro" ist Mustererkennung, keine
Beurteilung. Eine Telefonvorwahl +34 ist Spanien, „Marbella" ist Spanien, und
kein Sprachmodell weiß das besser als eine Tabelle. Ein Modell kostet dagegen
Geld je Büro, ist bei 10.576 Büros nicht umsonst zu wiederholen, und sein
Ergebnis lässt sich nicht nachrechnen. Diese Datei kostet nichts, läuft beliebig
oft neu und legt zu jedem Land offen, WORAN es erkannt wurde.

Das Modell bleibt für das, was wirklich Urteil braucht (`decision_role`,
`solarlux_relevance`) — und dann nur noch auf der schon eingegrenzten Liste.

VIER SIGNALE, NACH VERLÄSSLICHKEIT GEORDNET.

  1. Telefonvorwahl  +34 -> ES. Das stärkste Signal überhaupt: eine Vorwahl ist
     eindeutig, und Büros schreiben ihre Nummer auf jede Kontaktseite.
  2. Ländername im Klartext  „España", „Spain", „Spanien", „Espagne".
  3. Ortsname gegen `plz_geo`  70.701 Namen über 18 Länder, schon in der
     Datenbank (dort für die PLZ-Geokodierung geladen, hier rückwärts benutzt).
  4. Top-Level-Domain  `.es` — nur als Stichentscheid, nie allein.

DIE FALLE BEI ORTSNAMEN, GEMESSEN STATT VERMUTET.
`plz_geo` führt Portugal mit 197.772 Zeilen (63 % der Tabelle), weil portugiesische
Postleitzahlen straßenfein sind. Dadurch stehen dort Weiler namens `Real` (107),
`Campo` (255), `Santa`, `Monte`, `Rio` — allesamt auch gewöhnliche Wörter. Wer
stumpf jeden Ortsnamen sucht, findet in jedem spanischen Text „Portugal", weil
irgendwo „campo" steht.

Vier Regeln dagegen, alle billig:
  * Mindestlänge 5 — schneidet 1.765 der 70.701 Namen weg, fast alles Weiler.
  * Der Treffer muss im Quelltext GROSSGESCHRIEBEN stehen. „Real Madrid" ja,
    „un proyecto real" nein. Das ist der wirksamste Filter, weil Ortsnamen im
    Fließtext immer groß stehen und Adjektive fast nie.
  * Eine Stoppliste gewöhnlicher Wörter, die zugleich Orte sind.
  * Bei Mehrdeutigkeit entscheidet nicht der Zufall, sondern der Reihe nach:
    Vorwahl -> Ländername auf derselben Seite -> die meisten Postleitzahlen
    -> sonst wird BEIDES vermerkt und als unsicher markiert. Ein stiller
    Münzwurf ist genau der Fehler, der schon ein dänisches Projekt nach
    Österreich gepinnt hat.
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict

from sqlalchemy import text as _sql

from ..db import SessionLocal

# --- 1. Telefonvorwahlen ---------------------------------------------------
# Nur Europa (Ihebs Vorgabe 2026-09-07). Längere Vorwahlen zuerst prüfen, sonst
# schluckt +3 5 nie +351: die Reihenfolge macht hier den Unterschied zwischen
# Portugal und Finnland.
LAND_VORWAHL: dict[str, str] = {
    "+350": "GI", "+351": "PT", "+352": "LU", "+353": "IE", "+354": "IS",
    "+355": "AL", "+356": "MT", "+357": "CY", "+358": "FI", "+359": "BG",
    "+370": "LT", "+371": "LV", "+372": "EE", "+373": "MD", "+374": "AM",
    "+375": "BY", "+376": "AD", "+377": "MC", "+378": "SM", "+380": "UA",
    "+381": "RS", "+382": "ME", "+383": "XK", "+385": "HR", "+386": "SI",
    "+387": "BA", "+389": "MK", "+420": "CZ", "+421": "SK", "+423": "LI",
    "+30": "GR", "+31": "NL", "+32": "BE", "+33": "FR", "+34": "ES",
    "+36": "HU", "+39": "IT", "+40": "RO", "+41": "CH", "+43": "AT",
    "+44": "GB", "+45": "DK", "+46": "SE", "+47": "NO", "+48": "PL",
    "+49": "DE",
}
_VORWAHLEN = sorted(LAND_VORWAHL, key=len, reverse=True)

# --- 2. Ländernamen im Klartext -------------------------------------------
# In den Sprachen, in denen die Websites tatsächlich geschrieben sind. Klein
# geschrieben und akzentfrei — normalisiert wird beim Vergleich.
LAND_NAMEN: dict[str, str] = {}
_NAMEN_ROH = {
    "ES": ("spanien", "spain", "espana", "espagne", "spagna", "espanha",
           "espanya", "spanje"),
    "PT": ("portugal", "portugual"),
    "DE": ("deutschland", "germany", "allemagne", "alemania", "germania",
           "duitsland", "tyskland", "alemanha"),
    "AT": ("osterreich", "austria", "autriche", "oostenrijk"),
    "CH": ("schweiz", "switzerland", "suisse", "suiza", "svizzera", "zwitserland"),
    "FR": ("frankreich", "france", "francia", "frankrijk", "franca"),
    "IT": ("italien", "italy", "italia", "italie", "italie"),
    "NL": ("niederlande", "netherlands", "nederland", "pays-bas", "holanda",
           "holland"),
    "BE": ("belgien", "belgium", "belgie", "belgique", "belgica"),
    "GB": ("grossbritannien", "great britain", "united kingdom", "england",
           "schottland", "scotland", "wales", "reino unido", "royaume-uni"),
    "IE": ("irland", "ireland", "irlanda"),
    "DK": ("danemark", "denmark", "danmark", "dinamarca"),
    "SE": ("schweden", "sweden", "sverige", "suecia", "suede"),
    "NO": ("norwegen", "norway", "norge", "noruega"),
    "FI": ("finnland", "finland", "suomi", "finlandia"),
    "PL": ("polen", "poland", "polska", "polonia"),
    "CZ": ("tschechien", "czech republic", "czechia", "cesko"),
    "LU": ("luxemburg", "luxembourg", "luxemburgo"),
    "GR": ("griechenland", "greece", "grecia", "ellada"),
    "HR": ("kroatien", "croatia", "hrvatska", "croacia"),
    "SI": ("slowenien", "slovenia", "slovenija"),
    "HU": ("ungarn", "hungary", "magyarorszag", "hungria"),
    "RO": ("rumanien", "romania", "rumania"),
    "MT": ("malta",),
    "MC": ("monaco",),
    "AD": ("andorra",),
}
for _land, _woerter in _NAMEN_ROH.items():
    for _w in _woerter:
        LAND_NAMEN[_w] = _land

# --- 3. Stoppliste ---------------------------------------------------------
# Gewöhnliche Wörter, die in `plz_geo` zugleich Ortsnamen sind — überwiegend
# portugiesische und spanische Weiler. Ohne diese Liste findet jeder spanische
# Text „Portugal", weil irgendwo „Campo" oder „Santa" steht.
_STOPP = {
    "real", "campo", "santa", "santo", "monte", "villa", "centro", "plaza",
    "calle", "norte", "porto", "praia", "ponte", "fonte", "torre", "cruz",
    "costa", "valle", "vista", "playa", "mundo", "grande", "nueva", "nuevo",
    "novo", "nova", "vieja", "viejo", "alta", "alto", "baja", "bajo",
    "stadt", "markt", "kirche", "berge", "insel", "mitte", "nieuw", "noord",
    "oost", "west", "zuid", "groot", "klein", "haus", "hause", "hotel",
    "atelier", "studio", "design", "projekt", "projekte", "office", "living",
    "garden", "park", "forum", "campus", "arena", "plan", "forma", "linea",
    "media", "mode", "arte", "obra", "casas", "lofts", "resort",
    # Englisch — auf britischen und internationalen Seiten. Alle winzig
    # (`street` GB=1, `stone` GB=1, `banks` GB=2), also fingen sie schon über
    # das Ortsgewicht kaum Punkte; hier stehen sie, weil sie in JEDER Adresse
    # vorkommen und als Beleg nur Rauschen wären.
    "street", "stone", "banks", "bridge", "church", "market",
    # Deutsch — die im Probelauf tatsächlich aufgetretenen Fehlalarme.
    "bauen", "fragen", "diesen", "planen", "wohnen", "leben", "sehen",
    # --- empirisch gefunden, nicht geraten -------------------------------
    # Iheb wollte die spanischen STÄDTE je Büro sehen. Fürs Land waren diese
    # Wörter harmlos (die Stark-Beleg-Regel fing sie ab), als Ortsliste sind
    # sie Unsinn. Gefunden durch die Frage: welches „Ortswort" taucht bei
    # Büros aus SECHS ODER MEHR verschiedenen Ländern auf? Echte Orte hängen
    # an einem Land, Alltagswörter überall. Die Liste trennte sauber in
    # bekannte Großstädte (Berlin 12, London 11, Barcelona 10 — die bleiben)
    # und diese hier:
    "march", "enter", "manage", "guide", "change", "court", "valley",
    "america", "opera", "hospital", "canal", "areal", "gross", "bosch",
    "campus", "atrium", "central", "terminal", "quartier", "carre",
    # aus der Sonde mit Staedteliste, 2026-09-08: portugiesische und
    # katalanische Alltagswoerter, die als Weiler gefuehrt werden
    "escola", "termas", "piloto", "ancora", "barrio", "estacao", "quinta",
    "moinho", "fabrica", "cidade", "aldeia", "serra", "outeiro", "varzea",
}

# Vornamen, die zugleich Ortsnamen sind. Auf Landesebene fangen sie sich an der
# Stark-Beleg-Regel; in einer STÄDTELISTE stünden sie mitten drin. Gemessen an
# den 317 Spanien-Treffern: `maria` bei 10 Büros, `manuel` 10, `garcia` 8,
# `javier` 7, `jesus` 6 — allesamt von Team- und Impressumsseiten.
#
# Eine Namensliste wird nie vollständig, taugt hier aber, weil sie nur die
# ANZEIGE der Städte säubert und nichts an der Länderentscheidung ändert.
_VORNAMEN = {
    # spanisch
    "maria", "manuel", "garcia", "javier", "jesus", "carmen", "pilar", "rosa",
    "carlos", "antonio", "miguel", "pablo", "elena", "laura", "marta", "david",
    "angel", "cristina", "lucia", "alba", "nuria", "gloria", "irene", "olga",
    "paula", "rocio", "silvia", "sonia", "victoria", "belen", "pilar",
    "fernando", "ramon", "sergio", "alvaro", "andres", "ignacio", "rafael",
    "esteban", "lorenzo", "vicente", "salvador", "domingo", "moreno",
    # deutsch / niederländisch / italienisch / französisch
    "albert", "klaus", "petra", "felix", "roman", "teresa", "isabel", "vera",
    "martin", "thomas", "walter", "werner", "hermann", "wilhelm", "ludwig",
    "anton", "bruno", "arnold", "otto", "emil", "hugo", "oskar", "kurt",
    "sara", "julia", "clara", "eva", "anna", "lena", "nora", "ida",
}


# Ab dieser Länge wird ein Ortsname überhaupt gesucht. 1.765 der 70.701 Namen
# sind kürzer — fast durchweg Weiler, deren Namen zugleich Alltagswörter sind.
_MIN_LAENGE = 5

# Anteil der Orte eines Landes, die als „groß" gelten. Die Schranke wird daraus
# JE LAND aus den Daten berechnet (siehe `_grossstadt_schwelle`), nicht fest
# gesetzt.
#
# WARUM NICHT EINE FESTE ZAHL, gemessen an plz_geo:
#
#            Orte   Median    p90    p99      max
#     DE    8.676        1      1      6      181
#     ES    9.922        1      1      4       63
#     PT   17.787        1     18    176    9.165
#     SE    1.780        3     21    126    1.139
#
# Eine feste Grenze von 10 Postleitzahlen heißt in Deutschland „Großstadt"
# (über dem 99. Perzentil) und in Portugal „unterdurchschnittlich" — dort sind
# die Postleitzahlen straßenfein. Genau daran ging `gernotschulzarchitektur.de`
# als „sicher in Portugal" durch: Belege waren `rande` (30 PLZ, ein Weiler; auf
# Deutsch der Rand von etwas) und `fundada` (portugiesisch „gegründet").
#
# Das 99. Perzentil DES JEWEILIGEN LANDES trifft dagegen überall das Richtige:
# DE ab 6 (Berlin 181, München 75, Aachen 10), ES ab 4 (Madrid 63, Marbella 7),
# PT erst ab 176 — womit `rande` und `areal` sauber herausfallen.
_GROSSSTADT_PERZENTIL = 0.99
#
# WARUM ES DIESE SCHRANKE BRAUCHT, gemessen an 1.483 deutschen Büros:
# `abdelkader.de` kam auf „aktiv in Spanien", und die Belege lauteten
# `cristina, felix, roman, teresa`. Das sind die VORNAMEN von der Team-Seite —
# und `plz_geo` führt zu jedem davon ein Dorf. Vier kleine Orte à 1-2 Punkte
# ergaben zusammen die Schwelle, ohne dass ein einziger echter Hinweis auf
# Spanien im Text stand. Der tiefere Crawl verschärft das sogar, weil er mehr
# Team- und Kreditseiten liest.
#
# Eine Namensliste wäre der falsche Weg (sie wird nie fertig). Stattdessen:
# ein Land erreicht „sicher" NUR mit mindestens einem starken Beleg —
# Telefonvorwahl, ausgeschriebener Ländername, kuratiertes Exonym oder eine
# Stadt dieser Größe. Vornamen fallen damit strukturell heraus, auch die, die
# noch niemand gesehen hat. Sie verschwinden nicht: das Land steht weiter als
# „möglich" mit seinen Belegen da.
_SCHWELLEN: dict[str, int] | None = None


def _grossstadt_schwelle(land: str) -> int:
    """Ab wie vielen Postleitzahlen ein Ort IN DIESEM LAND als groß gilt.

    Einmal je Prozess aus `plz_geo` berechnet. Ergibt DE ab 6, ES ab 4,
    PT erst ab 176 — und genau das trennt Madrid von einem Weiler namens
    `Rande`.
    """
    global _SCHWELLEN
    if _SCHWELLEN is None:
        werte: dict[str, list[int]] = defaultdict(list)
        for laender in ort_index().values():
            for l, n in laender.items():
                werte[l].append(n)
        gebaut = {}
        for l, zs in werte.items():
            zs.sort()
            i = min(int(len(zs) * _GROSSSTADT_PERZENTIL), len(zs) - 1)
            # nie unter 3, sonst gilt in einem kleinen Land jeder Weiler als groß
            gebaut[l] = max(zs[i], 3)
        _SCHWELLEN = gebaut
    return _SCHWELLEN.get(land, 10)

_index_lock = threading.Lock()
_ORT_INDEX: dict[str, dict[str, int]] | None = None
_ANZEIGE: dict[str, str] = {}      # gefalteter Name -> echte Schreibweise


def _falten(s: str) -> str:
    """Akzente und Umlaute weg, damit „Málaga" und „Malaga" dasselbe sind."""
    s = (s or "").lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss"), ("á", "a"),
                 ("à", "a"), ("â", "a"), ("ã", "a"), ("é", "e"), ("è", "e"),
                 ("ê", "e"), ("í", "i"), ("ì", "i"), ("î", "i"), ("ó", "o"),
                 ("ò", "o"), ("ô", "o"), ("õ", "o"), ("ú", "u"), ("ù", "u"),
                 ("û", "u"), ("ç", "c"), ("ñ", "n"), ("å", "a"), ("ø", "o"),
                 ("æ", "ae"), ("ý", "y")):
        s = s.replace(a, b)
    return s


def ort_index() -> dict[str, dict[str, int]]:
    """{gefalteter Ortsname: {Land: Anzahl Postleitzahlen}} aus `plz_geo`.

    Einmal je Prozess gebaut (~70.000 Namen, unter einer Sekunde). Die Anzahl
    der Postleitzahlen ist der einzige Größenhinweis, den die Tabelle hergibt —
    sie taugt nicht als Einwohnerzahl, aber als Stichentscheid zwischen `porto`
    in Portugal (4.302) und `porto` in Spanien (1) reicht sie völlig.
    """
    global _ORT_INDEX
    if _ORT_INDEX is not None:
        return _ORT_INDEX
    with _index_lock:
        if _ORT_INDEX is not None:
            return _ORT_INDEX
        idx: dict[str, dict[str, int]] = defaultdict(dict)
        anzeige: dict[str, tuple[int, str]] = {}
        with SessionLocal() as s:
            for place, land, n in s.execute(_sql(
                    "SELECT place, country, COUNT(*) FROM plz_geo "
                    "WHERE place IS NOT NULL AND place <> '' "
                    "GROUP BY place, country")).all():
                name = _falten(place).strip()
                if len(name) < _MIN_LAENGE or name in _STOPP:
                    continue
                if not re.fullmatch(r"[a-z][a-z0-9' \-]*", name):
                    continue      # Klammern, Ziffernpräfixe, Sonderzeichen raus
                idx[name][land] = max(idx[name].get(land, 0), n)
                # Schreibweise für die Anzeige: die des größten Vorkommens.
                # Gesucht und verglichen wird gefaltet und klein ("malaga"),
                # angezeigt werden soll aber "Málaga" — eine Städteliste in
                # Kleinbuchstaben sieht aus wie ein Datenfehler.
                if anzeige.get(name, (0, ""))[0] < n:
                    anzeige[name] = (n, place.strip())
        _ORT_INDEX = dict(idx)
        _ANZEIGE.update({k: v[1] for k, v in anzeige.items()})
        return _ORT_INDEX


# Verbindungswörter bleiben klein: „Palma de Mallorca", nicht „Palma De
# Mallorca". plz_geo selbst führt sie großgeschrieben (die Quelle ist durchweg
# titelgeschrieben), was in einer Städteliste falsch aussieht.
_KLEIN_IM_NAMEN = {"de", "del", "della", "der", "den", "la", "le", "les",
                   "los", "di", "da", "do", "dos", "das", "van", "von", "y",
                   "i", "of", "the", "am", "im", "an", "auf", "sur", "en",
                   "ob", "bei", "unter", "aan", "op"}


def ortsname(gefaltet: str) -> str:
    """Anzeigeform eines Ortsnamens („malaga" -> „Málaga")."""
    ort_index()          # stellt sicher, dass _ANZEIGE gefüllt ist
    roh = _ANZEIGE.get(gefaltet) or gefaltet.title()
    teile = roh.split()
    return " ".join(w if i == 0 or w.lower() not in _KLEIN_IM_NAMEN else w.lower()
                    for i, w in enumerate(teile))


def _vorwahl_laender(roh: str) -> dict[str, int]:
    """Länder aus internationalen Telefonnummern im Text."""
    treffer: dict[str, int] = defaultdict(int)
    # +34 952 12 34 56 / 0034 952… / +34(0)952… — die Trennzeichen sind beliebig
    for m in re.finditer(r"(?:\+|00)\s?(\d{1,3})(?=[\s\-/().]*\d)", roh):
        for pre in _VORWAHLEN:
            if m.group(1).startswith(pre[1:]):
                treffer[LAND_VORWAHL[pre]] += 1
                break
    return dict(treffer)


def _namen_laender(gefaltet: str) -> dict[str, list[str]]:
    """Länder, die im Text ausgeschrieben stehen."""
    treffer: dict[str, list[str]] = defaultdict(list)
    for wort, land in LAND_NAMEN.items():
        if re.search(r"\b" + re.escape(wort) + r"\b", gefaltet):
            treffer[land].append(wort)
    return dict(treffer)


# Namenspartikel, die INNERHALB eines Ortsnamens stehen dürfen. Bewusst nur
# romanische und niederländische — die deutschen Präpositionen „in", „am", „an",
# „auf", „im" standen zuerst mit drin und waren ein echter Fehler: aus
# „Villa in Palma de Mallorca" wurde die Wortgruppe „Villa in Palma", die es
# nirgends gibt, und dabei wurde „Palma de Mallorca" verschluckt. Der Preis ist
# klein — „Frankfurt am Main" wird über „Frankfurt" gefunden, „Sankt Johann im
# Pongau" über „Sankt Johann".
_PARTIKEL = {"de", "del", "della", "der", "den", "la", "le", "les", "los",
             "di", "da", "do", "dos", "das", "van", "op", "aan", "sur", "y"}

# Ortsnamen, die `plz_geo` unter dem amtlichen Namen führt, während Websites den
# international üblichen schreiben. Ohne diese Brücke fehlt ausgerechnet das,
# was Architekten am häufigsten nennen: „Ibiza" steht dort als „Eivissa",
# „Seville" als „Sevilla", „Munich" als „München".
_EXONYME: dict[str, str] = {
    # Spanien
    "ibiza": "ES", "eivissa": "ES", "seville": "ES", "majorca": "ES",
    "mallorca": "ES", "menorca": "ES", "minorca": "ES", "formentera": "ES",
    "balearics": "ES", "balearen": "ES", "baleares": "ES", "canarias": "ES",
    "canaries": "ES", "kanaren": "ES", "tenerife": "ES", "teneriffa": "ES",
    "lanzarote": "ES", "fuerteventura": "ES", "gran canaria": "ES",
    "andalusia": "ES", "andalusien": "ES", "andalucia": "ES", "catalonia": "ES",
    "katalonien": "ES", "cataluna": "ES", "cataluny": "ES", "costa brava": "ES",
    "costa del sol": "ES", "saragossa": "ES", "corunna": "ES", "basque": "ES",
    "baskenland": "ES", "pais vasco": "ES", "galicia": "ES", "galicien": "ES",
    # Portugal
    "lisbon": "PT", "lissabon": "PT", "lisboa": "PT", "algarve": "PT",
    "madeira": "PT", "azores": "PT", "azoren": "PT", "oporto": "PT",
    # Deutschland
    "munich": "DE", "cologne": "DE", "nuremberg": "DE", "hanover": "DE",
    "brunswick": "DE", "ratisbon": "DE", "bavaria": "DE", "bayern": "DE",
    "saxony": "DE", "westphalia": "DE", "black forest": "DE", "schwarzwald": "DE",
    # Italien
    "milan": "IT", "mailand": "IT", "florence": "IT", "florenz": "IT",
    "rome": "IT", "rom": "IT", "turin": "IT", "venice": "IT", "venedig": "IT",
    "naples": "IT", "neapel": "IT", "genoa": "IT", "genua": "IT",
    "sardinia": "IT", "sardinien": "IT", "sicily": "IT", "sizilien": "IT",
    "tuscany": "IT", "toskana": "IT", "south tyrol": "IT", "sudtirol": "IT",
    "lake garda": "IT", "gardasee": "IT", "como": "IT",
    # übriges Europa
    "vienna": "AT", "tyrol": "AT", "tirol": "AT", "salzburg": "AT",
    "geneva": "CH", "genf": "CH", "zurich": "CH", "basle": "CH",
    "engadin": "CH", "wallis": "CH", "valais": "CH",
    "copenhagen": "DK", "kopenhagen": "DK", "kobenhavn": "DK", "jutland": "DK",
    "the hague": "NL", "den haag": "NL", "hague": "NL",
    "brussels": "BE", "brussel": "BE", "bruxelles": "BE", "flanders": "BE",
    "flandern": "BE", "wallonia": "BE", "antwerp": "BE", "antwerpen": "BE",
    "gothenburg": "SE", "goteborg": "SE", "scania": "SE", "schonen": "SE",
    "warsaw": "PL", "warschau": "PL", "cracow": "PL", "krakau": "PL",
    "prague": "CZ", "prag": "CZ", "bohemia": "CZ",
    "athens": "GR", "athen": "GR", "crete": "GR", "kreta": "GR",
    "corfu": "GR", "korfu": "GR", "santorini": "GR", "mykonos": "GR",
    "dalmatia": "HR", "dalmatien": "HR", "istria": "HR", "istrien": "HR",
    "dubrovnik": "HR", "zagreb": "HR",
    "lyons": "FR", "marseilles": "FR", "corsica": "FR", "korsika": "FR",
    "provence": "FR", "riviera": "FR", "cote d'azur": "FR", "brittany": "FR",
    "bretagne": "FR", "normandy": "FR", "normandie": "FR", "elsass": "FR",
    "alsace": "FR", "paris": "FR",
    "london": "GB", "edinburgh": "GB", "cornwall": "GB", "yorkshire": "GB",
    "highlands": "GB", "cotswolds": "GB",
    "dublin": "IE", "cork": "IE",
    "oslo": "NO", "bergen": "NO", "lofoten": "NO",
    "helsinki": "FI", "lapland": "FI", "lappland": "FI",
}


def _ort_treffer(roh: str) -> dict[str, list[tuple[str, dict[str, int]]]]:
    """Großgeschriebene Wortgruppen, die ein Ortsname sind.

    Warum nur großgeschriebene: „Real" ist ein Ort, „real" ein Adjektiv. Im
    Fließtext stehen Ortsnamen immer groß und Alltagswörter fast nie — das ist
    der billigste wirksame Filter, den es hier gibt.

    Gesucht wird über JEDEN zusammenhängenden Teilausschnitt einer großgeschriebenen
    Wortfolge, längster zuerst, und ein Treffer verbraucht seine Wörter. Die
    erste Fassung prüfte nur Anfangsstücke und fand deshalb in „Wohnhaus in
    Marbella" nichts: „wohnhaus in marbella" kennt niemand, und danach war der
    Text verbraucht. Jetzt greift „Marbella" als Ausschnitt.
    """
    idx = ort_index()
    gefunden: dict[str, dict[str, int]] = {}
    partikel = "|".join(sorted(_PARTIKEL, key=len, reverse=True))
    GROSS = "A-ZÄÖÜÁÉÍÓÚÀÈÌÒÙÂÊÎÔÛÇÑÅØÆ"
    lauf = re.compile(rf"\b[{GROSS}][\w'\-]+(?:[ ]+(?:(?:{partikel})[ ]+)?"
                      rf"[{GROSS}][\w'\-]+)*")

    def eintragen(name: str) -> bool:
        if len(name) < _MIN_LAENGE or name in _STOPP:
            return False
        if name in _EXONYME:
            gefunden[name] = {_EXONYME[name]: 999}   # kuratiert = maximal sicher
            return True
        if name in idx:
            gefunden[name] = idx[name]
            return True
        return False

    for m in lauf.finditer(roh):
        teile = _falten(m.group(0)).split()
        belegt = [False] * len(teile)
        for laenge in range(min(len(teile), 3), 0, -1):
            for start in range(0, len(teile) - laenge + 1):
                if any(belegt[start:start + laenge]):
                    continue
                name = " ".join(teile[start:start + laenge])
                if eintragen(name):
                    for i in range(start, start + laenge):
                        belegt[i] = True

    aus: dict[str, list] = defaultdict(list)
    for name, laender in gefunden.items():
        for land in laender:
            aus[land].append((name, laender))
    return dict(aus)


def laender_aus_text(roh: str, heimat: str | None = None,
                     tld: str | None = None) -> dict:
    """Welche Länder nennt dieser Website-Text — und woran erkennt man das.

    `heimat` ist das Land der Büroadresse (aus dem CRM). Es zählt als eigenes
    Signal UND als Stichentscheid: wer in Málaga sitzt, meint mit „Porto"
    eher Portugal als das gleichnamige Dorf in Frankreich.

    Rückgabe:
        {"laender": {"ES": {"punkte": 9, "sicher": True,
                            "belege": ["+34", "Marbella", "espana"]}, ...},
         "unsicher": ["porto: PT|ES"]}
    """
    roh = roh or ""
    gefaltet = _falten(roh)
    punkte: dict[str, int] = defaultdict(int)
    belege: dict[str, list[str]] = defaultdict(list)
    unsicher: list[str] = []
    # Länder mit mindestens EINEM starken Beleg. Siehe _grossstadt_schwelle.
    stark: set[str] = set()
    # Die ORTE je Land, getrennt von den Belegen. Iheb braucht sie als eigene
    # Liste („welche Städte in Spanien?"), und die Belege taugen dafür nicht:
    # dort stehen Vorwahl und Ländername mit drin, und sie sind auf acht
    # Einträge gekappt — ein Büro mit dreißig Mallorca-Projekten hätte acht.
    staedte: dict[str, set[str]] = defaultdict(set)

    # Vorwahl — das stärkste Signal, deshalb das höchste Gewicht
    for land, n in _vorwahl_laender(roh).items():
        punkte[land] += 5 * min(n, 3)
        belege[land].append(f"Vorwahl {[k for k, v in LAND_VORWAHL.items() if v == land][0]}")
        stark.add(land)

    # Ländername im Klartext
    for land, woerter in _namen_laender(gefaltet).items():
        punkte[land] += 4
        belege[land].append(woerter[0])
        stark.add(land)

    # Ortsnamen — mehrdeutige gehen durch die Stichentscheid-Kette
    for land, treffer in _ort_treffer(roh).items():
        for name, laender in treffer:
            groesse = laender.get(land, 1)
            gewicht = _ortsgewicht(groesse)
            if groesse >= _grossstadt_schwelle(land):
                stark.add(land)
            if len(laender) == 1:
                punkte[land] += gewicht
                belege[land].append(name)
                if _taugt_als_ort(name):
                    staedte[land].add(name)
                continue
            # mehrdeutig: entscheiden, nicht würfeln
            wahl = _stichentscheid(laender, punkte, heimat, tld)
            if wahl is None:
                unsicher.append(f"{name}: {'|'.join(sorted(laender))}")
            elif wahl == land:
                punkte[land] += max(1, gewicht - 1)   # abgeleitet, also schwächer
                belege[land].append(f"{name} (mehrdeutig)")
                if _taugt_als_ort(name):
                    staedte[land].add(name)

    # Heimatland des Büros — es ist dort unstrittig tätig
    if heimat:
        punkte[heimat] += 4
        belege[heimat].append("Büroadresse")
        stark.add(heimat)

    # TLD nur als schwacher Zusatz, nie als alleiniger Beleg
    if tld and tld.upper() in set(LAND_VORWAHL.values()):
        punkte[tld.upper()] += 1
        belege[tld.upper()].append(f".{tld.lower()}")

    aus = {}
    for land, p in punkte.items():
        stufe = _stufe(p)
        # Ohne EINEN starken Beleg kommt kein Land über „möglich" hinaus.
        if stufe == "sicher" and land not in stark:
            stufe = "moeglich"
        aus[land] = {"punkte": p, "sicherheit": stufe,
                     "belege": sorted(set(belege[land]))[:8],
                     # ungekappt: genau das ist die Information, die gefragt war
                     "staedte": [ortsname(x) for x in sorted(staedte.get(land, ()))]}
    return {"laender": dict(sorted(aus.items(), key=lambda x: -x[1]["punkte"])),
            "unsicher": sorted(set(unsicher))[:10]}


def _ortsgewicht(plz_anzahl: int) -> int:
    """Wie schwer wiegt ein Ortstreffer — nach der Größe des Ortes.

    GEMESSEN AN ECHTEN WEBSITES, 2026-09-07. Neun Architektenseiten gecrawlt,
    und die Fehlalarme hatten alle dasselbe Merkmal:

        bauen        -> CH  1 Postleitzahl   (Dorf in Uri; deutsches Verb)
        fragen       -> ES  1                (Weiler; deutsches Verb)
        diesen       -> FR  1                (Weiler; deutsches Pronomen)
        thuringen    -> AT  1                (Vorarlberg; gemeint war das Bundesland)
        bundesarchiv -> DE  1                (überhaupt kein Ort)

    Echte Städte dagegen: Berlin 181, München 75, Madrid 63, Barcelona 46,
    Hamburg 42, Valencia 30, Aachen 10, Darmstadt 8, Plauen 4.

    Die Trennung liegt also nicht im Wort, sondern in der Größe — und das ist
    besser als eine Stoppliste, weil es auch die Fehlalarme abfängt, die noch
    niemand gesehen hat. Statt sie zu verwerfen (dabei fiele auch `Ronda` in
    Andalusien mit seiner einen PLZ weg, ein echter Treffer), zählen
    Ein-PLZ-Orte nur noch als schwacher Hinweis: sie können ein Land nicht mehr
    allein auf „möglich" heben, bleiben aber mitsamt Beleg sichtbar.
    """
    if plz_anzahl >= 10:
        return 3        # zwei solche Städte reichen für „sicher"
    if plz_anzahl >= 2:
        return 2        # eine reicht für „möglich"
    return 1            # Ein-PLZ-Ort: sichtbar, aber nicht tragend


def _stufe(punkte: int) -> str:
    """Drei Stufen statt eines Ja/Nein.

    Die erste Fassung kannte nur „sicher ab 4 Punkten". Damit fiel ein Büro in
    London, das Projekte in Mailand zeigt, für Italien stumm durch: eine einzelne
    Stadt bringt 2 Punkte. Das war keine Erkennungslücke — der Ort WURDE gefunden
    —, sondern eine Schwelle, die eine echte Beobachtung wegwarf.

    Eine einzelne genannte Stadt ist ein Hinweis, kein Beweis (sie kann auch die
    Adresse eines Lieferanten oder eine Reisenotiz sein). Also wird sie als das
    ausgewiesen, was sie ist, und Iheb entscheidet beim Filtern, ob ihm
    „möglich" reicht.

      sicher   >= 4  Vorwahl, ausgeschriebener Ländername, Büroadresse,
                     oder mindestens zwei verschiedene Städte
      moeglich 2-3   genau eine eindeutige Stadt
      schwach    1   nur die Domainendung oder ein mehrdeutiger Ortsname
    """
    if punkte >= 4:
        return "sicher"
    return "moeglich" if punkte >= 2 else "schwach"


def _taugt_als_ort(name: str) -> bool:
    """Gehoert dieser Treffer in eine STAEDTELISTE?

    Zwei Sorten fallen heraus, beide im Probelauf aufgetaucht:
      * Vornamen von Team-Seiten (`maria`, `manuel`) — siehe _VORNAMEN.
      * LAENDERNAMEN. `plz_geo` fuehrt tatsaechlich Orte namens „España" und
        „Nederland", also stand in der Staedteliste eines spanischen Bueros
        „Barcelona, España, Madrid". Fuers Land war das richtig, als Stadt ist
        es Unsinn.

    Regionen (`Catalonia`, `Andalusia`) bleiben ABSICHTLICH drin: „taetig in
    Katalonien" ist eine brauchbare Auskunft, auch wenn es keine Stadt ist.
    """
    return name not in _VORNAMEN and name not in LAND_NAMEN


def _stichentscheid(laender: dict[str, int], punkte: dict[str, int],
                    heimat: str | None, tld: str | None) -> str | None:
    """Welches Land ist bei einem mehrdeutigen Ortsnamen gemeint?

    Der Reihe nach: schon durch Vorwahl/Ländername belegt -> Heimatland ->
    TLD -> deutlich mehr Postleitzahlen (Faktor 5). Bleibt es offen, wird
    NICHTS gewählt; der Fall geht als `unsicher` heraus.
    """
    belegt = [l for l in laender if punkte.get(l, 0) >= 4]
    if len(belegt) == 1:
        return belegt[0]
    if heimat in laender:
        return heimat
    if tld and tld.upper() in laender:
        return tld.upper()
    nach_groesse = sorted(laender.items(), key=lambda x: -x[1])
    if len(nach_groesse) >= 2 and nach_groesse[0][1] >= 5 * max(nach_groesse[1][1], 1):
        return nach_groesse[0][0]
    return None
