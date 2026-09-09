"""Spanischer Ort → Provinz → Autonome Gemeinschaft. Ohne Dienst, ohne Schlüssel.

Iheb braucht die Möglichkeit, „nach der größeren Region wie Katalonien oder
Andalusien" zu filtern. Dafür gibt es keine Spalte und es muss auch keine
gekauft werden: die spanische Postleitzahl trägt die Provinz in ihren ersten
zwei Ziffern, und die Zuordnung Provinz → Autonome Gemeinschaft ist ein
feststehendes Verzeichnis von 52 Einträgen. Beides steht unten.

    08                     Barcelona                Cataluña
    07                     Baleares                 Illes Balears
    29                     Málaga                   Andalucía

Der Weg ist damit: Ortsname → `plz_geo` (die Tabelle liegt schon in der
Datenbank, 11.150 spanische Zeilen) → PLZ → Provinzziffern → Region.

EIN ORT KANN IN MEHREREN PROVINZEN LIEGEN. „Los Villares" gibt es in Jaén und
in Salamanca. Wo das vorkommt, gewinnt die Provinz mit den meisten
Postleitzahlen dieses Namens; steht es 1:1, wird die Mehrdeutigkeit
zurückgegeben statt versteckt. Eine Region, die geraten wurde, muss man als
geraten erkennen können.
"""
from __future__ import annotations

import threading
from collections import Counter, defaultdict

from sqlalchemy import text as _sql

from ..db import SessionLocal

# Die ersten zwei Ziffern der spanischen PLZ. Reihenfolge ist die amtliche
# (alphabetisch nach Provinzname), deshalb sind die Zahlen nicht geografisch.
PROVINZ: dict[str, str] = {
    "01": "Álava", "02": "Albacete", "03": "Alicante", "04": "Almería",
    "05": "Ávila", "06": "Badajoz", "07": "Baleares", "08": "Barcelona",
    "09": "Burgos", "10": "Cáceres", "11": "Cádiz", "12": "Castellón",
    "13": "Ciudad Real", "14": "Córdoba", "15": "A Coruña", "16": "Cuenca",
    "17": "Girona", "18": "Granada", "19": "Guadalajara", "20": "Gipuzkoa",
    "21": "Huelva", "22": "Huesca", "23": "Jaén", "24": "León",
    "25": "Lleida", "26": "La Rioja", "27": "Lugo", "28": "Madrid",
    "29": "Málaga", "30": "Murcia", "31": "Navarra", "32": "Ourense",
    "33": "Asturias", "34": "Palencia", "35": "Las Palmas", "36": "Pontevedra",
    "37": "Salamanca", "38": "Santa Cruz de Tenerife", "39": "Cantabria",
    "40": "Segovia", "41": "Sevilla", "42": "Soria", "43": "Tarragona",
    "44": "Teruel", "45": "Toledo", "46": "Valencia", "47": "Valladolid",
    "48": "Bizkaia", "49": "Zamora", "50": "Zaragoza", "51": "Ceuta",
    "52": "Melilla",
}

