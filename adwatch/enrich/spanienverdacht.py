"""Zwei Stufen: ein absichtlich dummer Filter, dann ein Modell, das urteilt.

WARUM ZWEISTUFIG.
Der Ortsabgleich in `laender.py` ist gut darin, spanische Ortsnamen zu FINDEN,
und schlecht darin zu entscheiden, ob der gefundene Name der Ort DIESES
Projekts ist. Fünf Fehlerklassen sind daran aufgefallen — Navigationsmenü,
Seitenfuß, Literaturverzeichnis, Verwandtenliste, eingebettete Projektliste —
und jede brauchte eine eigene handgebaute Regel. Eine Stichprobe von 14
erkannten Projektzeilen ergab trotzdem 2 richtige und 12 falsche.

Alle fünf sind dieselbe Frage: **welcher Text auf dieser Seite gehört zu
diesem Projekt?** Das ist keine Tabellenfrage, sondern eine Lesefrage.

Also: die billige Stufe sucht nach GRÜNDEN, hinzusehen — großzügig, mit allen
Fehlalarmen, die dazugehören. Die teure Stufe liest die ganze Seite und
antwortet. Iheb: „it should be reliable if it looks at the complete library
etc, and would recognize anything that would point to spain... and then haiku
would approve and answer everything else there is to answer."

WAS DIE BILLIGE STUFE ABSICHTLICH NICHT TUT: filtern. Ein Verdacht, der hier
verworfen wird, erreicht das Modell nie. Deshalb reicht hier JEDES Signal —
auch „María" und „Ronda", die als Ortsnamen Unsinn sind. Das Aussortieren ist
die Aufgabe der zweiten Stufe.

DIE EINE GRENZE, DIE BLEIBT: eine Projektseite, die nirgends im Text sagt, wo
sie steht — Ort nur im Bild, in einer PDF oder gar nicht. Die findet auch das
Modell nicht. Das ist fehlende Information, kein Filterfehler, und sie steht
in der Excel-Spalte „ohne erkennbaren Ort".
"""
from __future__ import annotations

import json
import logging
import re

from . import laender

logger = logging.getLogger("adwatch.spanienverdacht")

MODELL = "claude-haiku-4-5"
MAX_ZEICHEN = 9000          # rund 2.300 Token je Seite
_MAX_TOKEN_AUS = 400

# --- Stufe 1: Gründe, hinzusehen -------------------------------------------
_SPANIENWORT = re.compile(
    r"\b(espa[nñ]a|espanya|spanien|spain|espagne|spagna|spanje|espanha|"
    r"spanisch|spanish|español|espanol|castellano|catalu[nñ]|balear|"
    r"canari|andaluc|mallorca|menorca|ibiza|tenerife)\b", re.I)
_VORWAHL_34 = re.compile(r"\+\s?34[\s\-/.]?\d")
_ES_PLZ_NAH = re.compile(
    r"\b(0[1-9]|[1-4]\d|5[0-2])\d{3}\b[^\n]{0,40}?"
    r"(espa|barcelona|madrid|valencia|sevilla|m[aá]laga|bilbao|palma|"
    r"alicante|murcia|zaragoza|granada|c[oó]rdoba|vigo|gij[oó]n)", re.I)
_ES_LINK = re.compile(r"https?://[^\s\"']+\.es\b", re.I)
_ES_SPRACHE = re.compile(
    r"\b(proyecto|vivienda|reforma|obra nueva|arquitectura|edificio|"
    r"promoci[oó]n|urbanizaci[oó]n|habitatge|projecte)\b", re.I)


def gruende(text: str, url: str = "", html: str = "") -> list[str]:
    """Alles, was auf Spanien deuten KÖNNTE. Großzügig mit Absicht."""
    g: list[str] = []
    if _SPANIENWORT.search(text):
        g.append("Spanien-Wort im Text")
    if _VORWAHL_34.search(text) or _VORWAHL_34.search(html or ""):
        g.append("Vorwahl +34")
    if _ES_PLZ_NAH.search(text):
        g.append("spanische PLZ mit Ort")
    if _ES_LINK.search(html or ""):
        g.append(".es-Verweis")
    if _ES_SPRACHE.search(text):
        g.append("spanische Fachwörter")
    # Der rohe Ortsabgleich, ohne jede Prüfung — genau das Rauschen, das die
    # erste Stufe liefern SOLL. „María" und „Ronda" sind hier willkommen.
    treffer = laender._ort_treffer(text)
    if treffer.get("ES"):
        namen = sorted({n for n, _ in treffer["ES"]})[:6]
        g.append("Ortsname: " + ", ".join(namen))
    pfad = (url or "").lower()
    if any(w in pfad for w in ("spanien", "spain", "espana", "mallorca", "ibiza",
                               "barcelona", "madrid", "marbella", "menorca")):
        g.append("Ort in der Adresse")
    return g


