"""Der ganze Lauf in zwei Stufen — Vorabtest, dann Tiefenlauf mit Haiku.

    python tools/spanienlauf.py              # beides, der Reihe nach
    python tools/spanienlauf.py --vorab      # nur Stufe 1
    python tools/spanienlauf.py --tief       # nur Stufe 2 (Vorabtest muss stehen)
    python tools/spanienlauf.py --limit=200  # zum Ausprobieren

Beide Stufen sind wiederaufnehmbar: Stufe 1 überspringt, was in
`arch_web_vorab` steht, Stufe 2 was fehlerfrei in `arch_web_scan` steht. Ein
Abbruch kostet nur die angefangene Domain — der Lauf darf also über Nacht
laufen und am nächsten Tag weitergehen.

GEMESSENE ERWARTUNG (Stichprobe 200 Domains, Trefferquote an 35 bestätigten):

    Stufe 1   10.212 Domains, 2,6 Seiten je Domain, 68 Domains/min   ~2,5 h
    Stufe 2   rund 900 Domains, 98 Seiten je Domain, Haiku            ~4,5 h
                                                                     ~7 h
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")

from adwatch.enrich import tiefenlauf as TL, vorlauf as VL   # noqa: E402


def _zeit(sek: float) -> str:
    return f"{sek/3600:.1f} h" if sek > 3600 else f"{sek/60:.0f} min"


if __name__ == "__main__":
    nur_vorab = "--vorab" in sys.argv
    nur_tief = "--tief" in sys.argv
    neu = "--neu" in sys.argv
    grenze = next((int(a.split("=", 1)[1]) for a in sys.argv
                   if a.startswith("--limit=")), None)

    t0 = time.time()

    if not nur_tief:
        topf = VL.grundgesamtheit(mit_spanischen=True)
        print(f"\n=== STUFE 1: Vorabtest ueber {len(topf)} Domains ===")
        a = VL.lauf(limit=grenze, neu=neu)
        print(f"\nStufe 1 fertig in {_zeit(time.time()-t0)}")
        for k in ("gesamt", "fertig", "verdacht", "leer", "unerreichbar"):
            print(f"  {k:14s} {a.get(k)}")
        if a.get("fertig"):
            print(f"  Verdachtsquote {100*a['verdacht']/a['fertig']:.1f} %")

    if not nur_vorab:
        kandidaten = VL.verdachtsdomains()
        print(f"\n=== STUFE 2: Tiefenlauf ueber {len(kandidaten)} Verdachtsdomains ===")
        t1 = time.time()
        b = TL.lauf(nur_spanien_aktiv=False, ohne_spanische=False,
                    nur_vorab_verdacht=True, limit=grenze, neu=neu, mit_ki=True)
        print(f"\nStufe 2 fertig in {_zeit(time.time()-t1)}")
        for k in ("gesamt", "fertig", "projekte", "spanien", "fehler",
                  "ki_aufrufe", "ki_fehler", "ki_verworfen"):
            print(f"  {k:14s} {b.get(k)}")
        print(f"  Haiku-Kosten   ${b.get('ki_kosten', 0):.2f}")

    print(f"\nGESAMT {_zeit(time.time()-t0)}")
