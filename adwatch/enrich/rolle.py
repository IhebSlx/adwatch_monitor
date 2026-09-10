"""Vergibt dieses Büro Aufträge — oder empfiehlt es nur? Ohne Modell.

WARUM DAS HIER STEHT UND NICHT IM LLM-PROMPT.
`decision_role` war von Anfang an im Architekten-Prompt, und der Prompt sagt
selbst, wie er es bestimmt: er sucht eine feste Liste von Leistungsphasen-
Begriffen im Text. Bauleitung, Ausschreibung, Vergabe, „dirección de obra",
„llave en mano". Das ist Extraktion, keine Beurteilung — und ein Sprachmodell,
das eine geschlossene Wortliste abgleicht, macht Stichwortsuche zum Cent-Preis.

Der Unterschied zu `solarlux_relevance` ist genau hier: DIE Frage („könnten
große Glasflächen zu diesem Portfolio passen?") lässt sich nicht am Wortlaut
entscheiden, weil kein Architekturbüro seine Fensterflächen auf die Website
schreibt. Dort muss ein Modell aus PROJEKTTYPEN schließen. Hier nicht.

Was das ändert: statt 153 bewerteter Büros für Geld sind es alle 20.696
umsonst, jederzeit wiederholbar, und jeder Wert trägt die Wendung mit, an der
er erkannt wurde — nachprüfbar, ohne die Seite noch einmal zu holen.

WAS DIE ROLLE BEDEUTET.
Architekten kaufen nichts (gemessen: 5,9 % Gewinnrate gegen eine Grundlinie von
21,3 %, über 808 Konten 0 % Konversion). Sie schreiben aus, und der ausführende
Fachbetrieb kauft. Ein Büro, das die Ausführung STEUERT, ist trotzdem etwas
anderes als eines, das nur entwirft: es entscheidet mit, wer liefert. Genau
diese Teilmenge trennt `vergibt Aufträge` heraus.
"""
from __future__ import annotations

import logging
import re



logger = logging.getLogger("adwatch.rolle")

VERGIBT = "vergibt Aufträge"
EMPFIEHLT = "empfiehlt"

# Wendungen, die STEUERUNG der Ausführung belegen. In den Sprachen, in denen
# die Websites tatsächlich geschrieben sind — die spanischen und italienischen
# Begriffe stehen nicht der Vollständigkeit halber da, sondern weil die
# Spanien-Liste sonst leer bliebe.
_STEUERT = (
    # deutsch
    "bauleitung", "objektüberwachung", "objektuberwachung", "bauüberwachung",
    "bauuberwachung", "ausschreibung", "vergabe", "projektsteuerung",
    "bauherrenvertretung", "generalplanung", "generalplaner",
    "schlüsselfertig", "schlusselfertig", "baumanagement", "bauoberleitung",
    "leistungsphase 8", "lph 8", "lph8", "leistungsphasen 1-9", "lp 8",
    # spanisch
    "direccion de obra", "direccion facultativa", "direccion ejecucion",
    "llave en mano", "obra completa", "gestion de obra", "project management",
    "direccion integrada",
    # italienisch / französisch / niederländisch / englisch
    "direzione lavori", "chiavi in mano", "clé en main", "cle en main",
    "maitrise d'oeuvre", "maitrise d oeuvre", "suivi de chantier",
    "directievoering", "bouwbegeleiding", "aanbesteding", "uitvoering",
    "contract administration", "tender", "site supervision",
    "construction management", "turnkey", "design and build",
    # --- schwedisch / daenisch / norwegisch ------------------------------
    # Gemessen 2026-09-08 und peinlich: SE 241 Bueros -> 8 mit Rolle (3 %),
    # DK 179 -> 8 (4 %). Nicht weil skandinavische Bueros die Ausfuehrung
    # seltener steuern, sondern weil meine Wortliste ihre Sprachen nicht
    # kannte. Eine Spalte, die fuer ein Land systematisch leer bleibt, sagt
    # etwas ueber die Liste und nichts ueber das Land.
    "byggledning", "projektering", "upphandling", "byggherreombud",
    "kontrollansvarig", "totalentreprenad", "generalentreprenad",
    "byggledelse", "byggherrerådgivning", "byggherreraadgivning",
    "fagtilsyn", "byggetilsyn", "udbud", "hovedentreprise",
    "totalentreprise", "prosjektledelse", "byggeledelse", "anbud",
)

# Wendungen, die auf reines Entwerfen und Planen hindeuten. Schwächer als die
# obigen: sie schließen Steuerung nicht aus, also gewinnt bei einem Treffer in
# BEIDEN Listen immer `vergibt Aufträge`.
_ENTWIRFT = (
    "entwurfsplanung", "entwurf und planung", "konzeptplanung", "vorentwurf",
    "machbarkeitsstudie", "wettbewerbe", "studien und wettbewerbe",
    "solo diseno", "anteproyecto", "proyecto basico",
    "conception", "esquisse", "ontwerp", "schetsontwerp",
    "concept design", "feasibility", "masterplanning", "competitions",
    # skandinavisch
    "skisseprosjekt", "idekonkurranse", "arkitekttavling", "arkitektkonkurrence",
    "forprojekt", "forstudie", "konceptdesign",
)


def _falten(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss"), ("á", "a"),
                 ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n"),
                 ("ç", "c"), ("è", "e"), ("à", "a"), ("ì", "i"), ("ò", "o")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s)


def rolle_aus_text(roh: str) -> tuple[str | None, list[str]]:
    """(Rolle, gefundene Wendungen). None, wenn der Text nichts hergibt.

    `null` ist ein ehrliches Ergebnis und kein Auffangwert: eine Seite, die ihre
    Leistungsphasen nicht nennt, sagt über die Rolle nichts. Sie als
    „empfiehlt" zu führen wäre geraten.
    """
    t = _falten(roh)
    if not t.strip():
        return None, []
    steuert = [w for w in _STEUERT if w in t]
    entwirft = [w for w in _ENTWIRFT if w in t]
    if steuert:
        return VERGIBT, steuert[:6]
    if entwirft:
        return EMPFIEHLT, entwirft[:6]
    return None, []
