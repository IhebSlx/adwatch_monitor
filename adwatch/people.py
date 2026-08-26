"""Kolleginnen und Kollegen aus dem Microsoft-Verzeichnis — nur zum Auswählen.

WOZU. Empfänger von Berichten wurden bisher abgetippt. Ein Tippfehler in einer
Adresse heißt: der Bericht geht an eine fremde Person oder ins Leere, und
niemand merkt es. Auswählen statt tippen behebt genau das.

WARUM ÜBER EINEN FLOW UND NICHT ÜBER MICROSOFT GRAPH. Der direkte Weg bräuchte
eine Azure-App-Registrierung, ein Client-Secret und die Zustimmung eines
Administrators für `User.Read.All`. Der Flow-Umweg braucht nichts davon: er
handelt unter der Identität dessen, der ihn angelegt hat — dieselbe Bauart wie
der Dataverse-Zugriff, und ein Geheimnis weniger zu verwalten.

DATENSPARSAMKEIT. Gespeichert wird ausschließlich, was zum Versenden nötig ist:
Name und Adresse, beides erst dann, wenn jemand die Person bewusst als
Empfänger aufnimmt. Suchergebnisse landen NICHT in der Datenbank, Fotos werden
gar nicht erst geholt. Es soll ein Empfängerfeld sein, kein Personenverzeichnis.

Nur Lesen. Es wird nie ins Verzeichnis geschrieben.
"""
from __future__ import annotations

import logging
import re
import time

from . import flows

log = logging.getLogger("adwatch.people")

# Kurzer Zwischenspeicher: Tippen loest je Zeichen eine Suche aus, und
# dieselbe Abfrage zweimal in zehn Sekunden ist immer dieselbe Antwort.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 120.0
_MIN_ZEICHEN = 2


def _rows(body) -> list[dict]:
    """Der Flow liefert je nach Aufbau eine nackte Liste oder {value: [...]}.
    Beides annehmen, statt sich auf eine Form zu verlassen — dieselbe Lehre wie
    beim Lead-Abruf, wo genau das still null Zeilen ergab."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("value", "users", "body"):
            v = body.get(key)
            if isinstance(v, list):
                return v
    return []


def _norm(r: dict) -> dict | None:
    """Eine Verzeichniszeile auf das reduzieren, was die App braucht.

    Die Feldnamen unterscheiden sich je nach Connector-Version (`mail` vs.
    `userPrincipalName`, `displayName` vs. `DisplayName`), deshalb wird der
    Reihe nach gesucht statt einen Namen vorauszusetzen."""
    def hol(*namen):
        for n in namen:
            v = r.get(n)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    mail = hol("mail", "Mail", "userPrincipalName", "UserPrincipalName", "email")
    if not mail or "@" not in mail:
        return None            # ohne Adresse als Empfänger wertlos
    return {
        "name": hol("displayName", "DisplayName", "givenName") or mail.split("@")[0],
        "email": mail,
        "titel": hol("jobTitle", "JobTitle"),
        "abteilung": hol("department", "Department"),
    }


def verfuegbar() -> bool:
    """Ist der Flow eingerichtet? Ohne ihn bleibt das freie Eingabefeld."""
    return flows.is_configured("graph_users")


def suchen(text: str, top: int = 8) -> list[dict]:
    """Personen im Verzeichnis suchen. Leere Liste, wenn nichts eingerichtet
    ist — die Empfängerauswahl fällt dann auf Tippen zurück, statt zu brechen."""
    text = (text or "").strip()
    if len(text) < _MIN_ZEICHEN or not verfuegbar():
        return []

    schluessel = f"{text.lower()}|{top}"
    jetzt = time.monotonic()
    treffer = _CACHE.get(schluessel)
    if treffer and jetzt - treffer[0] < _CACHE_TTL:
        return treffer[1]

    try:
        body = flows.post("graph_users", {"suche": text, "top": int(top)}, timeout=15)
    except Exception as exc:                     # noqa: BLE001
        # Ein kaputter Flow darf die Empfängerpflege nicht blockieren.
        log.warning("Personensuche fehlgeschlagen: %s", str(exc)[:200])
        return []

    leute = [p for p in (_norm(r) for r in _rows(body)) if p]
    # Der Connector sortiert nach Relevanz, nicht nach Wortanfang. Wer „Mar"
    # tippt, meint aber eher „Marouani" als „Baumarkt-Service".
    tl = text.lower()
    leute.sort(key=lambda p: (not (p["name"] or "").lower().startswith(tl),
                              not (p["email"] or "").lower().startswith(tl),
                              p["name"] or ""))
    _CACHE[schluessel] = (jetzt, leute)
    return leute


_TEAMS_MAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def teams_link(email: str | None) -> str | None:
    """Tiefer Link in einen Teams-Chat mit dieser Person.

    Teams lässt sich NICHT einbetten — Microsoft verbietet das per
    frame-ancestors, ein <iframe> bliebe leer. Ein Deep Link tut, was gemeint
    ist, und braucht überhaupt keine Berechtigung."""
    e = (email or "").strip()
    if not e or not _TEAMS_MAIL.match(e):
        return None
    from urllib.parse import quote
    return f"https://teams.microsoft.com/l/chat/0/0?users={quote(e)}"
