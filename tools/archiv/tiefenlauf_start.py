"""Den Tiefenlauf starten. Läuft lange — für den Hintergrund gedacht.

    python tools/tiefenlauf_start.py            # Durchgang 1: die 82
    python tools/tiefenlauf_start.py --alle     # Durchgang 2: alle Nicht-Spanier
    python tools/tiefenlauf_start.py --neu      # noch einmal, auch das Fertige

Wiederaufnehmbar: was in `arch_web_scan` ohne Fehler steht, wird übersprungen.
Ein Abbruch kostet also nur die angefangene Domain.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])

from adwatch.enrich import tiefenlauf as TL   # noqa: E402

if __name__ == "__main__":
    alle = "--alle" in sys.argv
    neu = "--neu" in sys.argv
    grenze = None
    for a in sys.argv:
        if a.startswith("--limit="):
            grenze = int(a.split("=", 1)[1])

    topf = TL.grundgesamtheit(nur_spanien_aktiv=not alle, ohne_spanische=True)
    print(f"Durchgang: {'ALLE europaeischen Nicht-Spanier' if alle else 'die Spanien-aktiven'}")
    print(f"Domains im Topf: {len(topf)}")
    t0 = time.time()
    aus = TL.lauf(nur_spanien_aktiv=not alle, ohne_spanische=True,
                  limit=grenze, neu=neu)
    print(f"\nFertig in {(time.time()-t0)/60:.1f} Minuten")
    for k in ("gesamt", "fertig", "projekte", "spanien", "fehler"):
        print(f"  {k:10s} {aus.get(k)}")
