"""Im Chat gelesene Urteile in die Datenbank schreiben.

Das Guthaben lief mitten im Lauf aus; 9.092 Modellaufrufe scheiterten. Für die
562 Projektseiten NICHT-spanischer Büros lese ich die Seiten stattdessen selbst
und trage das Ergebnis hier ein.

Format je Zeile: URL, Urteil, optional Ort und Baujahr.

    ("https://…/casa-x", "ES", "Marbella", 2019),
    ("https://…/haus-y", "NEIN"),           # nicht in Spanien
    ("https://…/uebersicht", "KEIN"),       # gar keine Projektseite

`quelle` wird auf „gelesen" gesetzt — damit steht in der Excel neben jedem Ort,
wer ihn behauptet: Haiku, Regel oder gelesen. Ein Urteil ohne Herkunft wäre
genau die Sorte Zahl, die dieses Projekt schon zweimal verdorben hat.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as _sql          # noqa: E402

from adwatch.db import SessionLocal          # noqa: E402
from adwatch.enrich import laender, regionen  # noqa: E402


def anwenden(urteile: list[tuple]) -> dict:
    """Urteile eintragen. Gibt eine Zählung zurück, keine Erfolgsmeldung."""
    jetzt = dt.datetime.now().isoformat(timespec="seconds")
    zahl = {"ES": 0, "NEIN": 0, "KEIN": 0, "unbekannt": 0}
    with SessionLocal() as s:
        for eintrag in urteile:
            url, urteil = eintrag[0], eintrag[1].upper()
            ort = eintrag[2] if len(eintrag) > 2 else None
            jahr = eintrag[3] if len(eintrag) > 3 else None

            if urteil in ("NEIN", "KEIN"):
                # Die Zeile verschwindet: entweder liegt das Projekt nicht in
                # Spanien, oder es ist gar kein Projekt. Beides heisst, dass
                # sie in der Spanien-Liste nichts zu suchen hat.
                s.execute(_sql("DELETE FROM arch_web_projects WHERE url = :u "
                               "AND land = 'ES'"), {"u": url})
                zahl[urteil] += 1
                continue
            if urteil != "ES":
                zahl["unbekannt"] += 1
                continue

            gefaltet = laender._falten(ort) if ort else None
            r = regionen.einordnen(ort) if ort else {
                "provinz": None, "region": None, "region_de": None, "eindeutig": None}
            s.execute(_sql("""
                UPDATE arch_web_projects
                   SET ort = COALESCE(:o, ort), provinz = :p, region = :r,
                       region_de = :rd, region_eindeutig = :e,
                       baujahr = COALESCE(:j, baujahr),
                       quelle = 'gelesen', sicherheit = 'hoch',
                       beleg = COALESCE(beleg, 'im Chat gelesen'),
                       gescannt_am = :z
                 WHERE url = :u AND land = 'ES'"""),
                {"o": gefaltet, "p": r["provinz"], "r": r["region"],
                 "rd": r["region_de"],
                 "e": None if r["eindeutig"] is None else int(r["eindeutig"]),
                 "j": jahr, "z": jetzt, "u": url})
            zahl["ES"] += 1
        s.commit()
    return zahl


def domain_urteil(domain: str, ausser: list[str] | None = None) -> int:
    """Alle spanischen Zeilen einer Domain verwerfen -- ausser den genannten.

    Der schnellste Weg durch die Liste, und der ehrlichste: bei
    bfl-architekten.de steht "Buero Valencia (ES)" im Fuss JEDER Seite, also
    ist die Entscheidung eine Eigenschaft der WEBSITE und nicht der einzelnen
    Seite. Sie einzeln zu treffen waere 28 mal dieselbe Entscheidung mit 28
    Gelegenheiten, sich zu vertun.
    """
    behalten = set(ausser or [])
    with SessionLocal() as s:
        urls = [r[0] for r in s.execute(_sql(
            "SELECT DISTINCT url FROM arch_web_projects WHERE domain=:d AND land='ES'"),
            {"d": domain})]
        weg = [u for u in urls if u not in behalten]
        for u in weg:
            s.execute(_sql("DELETE FROM arch_web_projects WHERE url=:u AND land='ES'"),
                      {"u": u})
        s.commit()
    return len(weg)


def offen() -> int:
    with SessionLocal() as s:
        return s.execute(_sql("""
            SELECT COUNT(DISTINCT p.url) FROM arch_web_projects p
            JOIN (SELECT website_domain, MIN(country) country FROM companies
                  WHERE website_domain <> '' GROUP BY website_domain) c
              ON c.website_domain = p.domain
            WHERE p.land='ES' AND (p.quelle IS NULL OR p.quelle NOT IN ('Haiku','gelesen'))
              AND c.country <> 'ES'""")).scalar()


if __name__ == "__main__":
    print("Noch offen:", offen())
