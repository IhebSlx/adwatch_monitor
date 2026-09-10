"""Hat `solarlux_relevance` überhaupt Signal? — der Test, den es nie gab.

DIE FRAGE.
`solarlux_relevance` bewertet ein Architekturbüro als hoch/mittel/gering: könnten
zu diesem Portfolio große Glasflächen passen? Das ist ein URTEIL des Modells,
kein extrahierter Fakt — und es wurde nie gegen ein Ergebnis geprüft.

Gemessen 2026-09-08 war die Überschneidung exakt null: von 190 bewerteten Büros
hatte KEINES eine Projekthistorie, und von 167 Büros mit gewonnenem Objekt war
KEINES bewertet. Die Note stand also seit Monaten in der Oberfläche, ohne dass
irgendjemand wusste, ob sie etwas vorhersagt.

DER AUFBAU.
Zwei Gruppen, gleich behandelt, und danach EIN Vergleich:

  POSITIVE   Architekten mit mindestens einem GEWONNENEN Objekt (n=111).
             Bei ihnen ist belegt, dass Solarlux mit ihnen gebaut hat.
  KONTROLLE  Architekten ohne gewonnenes Objekt, gleiche Länderverteilung.

Die Länderverteilung wird bewusst nachgebildet: die Positiven sind überwiegend
deutsch, und eine Kontrollgruppe voller spanischer Büros würde einen
Länderunterschied messen statt eines Relevanzunterschieds.

Die Frage lautet dann: bekommen Büros, mit denen wir gewonnen haben, häufiger
die Note `hoch` als vergleichbare Büros ohne Gewinn?

WAS DER TEST NICHT KANN.
Bei 111 gegen 150 lässt sich ein Unterschied wie 11 % gegen 25 % erkennen. Ein
kleinerer — etwa 11 % gegen 16 % — kommt als „nicht entscheidbar" heraus, und
genau das wird dann auch berichtet. Ausserdem: gewonnen zu haben heisst, dass
das Büro damals in einem Projekt vorkam, nicht dass es das ideale Büro ist. Der
Test prüft, ob die Note mit der Wirklichkeit korreliert, nicht ob sie kausal ist.
"""
import io
import json
import logging
import os
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text                      # noqa: E402

from adwatch import config                       # noqa: E402
from adwatch.db import SessionLocal, init_db     # noqa: E402

(config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(message)s",
    handlers=[logging.FileHandler(config.DATA_DIR / "logs" / "relevanztest.log",
                                  encoding="utf-8"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("relevanztest")

init_db()
from adwatch.enrich import service               # noqa: E402

ARBEITER = 3          # der Länderlauf hält schon 8 Verbindungen offen
KONTROLLE_JE_POSITIV = 1.4


def gruppen():
    """(positive_ids, kontroll_ids) — die Kontrolle bildet die Länder nach."""
    basis = ("segment='Architekten' AND duplicate_of IS NULL "
             "AND website_domain IS NOT NULL AND website_domain <> ''")
    with SessionLocal() as s:
        pos = s.execute(text(
            f"SELECT id, country FROM companies WHERE {basis} "
            "AND COALESCE(arch_won,0) > 0")).all()
        frei = s.execute(text(
            f"SELECT id, country FROM companies WHERE {basis} "
            "AND COALESCE(arch_won,0) = 0 AND solarlux_relevance IS NULL")).all()

    je_land = Counter(land for _, land in pos)
    nach_land = defaultdict(list)
    for cid, land in frei:
        nach_land[land].append(cid)
    rng = random.Random(20260908)          # feste Ziehung, nachvollziehbar
    kontrolle = []
    for land, n in je_land.items():
        topf = nach_land.get(land, [])
        rng.shuffle(topf)
        kontrolle += topf[:int(n * KONTROLLE_JE_POSITIV)]
    return [cid for cid, _ in pos], kontrolle


def bewerten(ids, etikett):
    fertig, fehler = 0, 0
    t0 = time.time()

    def eins(cid):
        try:
            return cid, service.enrich_company(cid, allow_search=False,
                                               allow_llm=True), None
        except Exception as e:                    # noqa: BLE001
            return cid, None, str(e)[:120]

    with ThreadPoolExecutor(max_workers=ARBEITER) as pool:
        futures = {pool.submit(eins, c): c for c in ids}
        for fut in as_completed(futures):
            _cid, _res, err = fut.result()
            fertig += 1
            if err:
                fehler += 1
                if fehler <= 5:
                    log.warning("  %s: %s", etikett, err)
            if fertig % 25 == 0:
                log.info("  %s: %d/%d (%d Fehler, %.0f min)",
                         etikett, fertig, len(ids), fehler, (time.time() - t0) / 60)
    log.info("%s fertig: %d Büros, %d Fehler, %.0f Minuten",
             etikett, len(ids), fehler, (time.time() - t0) / 60)


def auswerten(pos_ids, ktr_ids):
    with SessionLocal() as s:
        def verteilung(ids):
            if not ids:
                return Counter()
            roh = s.execute(text(
                "SELECT COALESCE(solarlux_relevance,'(offen)'), COUNT(*) "
                f"FROM companies WHERE id IN ({','.join(str(i) for i in ids)}) "
                "GROUP BY 1")).all()
            return Counter(dict(roh))
        return verteilung(pos_ids), verteilung(ktr_ids)


def main():
    pos, ktr = gruppen()
    log.info("Positive (gewonnenes Objekt): %d   Kontrolle: %d   ~%.2f EUR",
             len(pos), len(ktr), (len(pos) + len(ktr)) * 0.01)
    bewerten(pos, "positiv")
    bewerten(ktr, "kontrolle")

    vp, vk = auswerten(pos, ktr)
    np_, nk = sum(vp.values()), sum(vk.values())
    log.info("")
    log.info("%-10s %-22s %-22s", "Note", "gewonnen", "Kontrolle")
    for note in ("hoch", "mittel", "gering", "(offen)"):
        a, b = vp.get(note, 0), vk.get(note, 0)
        log.info("%-10s %4d  (%5.1f %%)        %4d  (%5.1f %%)",
                 note, a, 100 * a / max(np_, 1), b, 100 * b / max(nk, 1))

    ah, bh = vp.get("hoch", 0), vk.get("hoch", 0)
    pa, pb = ah / max(np_, 1), bh / max(nk, 1)
    log.info("")
    log.info("hoch-Anteil: gewonnen %.1f %%  gegen Kontrolle %.1f %%", pa * 100, pb * 100)
    # Wilson-Intervalle, damit die Aussage nicht auf der Punktschaetzung steht
    from adwatch.insights.konversion import wilson
    la, ha = wilson(ah, max(np_, 1))
    lb, hb = wilson(bh, max(nk, 1))
    log.info("   gewonnen  %.1f–%.1f %%", la * 100, ha * 100)
    log.info("   Kontrolle %.1f–%.1f %%", lb * 100, hb * 100)
    if la > hb:
        log.info("URTEIL: die Note hat Signal — gewonnene Bueros sind haeufiger 'hoch'.")
    elif lb > ha:
        log.info("URTEIL: die Note zeigt in die FALSCHE Richtung.")
    else:
        log.info("URTEIL: nicht entscheidbar — die Intervalle ueberlappen. "
                 "Nicht danach sortieren.")

    io.open(config.DATA_DIR / "logs" / "relevanztest.json", "w",
            encoding="utf-8").write(json.dumps(
                {"positiv": dict(vp), "kontrolle": dict(vk)},
                ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
