"""Die offenen Projektseiten zum Nachlesen aufbereiten — für die Prüfung im Chat.

Das Guthaben lief mitten im Lauf aus, 9.092 Modellaufrufe scheiterten, und die
betroffenen Seiten fielen auf den deterministischen Befund zurück — den, der in
der Stichprobe 2 von 14 richtig hatte.

Nachgelesen werden hier nur die Seiten NICHT-spanischer Büros: 562 statt 2.637.
Bei einem spanischen Büro ist „liegt in Spanien" ohnehin fast immer richtig,
und Iheb filtert diese Büros über die Spalte Hauptsitz selbst heraus.

Ausgegeben wird je Seite genau das, was auch das Modell bekam — Titel, Adresse,
Ortszone —, nur kompakt genug, um es am Stück zu lesen. Wo die Zone nichts
hergibt, holt `--voll` den ganzen Seitentext nach.

    python tools/offene_faelle.py --von=0 --bis=80
    python tools/offene_faelle.py --domain=gmp-architekten.de --voll
"""
from __future__ import annotations

import concurrent.futures as cf
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as _sql          # noqa: E402

from adwatch.db import SessionLocal          # noqa: E402
from adwatch.enrich import tiefenlauf as TL  # noqa: E402
from adwatch.identity import website_source as ws   # noqa: E402

OFFEN = """
SELECT p.domain, p.url, p.titel, GROUP_CONCAT(DISTINCT p.ort) orte,
       MAX(c.country) sitz, MAX(c.name) firma
FROM arch_web_projects p
JOIN (SELECT website_domain, MIN(country) country, MIN(name) name
      FROM companies WHERE website_domain <> '' GROUP BY website_domain) c
  ON c.website_domain = p.domain
WHERE p.land = 'ES' AND (p.quelle IS NULL OR p.quelle NOT IN ('Haiku','gelesen'))
  AND c.country <> 'ES'
GROUP BY p.url ORDER BY p.domain, p.url
"""


def faelle(von: int = 0, bis: int = 60, domain: str | None = None) -> list:
    with SessionLocal() as s:
        zeilen = s.execute(_sql(OFFEN)).all()
    if domain:
        zeilen = [z for z in zeilen if z[0] == domain]
    return zeilen[von:bis]


def _holen(fall, voll: bool):
    dom, url, titel, orte, sitz, firma = fall
    holen = ws._fetch_url(url, timeout=20)
    if not holen:
        return {"domain": dom, "url": url, "titel": titel, "orte": orte,
                "sitz": sitz, "firma": firma, "zone": None}
    html = holen[0]
    text = ws._page_text(html, limit=12000, drop_chrome=True)
    echter_titel = TL._titel(html) or titel
    zone = TL.ortszone(echter_titel, TL.ohne_anhang(text))
    return {"domain": dom, "url": url, "titel": echter_titel, "orte": orte,
            "sitz": sitz, "firma": firma,
            "zone": (text[:2600] if voll else zone[:420])}


if __name__ == "__main__":
    von = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--von=")), 0)
    bis = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--bis=")), 60)
    dom = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--domain=")), None)
    voll = "--voll" in sys.argv

    liste = faelle(von, bis, dom)
    with SessionLocal() as s:
        gesamt = len(s.execute(_sql(OFFEN)).all())
    print(f"# {len(liste)} von {gesamt} offenen Projektseiten "
          f"(nicht-spanische Bueros), Bereich {von}-{bis}\n")

    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for i, d in enumerate(pool.map(lambda f: _holen(f, voll), liste), start=von + 1):
            print(f"[{i}] {d['firma'][:44]}  ({d['sitz']})")
            print(f"    {d['url'][:104]}")
            print(f"    Titel:    {(d['titel'] or '')[:96]}")
            print(f"    Regel-Ort: {d['orte']}")
            z = (d["zone"] or "(Seite nicht erreichbar)").replace("\n", " ")
            print(f"    Zone: {z}")
            print()