# Provinz → Autonome Gemeinschaft. Die Namen in der Landessprache, wie sie auch
# auf den Websites stehen — wer nach „Cataluña" filtert, tippt nicht
# „Katalonien". Der deutsche Name steht in REGION_DE daneben.
REGION: dict[str, str] = {
    "Álava": "País Vasco", "Bizkaia": "País Vasco", "Gipuzkoa": "País Vasco",
    "Barcelona": "Cataluña", "Girona": "Cataluña", "Lleida": "Cataluña",
    "Tarragona": "Cataluña",
    "Almería": "Andalucía", "Cádiz": "Andalucía", "Córdoba": "Andalucía",
    "Granada": "Andalucía", "Huelva": "Andalucía", "Jaén": "Andalucía",
    "Málaga": "Andalucía", "Sevilla": "Andalucía",
    "Alicante": "Comunitat Valenciana", "Castellón": "Comunitat Valenciana",
    "Valencia": "Comunitat Valenciana",
    "A Coruña": "Galicia", "Lugo": "Galicia", "Ourense": "Galicia",
    "Pontevedra": "Galicia",
    "Ávila": "Castilla y León", "Burgos": "Castilla y León",
    "León": "Castilla y León", "Palencia": "Castilla y León",
    "Salamanca": "Castilla y León", "Segovia": "Castilla y León",
    "Soria": "Castilla y León", "Valladolid": "Castilla y León",
    "Zamora": "Castilla y León",
    "Albacete": "Castilla-La Mancha", "Ciudad Real": "Castilla-La Mancha",
    "Cuenca": "Castilla-La Mancha", "Guadalajara": "Castilla-La Mancha",
    "Toledo": "Castilla-La Mancha",
    "Badajoz": "Extremadura", "Cáceres": "Extremadura",
    "Huesca": "Aragón", "Teruel": "Aragón", "Zaragoza": "Aragón",
    "Las Palmas": "Canarias", "Santa Cruz de Tenerife": "Canarias",
    "Baleares": "Illes Balears",
    "Madrid": "Comunidad de Madrid", "Murcia": "Región de Murcia",
    "Navarra": "Navarra", "La Rioja": "La Rioja", "Asturias": "Asturias",
    "Cantabria": "Cantabria", "Ceuta": "Ceuta", "Melilla": "Melilla",
}

REGION_DE: dict[str, str] = {
    "Cataluña": "Katalonien", "Andalucía": "Andalusien",
    "Comunitat Valenciana": "Valencia", "Illes Balears": "Balearen",
    "Canarias": "Kanaren", "País Vasco": "Baskenland",
    "Comunidad de Madrid": "Madrid", "Castilla y León": "Kastilien-León",
    "Castilla-La Mancha": "Kastilien-La Mancha", "Galicia": "Galicien",
    "Región de Murcia": "Murcia", "Navarra": "Navarra", "Aragón": "Aragón",
    "Extremadura": "Extremadura", "Asturias": "Asturien",
    "Cantabria": "Kantabrien", "La Rioja": "La Rioja", "Ceuta": "Ceuta",
    "Melilla": "Melilla",
}

# Inseln und Regionen, die als Ortsname auftreten und keine eigene PLZ haben —
# dieselbe Lücke, die `taetigkeit._FLAECHEN` für die Karte schließt. Ein
# Projekt „auf Mallorca" muss unter Balearen filterbar sein, auch wenn kein
# Ort genannt ist.
_FLAECHE_ZU_PROVINZ: dict[str, str] = {
    "mallorca": "Baleares", "majorca": "Baleares", "menorca": "Baleares",
    "ibiza": "Baleares", "eivissa": "Baleares", "formentera": "Baleares",
    "baleares": "Baleares", "balearen": "Baleares", "illes balears": "Baleares",
    "tenerife": "Santa Cruz de Tenerife", "la palma": "Santa Cruz de Tenerife",
    "la gomera": "Santa Cruz de Tenerife", "el hierro": "Santa Cruz de Tenerife",
    "gran canaria": "Las Palmas", "lanzarote": "Las Palmas",
    "fuerteventura": "Las Palmas", "canarias": "Las Palmas",
    "cataluna": "Barcelona", "catalunya": "Barcelona", "cataluña": "Barcelona",
    "galicia": "A Coruña", "andalucia": "Sevilla", "andalucía": "Sevilla",
    "costa del sol": "Málaga", "costa brava": "Girona",
    "costa blanca": "Alicante", "pais vasco": "Bizkaia",
    # Englische und deutsche Schreibweisen. `laender._EXONYME` erkennt sie als
    # Ort, `plz_geo` kennt sie nicht -- ohne diese Zeilen bleibt die Region
    # leer, obwohl der Ort eindeutig ist (cruzyortiz.com: 64x "seville").
    "seville": "Sevilla", "saragossa": "Zaragoza", "cordova": "Córdoba",
    "catalonia": "Barcelona", "andalusia": "Sevilla", "andalusien": "Sevilla",
    "katalonien": "Barcelona", "balearics": "Baleares", "minorca": "Baleares",
    "canary islands": "Las Palmas", "basque country": "Bizkaia",
    "baskenland": "Bizkaia", "biscay": "Bizkaia", "corunna": "A Coruña",
}

