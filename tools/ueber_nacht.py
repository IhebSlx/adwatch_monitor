"""Die ganze Kette, unbeaufsichtigt: Vorabtest, Tiefenlauf, Excel.

Gebaut, weil Iheb weg ist und morgen ein Ergebnis auf dem Tisch haben will.
Drei Eigenschaften, die dafür nötig sind:

1. UNABHÄNGIG VON DER SITZUNG. Wird als eigener Windows-Prozess gestartet und
   überlebt das Ende der Claude-Sitzung, das Sperren des Rechners und das
   Schließen des Terminals.

2. JEDE STUFE EIN EIGENER PROZESS. Die Excel wird nicht importiert, sondern
   als Unterprozess aufgerufen. Damit gilt der Stand des Skripts VOM ZEITPUNKT
   DES AUFRUFS — ich kann `tools/spanien_excel.py` also noch verbessern,
   während der Crawl läuft, und die Verbesserung landet in der Datei von
   morgen.

3. NICHTS VERSCHLUCKEN. Jede Stufe schreibt Anfang, Ende, Dauer und Rückgabe
   ins Protokoll. Fällt eine Stufe um, laufen die folgenden trotzdem — aber
   die Zusammenfassung am Ende sagt, was gefehlt hat. Ein Lauf, der nachts
   scheitert und wie ein Erfolg aussieht, wäre das Schlimmste hier.

Aufruf (macht `tools/nacht_start.ps1` automatisch):
    python tools/ueber_nacht.py
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL))

PYTHON = sys.executable
STUFEN = [
    ("Vorabtest + Tiefenlauf", [PYTHON, "tools/spanienlauf.py"]),
    ("Excel", [PYTHON, "tools/spanien_excel.py"]),
    ("Buerodossier PDF", [PYTHON, "tools/spanien_bueros.py"]),
]


def sag(*teile) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}]", *teile, flush=True)


if __name__ == "__main__":
    t0 = time.time()
    sag("=" * 62)
    sag("NACHTLAUF START", dt.datetime.now().strftime("%d.%m.%Y %H:%M"))
    sag("=" * 62)

    bericht = []
    for name, befehl in STUFEN:
        sag(f"--- {name} ---")
        t1 = time.time()
        try:
            p = subprocess.run(befehl, cwd=str(WURZEL), text=True,
                               capture_output=True, timeout=20 * 3600)
            dauer = time.time() - t1
            # Die letzten Zeilen der Stufe ins Protokoll, damit man morgens
            # sieht, was sie gefunden hat, ohne ein zweites Log zu suchen.
            for zeile in (p.stdout or "").strip().splitlines()[-25:]:
                sag("   ", zeile)
            if p.returncode != 0:
                for zeile in (p.stderr or "").strip().splitlines()[-12:]:
                    sag("  FEHLER:", zeile)
            bericht.append((name, p.returncode, dauer))
            sag(f"--- {name}: Rueckgabe {p.returncode}, {dauer/60:.0f} min ---")
        except subprocess.TimeoutExpired:
            bericht.append((name, "Zeitueberschreitung", time.time() - t1))
            sag(f"--- {name}: ZEITUEBERSCHREITUNG nach 20 h ---")
        except Exception as e:                              # noqa: BLE001
            bericht.append((name, f"{type(e).__name__}: {e}", time.time() - t1))
            sag(f"--- {name}: {type(e).__name__}: {e} ---")

    sag("=" * 62)
    sag(f"NACHTLAUF FERTIG nach {(time.time()-t0)/3600:.1f} h")
    for name, rueck, dauer in bericht:
        zeichen = "ok" if rueck == 0 else "FEHLER"
        sag(f"  {zeichen:7s} {name:26s} {dauer/60:6.0f} min   {rueck}")
    sag("Ergebnisse in output/ — Excel und PDF tragen das heutige Datum.")
    sag("=" * 62)
