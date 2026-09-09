"""Stichprobe: was stand auf der Seite, und was hat der Lauf daraus gemacht?

Iheb hat gefragt, ob ich jede gecrawlte Seite persönlich durchsehen kann.
Kann ich nicht — 82 Büros sind rund 14.000 Seiten, der volle Durchgang über
alle europäischen Nicht-Spanier wären etwa 1,7 Millionen. Was geht, und was
bisher jeden einzelnen Fehler gefunden hat, ist die STICHPROBE: eine Handvoll
Seiten vollständig lesen, nicht tausend überfliegen.

Dieses Werkzeug macht die Stichprobe nachprüfbar. Es holt die Seite noch
einmal, legt den gelesenen Text daneben und schreibt dazu, welche Orte der
Lauf erkannt hat und mit welcher Begründung. Wer eine Zeile in der Excel nicht
glaubt, sieht hier den Text, aus dem sie stammt.

    python tools/tiefenlauf_pruefen.py                    # 25 zufällige ES-Treffer
    python tools/tiefenlauf_pruefen.py --domain=big.dk    # alles zu einer Domain
    python tools/tiefenlauf_pruefen.py --n=60 --html      # als HTML zum Durchblättern
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as _sql          # noqa: E402

from adwatch import config                   # noqa: E402
from adwatch.db import SessionLocal          # noqa: E402
from adwatch.enrich import laender, regionen, tiefenlauf as TL   # noqa: E402
from adwatch.identity import website_source as ws                # noqa: E402


def stichprobe(n: int = 25, domain: str | None = None,
               nur_es: bool = True) -> list[dict]:
    wo = ["1=1"]
    if domain:
        wo.append("domain = :d")
    if nur_es:
        wo.append("land = 'ES'")
    with SessionLocal() as s:
        zeilen = s.execute(
            _sql(f"SELECT domain, url, titel, ort, region, beleg FROM arch_web_projects "
                 f"WHERE {' AND '.join(wo)}"),
            {"d": domain} if domain else {}).all()
    if not domain and len(zeilen) > n:
        zeilen = random.sample(zeilen, n)
    aus = []
    for dom, url, titel, ort, region, beleg in zeilen[:n if domain else n]:
        holen = ws._fetch_url(url, timeout=15)
        if not holen:
            aus.append({"domain": dom, "url": url, "titel": titel, "ort": ort,
                        "region": region, "beleg": beleg, "text": None,
                        "jetzt_erkannt": None, "hinweis": "Seite jetzt nicht erreichbar"})
            continue
        html = holen[0]
        text = ws._page_text(html, limit=12000, drop_chrome=True)
        jetzt_titel = TL._titel(html)
        treffer = laender._ort_treffer(f"{jetzt_titel}\n{text}")
        es_jetzt = sorted({x for x, _ in treffer.get("ES", [])})
        aus.append({
            "domain": dom, "url": url, "titel": titel or jetzt_titel,
            "ort": ort, "region": region, "beleg": beleg,
            "text": text,
            "jetzt_erkannt": es_jetzt,
            "hinweis": None if (ort in es_jetzt or not ort) else
                       "Ort steht heute nicht mehr im Seitentext",
        })
    return aus


def als_text(proben: list[dict]) -> str:
    zeilen = []
    for i, p in enumerate(proben, 1):
        zeilen.append("=" * 78)
        zeilen.append(f"{i}. {p['domain']}   {p['titel']}")
        zeilen.append(f"   {p['url']}")
        zeilen.append(f"   ERKANNT: {p['ort']}  ({p['region']})   Beleg: {p['beleg']}")
        if p["jetzt_erkannt"] is not None:
            zeilen.append(f"   beim Nachlesen: {', '.join(p['jetzt_erkannt']) or '(nichts)'}")
        if p["hinweis"]:
            zeilen.append(f"   ACHTUNG: {p['hinweis']}")
        zeilen.append("   --- Seitentext, wie der Lauf ihn gelesen hat ---")
        for stueck in (p["text"] or "(kein Text)").split("\n"):
            zeilen.append("   " + stueck[:400])
        zeilen.append("")
    return "\n".join(zeilen)


def als_html(proben: list[dict], pfad: str) -> str:
    from xml.sax.saxutils import escape as e
    teile = ["<meta charset='utf-8'><title>Tiefenlauf — Stichprobe</title>",
             "<style>body{font:14px/1.5 system-ui;margin:0;background:#f7f9fb;color:#1f2933}"
             "header{background:#1f2933;color:#fff;padding:14px 22px}"
             "article{background:#fff;margin:14px 22px;padding:14px 18px;border-left:3px solid #2b6cb0;"
             "border-radius:4px}h2{font-size:15px;margin:0 0 4px}"
             ".m{color:#647380;font-size:12.5px}.t{white-space:pre-wrap;background:#f7f9fb;"
             "padding:10px;border-radius:4px;font-size:12.5px;max-height:340px;overflow:auto}"
             ".w{background:#fff5f5;border-left-color:#c53030}"
             "mark{background:#fbd38d}</style>",
             f"<header><b>Tiefenlauf — Stichprobe</b> · {len(proben)} Projektseiten · "
             f"jede Zeile mit dem Text, aus dem sie stammt</header>"]
    for i, p in enumerate(proben, 1):
        klasse = " class='w'" if p["hinweis"] else ""
        text = e(p["text"] or "(kein Text)")
        if p["ort"]:
            import re
            text = re.sub(f"({re.escape(p['ort'])})", r"<mark>\1</mark>", text, flags=re.I)
        teile.append(
            f"<article{klasse}><h2>{i}. {e(p['titel'] or '')}</h2>"
            f"<div class='m'>{e(p['domain'])} · <a href='{e(p['url'])}'>{e(p['url'])}</a></div>"
            f"<div class='m'><b>erkannt:</b> {e(str(p['ort']))} ({e(str(p['region']))}) · "
            f"<b>Beleg:</b> {e(str(p['beleg']))}"
            + (f" · <b>ACHTUNG:</b> {e(p['hinweis'])}" if p["hinweis"] else "")
            + f"</div><div class='t'>{text}</div></article>")
    Path(pfad).write_text("\n".join(teile), encoding="utf-8")
    return pfad


if __name__ == "__main__":
    n, domain, html = 25, None, False
    for a in sys.argv[1:]:
        if a.startswith("--n="):
            n = int(a.split("=", 1)[1])
        elif a.startswith("--domain="):
            domain = a.split("=", 1)[1]
        elif a == "--html":
            html = True
    proben = stichprobe(n=n, domain=domain)
    if not proben:
        print("Keine Treffer — laeuft der Tiefenlauf schon?")
        raise SystemExit(1)
    if html:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        p = als_html(proben, str(config.OUTPUT_DIR / "tiefenlauf_stichprobe.html"))
        print("geschrieben:", p)
    else:
        print(als_text(proben))
    warn = [p for p in proben if p["hinweis"]]
    print(f"\n{len(proben)} Seiten geprueft, {len(warn)} mit Hinweis.")