def verdaechtig(text: str, url: str = "", html: str = "") -> bool:
    return bool(gruende(text, url, html))


# --- Stufe 2: das Modell liest und entscheidet -----------------------------
_ANWEISUNG = """Du bekommst den Text EINER Seite von der Website eines \
Architektur- oder Planungsbüros. Beantworte ausschließlich über DIESES eine \
Projekt, nicht über andere Projekte des Büros.

Auf solchen Seiten stehen regelmäßig Orte, die NICHT der Projektort sind:
- der Sitz des Büros (Kopf- oder Fußzeile)
- Verlagsorte in Literaturangaben ("Vol. 89, Madrid, El Croquis, 2006")
- andere Projekte in einer Liste ("weitere Projekte", Navigationsmenü)
- Werbeprosa ("Wiener Kaffeehaus trifft Barcelona")
- Namen von Personen, die zufällig auch Gemeindenamen sind (María, Borja, Ronda)

Antworte NUR mit JSON, ohne Vorrede und ohne Code-Zaun:
{"ist_projekt": true/false,
 "in_spanien": true/false,
 "ort": "Stadt oder Gemeinde, sonst null",
 "provinz_oder_region": "sonst null",
 "sicherheit": "hoch"/"mittel"/"niedrig",
 "beleg": "der Wortlaut der Seite, aus dem der Ort hervorgeht, max. 120 Zeichen"}

ist_projekt=false, wenn die Seite eine Übersicht, ein Archiv, eine Nachricht \
oder eine Bürovorstellung ist statt ein einzelnes Projekt.
in_spanien=false, wenn das Projekt woanders liegt — auch wenn Spanien auf der \
Seite vorkommt.
ort=null, wenn die Seite nicht sagt, wo das Projekt steht. Rate nicht."""


def _client():
    import anthropic

    from .. import config
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("Kein ANTHROPIC_API_KEY hinterlegt — Settings-Tab oder .env.")
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def beurteilen(titel: str, url: str, text: str, client=None) -> dict:
    """Eine Projektseite von Haiku lesen lassen.

    Bei jedem Fehler kommt `{"fehler": ...}` zurück und NICHT etwa ein leeres
    Ergebnis: ein verschluckter Modellfehler hat in diesem Projekt schon
    einmal einen kompletten Lauf als „0 Fehler" durchgehen lassen, obwohl
    jeder einzelne Aufruf mit 400 abgelehnt worden war.
    """
    import anthropic

    client = client or _client()
    inhalt = f"URL: {url}\nTitel: {titel}\n\n{(text or '')[:MAX_ZEICHEN]}"
    try:
        antwort = client.messages.create(
            model=MODELL, max_tokens=_MAX_TOKEN_AUS,
            system=_ANWEISUNG,
            messages=[{"role": "user", "content": inhalt}])
    except anthropic.APIStatusError as e:
        return {"fehler": f"{e.status_code}: {str(e.message)[:160]}"}
    except Exception as e:                                   # noqa: BLE001
        return {"fehler": f"{type(e).__name__}: {str(e)[:160]}"}

    roh = "".join(b.text for b in antwort.content if b.type == "text").strip()
    roh = re.sub(r"^```(?:json)?|```$", "", roh, flags=re.M).strip()
    try:
        d = json.loads(roh)
    except ValueError:
        return {"fehler": "kein JSON", "roh": roh[:200]}
    d["tokens_ein"] = antwort.usage.input_tokens
    d["tokens_aus"] = antwort.usage.output_tokens
    d["kosten"] = round(antwort.usage.input_tokens / 1e6 * 1.0
                        + antwort.usage.output_tokens / 1e6 * 5.0, 6)
    return d
