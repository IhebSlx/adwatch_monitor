"""Den Länderlauf über die ganze Grundgesamtheit fahren — als Hintergrundlauf.

Aufruf:  python tools/laenderlauf_start.py
Der Fortschritt landet in data/logs/laenderlauf.log; der Lauf ist
wiederaufnehmbar (schon geprüfte Zeilen werden übersprungen).
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adwatch import config                      # noqa: E402
from adwatch.db import init_db                  # noqa: E402

(config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.FileHandler(config.DATA_DIR / "logs" / "laenderlauf.log",
                                  encoding="utf-8"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("start")

init_db()
from adwatch.enrich import laenderlauf          # noqa: E402
import threading                                # noqa: E402

t0 = time.time()
ergebnis = {}


def fahren():
    ergebnis.update(laenderlauf.lauf())


th = threading.Thread(target=fahren, daemon=False)
th.start()
while th.is_alive():
    time.sleep(60)
    s = laenderlauf.stand()
    log.info("Stand: %s/%s fertig · gefunden %s · leer %s · Fehler %s · "
             "%s/min · noch ~%s min",
             s["fertig"], s["gesamt"], s["gefunden"], s["leer"], s["fehler"],
             s.get("pro_minute", "?"), s.get("rest_minuten", "?"))
th.join()
log.info("FERTIG nach %.0f Minuten: %s", (time.time() - t0) / 60,
         {k: v for k, v in ergebnis.items() if k != "fehler_beispiele"})
for f in ergebnis.get("fehler_beispiele", [])[:20]:
    log.info("   Fehler: %s", f)
log.info("Übersicht: %s", laenderlauf.uebersicht())
