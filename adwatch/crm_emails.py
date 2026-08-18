"""Angehängte E-Mail-Korrespondenz aus dem CRM holen — lesend, seitenweise.

Die strukturierten Felder sagen DASS ein Objekt verloren ging; die angehängte
Korrespondenz sagt WARUM. Gemessen 2026-08-18: jede Mail einer 300er-Stichprobe
hängt an einem Datensatz (150 Verkaufschance, 99 Firma, 43 Angebot, 8 Lead) —
es ist Projektakte, nicht Postfach.

ZWEI DINGE, DIE DEN ABRUF BESTIMMEN, beide gemessen statt geschätzt:

1. Der Flow kappt bei 5.000 Zeilen, und selbst ein einzelner MONAT erreicht
   diesen Deckel. Feste Fenster reichen also nicht. `_walk` halbiert ein
   Zeitfenster so lange, bis es unter den Deckel passt — und protokolliert, wenn
   selbst ein einzelner Tag ihn erreicht, statt stillschweigend abzuschneiden.
   Eine unbemerkte Kappung wäre hier besonders tückisch, weil das Ergebnis
   vollständig AUSSIEHT.

2. Die Textkörper sind HTML, und nur 12–24 % der Rohbytes sind Inhalt. Gespeichert
   wird der von Markup befreite Text — das IST das Gespräch, der Rest ist
   Formatierung und Signatur-Auszeichnung. `body_raw_chars` hält die Rohlänge
   fest, damit später nachvollziehbar bleibt, was entfernt wurde.

Nur Lesen. Es wird nie nach Dataverse geschrieben.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

from sqlalchemy import select

from . import flows
from .db import SessionLocal
from .models import Company, CrmEmail, CrmOpportunity

log = logging.getLogger("adwatch.crm_emails")

PAGE_CAP = 5000          # harte Grenze des Flows
# Nicht die Zeilenzahl begrenzt uns, sondern die Antwortgroesse: E-Mail-Rumpfe
# sind HTML. Eine Woche (~2.800 Zeilen, ~23 MB) laeuft gemessen sauber durch,
# ein Monat kippte den Flow mit HTTP 504.
MAX_SPAN_DAYS = 7
_PAUSE_S = 0.5           # Hoeflichkeitspause zwischen Abrufen
_LOOKUP_ANN = "_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname"

SELECT = ("activityid,_regardingobjectid_value,createdon,senton,directioncode,"
          "statecode,subject,description")

_TAG = re.compile(r"<[^>]+>")
_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def to_text(html: str | None) -> str:
    """HTML zu lesbarem Text. Blockgrenzen werden zu Zeilenumbrüchen, damit ein
    zitierter Antwortverlauf nicht zu einem einzigen Absatz verschmilzt."""
    if not html:
        return ""
    s = _STYLE.sub(" ", html)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", s, flags=re.I)
    s = _TAG.sub(" ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&auml;", "ä"), ("&ouml;", "ö"),
                 ("&uuml;", "ü"), ("&szlig;", "ß"), ("&Auml;", "Ä"),
                 ("&Ouml;", "Ö"), ("&Uuml;", "Ü")):
        s = s.replace(a, b)
    s = _WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return _NL.sub("\n\n", s).strip()


def _rows(body) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("value") or []
    return []


def _fetch(start: dt.date, end: dt.date) -> list[dict]:
    flt = (f"createdon ge {start.isoformat()}T00:00:00Z and "
           f"createdon lt {end.isoformat()}T00:00:00Z")
    out = _rows(flows.post("crm_query", {"entity": "emails", "select": SELECT,
                                         "filter": flt, "top": PAGE_CAP},
                           timeout=180))
    time.sleep(_PAUSE_S)
    return out


def _walk(start: dt.date, end: dt.date, out: list[dict], depth: int = 0) -> None:
    """Fenster holen; bei erreichtem Deckel halbieren und beide Hälften erneut.

    Der Deckel ist nicht von echten 5.000 Zeilen zu unterscheiden, also wird bei
    Gleichstand IMMER geteilt. Lieber eine Abfrage zu viel als ein stilles Loch.

    WICHTIG — von KLEIN nach groß, nicht umgekehrt: der erste Entwurf fragte ein
    ganzes Jahr an und halbierte erst bei Deckel-Treffer. Das scheiterte
    vollständig (HTTP 504, danach abgerissene Verbindungen), weil nicht die
    ZEILENZAHL das Problem ist, sondern die ANTWORTGRÖSSE — E-Mail-Rümpfe sind
    HTML, ein Jahr wären Hunderte Megabyte in einer Antwort. Ein Fenster wird
    deshalb nie größer als MAX_SPAN_DAYS angefragt; die gemessene Probewoche
    (2.834 Zeilen, rund 23 MB) lief problemlos.
    """
    span = (end - start).days
    if span > MAX_SPAN_DAYS:
        cur = start
        while cur < end:
            nxt = min(cur + dt.timedelta(days=MAX_SPAN_DAYS), end)
            _walk(cur, nxt, out, depth + 1)
            cur = nxt
        return

    got = _fetch(start, end)
    if len(got) < PAGE_CAP:
        out.extend(got)
        return
    if span <= 1:
        # Selbst ein Tag ist voll — hier ist ohne Sortierung/Skiptoken nichts
        # mehr zu holen. Das wird LAUT vermerkt, nicht verschwiegen.
        log.warning("E-Mail-Abruf: %s hat >= %d Zeilen an EINEM Tag — Rest nicht "
                    "abrufbar (Flow kappt, keine Sortierung erlaubt)", start, PAGE_CAP)
        out.extend(got)
        return
    mid = start + dt.timedelta(days=span // 2)
    _walk(start, mid, out, depth + 1)
    _walk(mid, end, out, depth + 1)


def _company_resolver(s):
    """Bezug -> unsere Firmen-Id. Firmen direkt, Verkaufschancen über ihren
    Auftraggeber. Angebote und Leads bleiben ohne Firma — wir halten weder
    Angebote (HTTP 403 im CRM) noch Leads, und ein falsch geratener Bezug wäre
    schlimmer als ein leeres Feld."""
    by_crm = {c: i for c, i in s.execute(
        select(Company.crm_id, Company.id).where(Company.crm_id.is_not(None)))}
    opp_owner = {o: p for o, p in s.execute(
        select(CrmOpportunity.opportunity_guid, CrmOpportunity.parent_account_crm_id)
        .where(CrmOpportunity.opportunity_guid.is_not(None)))}

    def resolve(rid: str | None, rtype: str | None) -> int | None:
        if not rid:
            return None
        low = (rid or "").lower()
        if rtype == "account":
            return by_crm.get(rid) or by_crm.get(low)
        if rtype == "opportunity":
            owner = opp_owner.get(rid) or opp_owner.get(low)
            return by_crm.get(owner) if owner else None
        return None
    return resolve


def sync(since: dt.date | None = None, until: dt.date | None = None) -> dict:
    """Alle angehängten E-Mails eines Zeitraums holen und ablegen. Idempotent:
    `activity_id` ist eindeutig, ein zweiter Lauf aktualisiert statt zu doppeln."""
    since = since or dt.date(2019, 1, 1)
    until = until or (dt.date.today() + dt.timedelta(days=1))
    raw: list[dict] = []
    _walk(since, until, raw)
    log.info("E-Mail-Abruf: %d Zeilen aus %s..%s", len(raw), since, until)

    inserted = updated = 0
    with SessionLocal() as s:
        resolve = _company_resolver(s)
        have = {a: i for a, i in s.execute(
            select(CrmEmail.activity_id, CrmEmail.id))}
        for r in raw:
            aid = r.get("activityid")
            if not aid:
                continue
            body = r.get("description") or ""
            vals = dict(
                regarding_id=r.get("_regardingobjectid_value"),
                regarding_type=r.get(_LOOKUP_ANN),
                created_on=_dt(r.get("createdon")),
                sent_on=_dt(r.get("senton")),
                direction=("ausgehend" if r.get("directioncode") else "eingehend"),
                statecode=r.get("statecode"),
                subject=(r.get("subject") or None),
                body_text=to_text(body) or None,
                body_raw_chars=len(body) or None,
                synced_at=dt.datetime.utcnow(),
            )
            vals["company_id"] = resolve(vals["regarding_id"], vals["regarding_type"])
            if aid in have:
                row = s.get(CrmEmail, have[aid])
                for k, v in vals.items():
                    setattr(row, k, v)
                updated += 1
            else:
                s.add(CrmEmail(activity_id=aid, **vals))
                inserted += 1
        s.commit()
    return {"fetched": len(raw), "inserted": inserted, "updated": updated,
            "since": str(since), "until": str(until)}


def _dt(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def stats() -> dict:
    """Was liegt da — und wie viel davon ist an eine Firma gebunden?"""
    from sqlalchemy import func
    with SessionLocal() as s:
        total = s.scalar(select(func.count(CrmEmail.id))) or 0
        linked = s.scalar(select(func.count(CrmEmail.id))
                          .where(CrmEmail.company_id.is_not(None))) or 0
        by_type = dict(s.execute(
            select(CrmEmail.regarding_type, func.count(CrmEmail.id))
            .group_by(CrmEmail.regarding_type)).all())
        by_dir = dict(s.execute(
            select(CrmEmail.direction, func.count(CrmEmail.id))
            .group_by(CrmEmail.direction)).all())
        chars = s.scalar(select(func.sum(func.length(CrmEmail.body_text)))) or 0
        firms = s.scalar(select(func.count(func.distinct(CrmEmail.company_id)))
                         .where(CrmEmail.company_id.is_not(None))) or 0
    return {"total": total, "mit_firma": linked, "firmen": firms,
            "nach_bezug": by_type, "nach_richtung": by_dir,
            "textzeichen": int(chars)}


# ---------------------------------------------------------------------------
# Merkmale aus der Korrespondenz — der eigentliche Zweck des Abrufs
# ---------------------------------------------------------------------------
# Gemessen 2026-08: beschreibende Merkmale (was eine Firma IST) bringen +0,03
# AUC, verhaltensbezogene (was zwischen uns und ihr GESCHEHEN ist) +0,14 bis
# +0,16. Korrespondenz ist Verhalten in Reinform.
#
# Das schärfste Feld ist die RICHTUNG. Eine eingehende Mail heißt: die Firma
# meldet sich bei UNS. Das ist Nachfrage, nicht Vertriebsaufwand — und der
# Unterschied ist genau der, den ein Neigungsmodell sonst verwechselt. In der
# Probewoche waren 45 % eingehend (die frühere Stichprobe zeigte 1,3 %, weil sie
# auf abgeschlossene Mails gefiltert war — ein Beispiel dafür, wie ein Filter
# ein Merkmal wertlos aussehen lassen kann).
#
# ALLE Merkmale sind stichtagsfähig: `until` schneidet hart ab, damit ein Modell
# nie Korrespondenz sieht, die nach dem vorhergesagten Ereignis entstand.

def features(company_ids: list[int] | None = None,
             until: dt.date | None = None) -> dict[int, dict]:
    """Korrespondenz-Merkmale je Firma, strikt vor `until`."""
    from sqlalchemy import and_ as _and

    with SessionLocal() as s:
        stmt = select(CrmEmail.company_id, CrmEmail.direction, CrmEmail.created_on,
                      CrmEmail.body_text).where(CrmEmail.company_id.is_not(None))
        if until:
            stmt = stmt.where(CrmEmail.created_on < dt.datetime.combine(
                until, dt.time.min))
        if company_ids:
            stmt = stmt.where(CrmEmail.company_id.in_(company_ids))
        rows = list(s.execute(stmt))

    ref = dt.datetime.combine(until, dt.time.min) if until else dt.datetime.utcnow()
    agg: dict[int, dict] = {}
    for cid, direction, created, body in rows:
        a = agg.setdefault(cid, {"mails": 0, "eingehend": 0, "ausgehend": 0,
                                 "letzte": None, "erste": None, "zeichen": 0})
        a["mails"] += 1
        a["eingehend" if direction == "eingehend" else "ausgehend"] += 1
        a["zeichen"] += len(body or "")
        if created:
            a["letzte"] = created if a["letzte"] is None else max(a["letzte"], created)
            a["erste"] = created if a["erste"] is None else min(a["erste"], created)

    out: dict[int, dict] = {}
    for cid, a in agg.items():
        n = a["mails"]
        out[cid] = {
            "mails": n,
            "eingehend": a["eingehend"],
            "ausgehend": a["ausgehend"],
            # Anteil statt Anzahl: eine kleine Firma mit 2 von 3 eingehenden
            # Mails ist interessanter als ein Großkunde mit 5 von 400.
            "eingehend_anteil": round(a["eingehend"] / n, 3) if n else 0.0,
            "hat_eingehend": 1 if a["eingehend"] else 0,
            "tage_seit_letzter": ((ref - a["letzte"]).days
                                  if a["letzte"] else None),
            "dauer_tage": ((a["letzte"] - a["erste"]).days
                           if a["letzte"] and a["erste"] else 0),
            "zeichen_schnitt": round(a["zeichen"] / n) if n else 0,
        }
    return out


def for_company(company_id: int, limit: int = 50) -> list[dict]:
    """Der Schriftverkehr einer Firma, neueste zuerst — für das Dossier.

    Der Text wird auf einen Anriss gekürzt: die Akte soll im Dossier lesbar
    sein, nicht die Seite fluten. Wer den ganzen Verlauf braucht, hat ihn im
    CRM, wo er hingehört."""
    with SessionLocal() as s:
        rows = s.execute(
            select(CrmEmail).where(CrmEmail.company_id == company_id)
            .order_by(CrmEmail.created_on.desc()).limit(limit)).scalars().all()
        return [{
            "id": e.id, "betreff": e.subject, "richtung": e.direction,
            "datum": e.created_on.isoformat() if e.created_on else None,
            "bezug": e.regarding_type,
            "anriss": (e.body_text or "")[:400],
            "zeichen": len(e.body_text or ""),
        } for e in rows]
