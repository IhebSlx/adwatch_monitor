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