_lock = threading.Lock()
_index: dict[str, list[str]] | None = None


def _plz_index() -> dict[str, list[str]]:
    """Gefalteter Ortsname → die Provinznamen, in denen er vorkommt (häufigste
    zuerst). Einmal gebaut, dann im Speicher — 11.150 Zeilen."""
    global _index
    with _lock:
        if _index is not None:
            return _index
        roh: dict[str, Counter] = defaultdict(Counter)
        with SessionLocal() as s:
            for plz, ort in s.execute(_sql(
                    "SELECT plz, place FROM plz_geo WHERE country='ES'")):
                prov = PROVINZ.get(str(plz or "").zfill(5)[:2])
                if prov and ort:
                    roh[_falten(ort)][prov] += 1
        _index = {k: [p for p, _ in c.most_common()] for k, c in roh.items()}
        return _index


def _falten(s: str) -> str:
    s = (s or "").strip().lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
                 ("ñ", "n"), ("ü", "u"), ("à", "a"), ("è", "e"), ("ç", "c")):
        s = s.replace(a, b)
    return " ".join(s.split())


_schreibweisen: dict[str, str] | None = None


def ort_schoen(ort: str) -> str:
    """Die amtliche Schreibweise eines Ortes.

    Der Ortsabgleich in `laender.py` arbeitet gefaltet und kleingeschrieben —
    richtig zum Vergleichen, unbrauchbar in einer Tabelle, die jemand liest.
    „palma de mallorca" wird hier wieder zu „Palma de Mallorca", und zwar aus
    `plz_geo` und nicht aus einer Großschreibungsregel: die würde aus
    „port d'andratx" ein „Port D'Andratx" machen.
    """
    global _schreibweisen
    with _lock:
        if _schreibweisen is None:
            gebaut: dict[str, str] = {}
            with SessionLocal() as s:
                for (name,) in s.execute(_sql(
                        "SELECT DISTINCT place FROM plz_geo WHERE country='ES'")):
                    if name:
                        gebaut.setdefault(_falten(name), name)
            _schreibweisen = gebaut
    return _hilfswoerter_klein(_schreibweisen.get(_falten(ort)) or (ort or "").title())


# Die Verbindungswoerter spanischer Ortsnamen bleiben klein. `plz_geo` fuehrt
# sie gross ("Palma De Mallorca"), weil die Quelle jedes Wort gross schreibt.
_KLEIN = {"de", "del", "de la", "la", "las", "los", "el", "y", "i", "da", "do",
          "dos", "des", "der", "den", "a", "o", "e"}


def _hilfswoerter_klein(name: str) -> str:
    teile = (name or "").split()
    return " ".join(w if i == 0 or w.lower() not in _KLEIN else w.lower()
                    for i, w in enumerate(teile))


def einordnen(ort: str) -> dict:
    """{provinz, region, region_de, eindeutig} — leere Felder, wenn unbekannt.

    `eindeutig` ist False, wenn der Ortsname in mehreren Provinzen vorkommt.
    Die Spalte wandert bis in die Excel durch: eine Region, die geraten wurde,
    muss man als geraten erkennen können.
    """
    gefaltet = _falten(ort)
    prov = _FLAECHE_ZU_PROVINZ.get(gefaltet)
    eindeutig = True
    if not prov:
        treffer = _plz_index().get(gefaltet) or []
        if not treffer:
            return {"provinz": None, "region": None, "region_de": None,
                    "eindeutig": None}
        prov = treffer[0]
        eindeutig = len(treffer) == 1
    region = REGION.get(prov)
    return {"provinz": prov, "region": region,
            "region_de": REGION_DE.get(region or "", region),
            "eindeutig": eindeutig}
