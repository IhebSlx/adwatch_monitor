"""Den KV aus dem CRM nachholen — den echten, aus `ownerid`.

WARUM DAS NÖTIG WAR. In der Excel stand 66 mal derselbe KV und sonst nichts.
Iheb hat den Gegenbeweis geliefert: Chapman Taylor, im CRM oben rechts „Dieker,
Berthold", bei uns leer. Die Ursache steckt in zwei Sätzen:

    adwatch/customers.py    „kv": ("kv",)          <- aus hochgeladenem Excel
    adwatch/crm_accounts.py select_fields()        <- fragt ownerid NIE ab

Die Spalte `kv` wird also NUR gefüllt, wenn jemand einen Export mit einer
Spalte „KV" hochlädt. Für den spanischen Markt ist das passiert (982 Firmen,
alle „Gimenez, Juan"), für den deutschen Kundenstamm auch (3.612). Firmen, die
über die Live-Anbindung hereinkamen — Chapman Taylor, Herzog & de Meuron,
Chipperfield — hatten nie einen. Sie standen als „kein KV zugewiesen" da,
obwohl im CRM einer steht. Ein leeres Feld sah aus wie eine Tatsache.

Live nachgeprüft, ein GET auf zwei Datensätze:

    _ownerid_value@…FormattedValue = "Dieker, Berthold"     (Chapman Taylor)
    _ownerid_value@…FormattedValue = "Reck, Carsten"        (Chapman Taylor DE)

Der KV im Formularkopf IST der Besitzer des Datensatzes. Er wird hier nach
`companies.crm_owner` geschrieben, nicht nach `kv`: die alte Spalte behält
ihre Herkunft, sonst wüsste hinterher niemand mehr, was aus welchem Jahr
stammt.

NUR LESEN. Das Skript schickt GETs über den bestehenden Flow und schreibt
ausschließlich in die lokale Datenbank. Ins CRM geht nichts zurück.

    python tools/kv_holen.py --spanienliste     nur die Büros der Excel
    python tools/kv_holen.py --alle             alle Firmen mit crm_id
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as _sql          # noqa: E402

from adwatch import crm_accounts             # noqa: E402
from adwatch.db import SessionLocal          # noqa: E402

FORMATIERT = "_ownerid_value@OData.Community.Display.V1.FormattedValue"

# Dataverse nimmt den Filter als URL entgegen; 25 GUIDs sind knapp 1.000
# Zeichen und bleiben sicher unter jeder Längengrenze.
BUENDEL = 25


def _ids_spanienliste() -> list[str]:
    """Die crm_ids der Büros, die in der Spanien-Excel stehen."""
    from tools.spanien_excel import _ist_portal, erheben

    d = erheben()
    doms = {p[0] for p in d["projekte"] if not _ist_portal(p[1])}
    doms = {x for x in doms if x in d["firmen"] and not _ist_portal(x)}
    if not doms:
        return []
    platz = ",".join(f":d{i}" for i in range(len(doms)))
    with SessionLocal() as s:
        return [r[0] for r in s.execute(_sql(
            f"SELECT crm_id FROM companies WHERE crm_id IS NOT NULL "
            f"AND website_domain IN ({platz})"),
            {f"d{i}": x for i, x in enumerate(sorted(doms))})]


def _ids_alle() -> list[str]:
    with SessionLocal() as s:
        return [r[0] for r in s.execute(_sql(
            "SELECT crm_id FROM companies WHERE crm_id IS NOT NULL"))]


def holen(ids: list[str]) -> dict:
    """Je Bündel ein GET. Gibt Zählungen zurück, keine Erfolgsmeldung."""
    zahl = {"angefragt": len(ids), "geantwortet": 0, "mit_kv": 0,
            "geschrieben": 0, "fehler": 0}
    for anfang in range(0, len(ids), BUENDEL):
        teil = ids[anfang:anfang + BUENDEL]
        flt = " or ".join(f"accountid eq {x}" for x in teil)
        try:
            zeilen = crm_accounts.fetch_accounts(flt, top=BUENDEL)
        except Exception as e:                              # noqa: BLE001
            zahl["fehler"] += len(teil)
            print(f"  Buendel ab {anfang}: {type(e).__name__} {str(e)[:90]}",
                  flush=True)
            continue
        zahl["geantwortet"] += len(zeilen)
        with SessionLocal() as s:
            for z in zeilen:
                name = (z.get(FORMATIERT) or "").strip()
                if not name:
                    continue
                zahl["mit_kv"] += 1
                zahl["geschrieben"] += s.execute(_sql(
                    "UPDATE companies SET crm_owner = :n WHERE crm_id = :i"),
                    {"n": name, "i": z.get("accountid")}).rowcount
            s.commit()
        print(f"  {anfang + len(teil):5d}/{len(ids)}  "
              f"mit KV: {zahl['mit_kv']}", flush=True)
    return zahl


if __name__ == "__main__":
    # `ownerid` muss im $select stehen, sonst liefert der Flow die Spalte nicht.
    # Hier gesetzt statt in crm_accounts.select_fields(), damit ein GET dieses
    # Skripts nichts an dem ändert, was der nächtliche Abgleich anfasst.
    _urspruenglich = crm_accounts.select_fields
    crm_accounts.select_fields = lambda: ["accountid", "name", "_ownerid_value"]
    try:
        if "--alle" in sys.argv:
            ids = _ids_alle()
        elif "--spanienliste" in sys.argv:
            ids = _ids_spanienliste()
        else:
            print(__doc__.strip().splitlines()[-3])
            raise SystemExit(2)
        print(f"{len(ids)} Datensaetze mit crm_id, {BUENDEL} je Abfrage")
        e = holen(ids)
        print(e)
    finally:
        crm_accounts.select_fields = _urspruenglich
