"""Über Zeitfenster blättern, wenn die Gegenstelle keine Seitenzahl kennt.

Der Flow zu Dataverse liefert höchstens `deckel` Zeilen und sagt NICHT, ob mehr
da wären. Ein voller Abruf ist von einem gekappten also nicht zu unterscheiden.
Daraus folgt die ganze Mechanik hier:

  * Bei Gleichstand wird IMMER geteilt. Eine Abfrage zu viel kostet Sekunden,
    ein stilles Loch in den Daten kostet Vertrauen in jede Zahl darüber.
  * Bei EINEM Tag ist Schluss mit Teilen. Dann wird gewarnt statt geschwiegen —
    ein gekapptes Ergebnis sieht vollständig aus, und genau diese Verwechslung
    hat hier schon einmal 36 Domains ihre Daten gekostet, ohne dass irgendwo
    ein Fehler stand.
  * `max_spanne` fragt von klein nach groß. Nicht die Zeilenzahl war das
    Problem, sondern die ANTWORTGRÖSSE: der erste Entwurf des E-Mail-Abrufs
    fragte ein Jahr am Stück und scheiterte vollständig (HTTP 504, danach
    abgerissene Verbindungen), weil E-Mail-Rümpfe HTML sind. Die gemessene
    Probewoche waren 2.834 Zeilen und rund 23 MB.

WAS HIER NICHT HINEINGEHÖRT. `crm_emails` und `crm_leads` haben denselben
Aufbau — `_rows`, `_fetch`, `_walk`, `_company_resolver`, `sync`, `stats` —
aber nur dieser Algorithmus ist wirklich derselbe. Die Zuordnung Datensatz →
Firma etwa sieht gleich aus und ist es nicht: E-Mails lösen über die
Verkaufschance zu deren Auftraggeber auf, Leads ausschließlich über die im CRM
gesetzte Mutterfirma, und zwar bewusst nicht über Namensähnlichkeit. Beides in
eine Funktion mit Schaltern zu ziehen würde zwei verschiedene Entscheidungen zu
einer verwaschenen machen. Geteilt wird die Mechanik, nicht das Urteil.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

log = logging.getLogger("adwatch.crm_fenster")


def blaettern(hole, start: dt.date, ende: dt.date, out: list, *,
              deckel: int, max_spanne: int | None = None,
              pause: float = 0.0, was: str = "Abruf") -> None:
    """`hole(von, bis)` über den Zeitraum aufrufen und alles nach `out` legen.

    `max_spanne` begrenzt die Größe einer einzelnen Anfrage in Tagen (None =
    unbegrenzt, dann wird erst bei Deckel-Treffer geteilt). `pause` ist eine
    Höflichkeitspause zwischen zwei Teilanfragen.
    """
    spanne = (ende - start).days

    if max_spanne is not None and spanne > max_spanne:
        jetzt = start
        while jetzt < ende:
            naechst = min(jetzt + dt.timedelta(days=max_spanne), ende)
            blaettern(hole, jetzt, naechst, out, deckel=deckel,
                      max_spanne=max_spanne, pause=pause, was=was)
            jetzt = naechst
        return

    got = hole(start, ende)
    if len(got) < deckel:
        out.extend(got)
        return
    if spanne <= 1:
        log.warning("%s: Deckel schon an einem Tag (%s), %d Zeilen — es fehlen "
                    "welche (Flow kappt, keine Sortierung erlaubt)",
                    was, start, len(got))
        out.extend(got)
        return

    mitte = start + dt.timedelta(days=spanne // 2)
    if pause:
        time.sleep(pause)
    blaettern(hole, start, mitte, out, deckel=deckel, max_spanne=max_spanne,
              pause=pause, was=was)
    if pause:
        time.sleep(pause)
    blaettern(hole, mitte, ende, out, deckel=deckel, max_spanne=max_spanne,
              pause=pause, was=was)
