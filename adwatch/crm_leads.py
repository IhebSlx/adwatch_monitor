"""Leads aus dem CRM holen — lesend, seitenweise.

WARUM ÜBERHAUPT. Gemessen am 2026-08-19: 94.219 der 438.979 angehängten
E-Mails — 21,5 % — hängen an Leads, verteilt auf 42.157 verschiedene. Ein
Fünftel der gesamten Korrespondenz zeigte auf Datensätze, die es in unserem
Spiegel nicht gab. Das war der Auslöser, nicht eine Vollständigkeitsidee.

DER ABRUF KIPPT HIER ANDERS ALS BEI DEN E-MAILS, und das bestimmt die Bauform:

* E-Mails scheiterten an der ANTWORTGRÖSSE (HTML-Rümpfe), deshalb dort feste
  kleine Fenster von 7 Tagen.
* Leads sind schmale Zeilen ohne Rumpf. Gemessen: ein Monat 2.465 Zeilen in
  2 Sekunden, ein Quartal reißt den 5.000er-Deckel. Hier bindet also die
  ZEILENZAHL — und gegen die ist Halbieren das richtige Mittel, weil es sich an
  die tatsächliche Dichte anpasst statt an eine geratene Konstante.

KEINE PERSONENDATEN. `firstname`, `lastname`, `emailaddress*` und `telephone*`
stehen in Dataverse und werden bewusst nicht geholt. Gespeichert wird nur, was
die FIRMA beschreibt.

Nur Lesen. Es wird nie nach Dataverse geschrieben.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from sqlalchemy import func, select

from . import flows
from .db import SessionLocal
from .models import Company, CrmLead

log = logging.getLogger("adwatch.crm_leads")

PAGE_CAP = 5000          # harte Grenze des Flows
_PAUSE_S = 0.3

# Firmenbezogene Felder. Bewusst ohne Namen, Mail und Telefon der Person.
SELECT = ("leadid,companyname,subject,websiteurl,address1_city,address1_postalcode,"
          "address1_country,statecode,statuscode,createdon,modifiedon,leadsourcecode,"
          "industrycode,revenue,numberofemployees,_parentaccountid_value,_ownerid_value,"
          "leadqualitycode")
_FV = "@OData.Community.Display.V1.FormattedValue"


def _rows(body) -> list[dict]:
    """Der Flow liefert für `leads` ein nacktes Array, für `accounts` ein
    {value: [...]}. Beides annehmen, statt sich auf eine Form zu verlassen."""
    if isinstance(body, list):
        return body
    return (body or {}).get("value") or []


def _dt(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _fetch(start: dt.date, end: dt.date) -> list[dict]:
    payload = {"entity": "leads", "select": SELECT, "top": PAGE_CAP,
               "filter": (f"createdon ge {start:%Y-%m-%d}T00:00:00Z and "
                          f"createdon lt {end:%Y-%m-%d}T00:00:00Z")}
    return _rows(flows.post("crm_query", payload))


def _walk(start: dt.date, end: dt.date, out: list, depth: int = 0) -> None:
    """Fenster holen; am Deckel halbieren, bis es passt.

    Ein Deckeltreffer bei einem EINZELNEN Tag wird protokolliert statt still
    abgeschnitten. Eine unbemerkte Kappung wäre hier besonders tückisch, weil
    das Ergebnis vollständig AUSSIEHT."""
    got = _fetch(start, end)
    if len(got) < PAGE_CAP:
        out.extend(got)
        return
    if (end - start).days <= 1:
        log.warning("Leads: Deckel schon an einem Tag (%s), %d Zeilen — es fehlen welche",
                    start, len(got))
        out.extend(got)
        return
    mitte = start + (end - start) / 2
    time.sleep(_PAUSE_S)
    _walk(start, mitte, out, depth + 1)
    time.sleep(_PAUSE_S)
    _walk(mitte, end, out, depth + 1)


def _company_resolver(s):
    """Lead auf Firma auflösen — ausschließlich über die im CRM gesetzte
    Mutterfirma.

    Bewusst NICHT über Namensähnlichkeit: ein Lead heißt „Fenster Meier" und
    unsere Firma „Meier Fenster- und Türenbau GmbH" — ob das dieselbe Firma ist,
    entscheidet kein Stringvergleich. Wo ein Mensch im CRM die Verknüpfung
    gesetzt hat, ist sie verlässlich; wo nicht, bleibt das Feld leer und die
    Frage sichtbar offen, statt geraten zu werden."""
    by_crm = {c: i for c, i in s.execute(
        select(Company.crm_id, Company.id).where(Company.crm_id.is_not(None)))}

    def resolve(parent_crm_id):
        return by_crm.get(parent_crm_id) if parent_crm_id else None
    return resolve


def sync(since: dt.date | None = None, until: dt.date | None = None) -> dict:
    """Leads eines Zeitraums holen und ablegen. Idempotent: `lead_id` ist
    eindeutig, ein zweiter Lauf aktualisiert statt zu doppeln."""
    since = since or dt.date(2016, 1, 1)
    until = until or (dt.date.today() + dt.timedelta(days=1))
    raw: list[dict] = []
    _walk(since, until, raw)
    log.info("Lead-Abruf: %d Zeilen aus %s..%s", len(raw), since, until)

    inserted = updated = 0
    with SessionLocal() as s:
        resolve = _company_resolver(s)
        have = {a: i for a, i in s.execute(select(CrmLead.lead_id, CrmLead.id))}
        for r in raw:
            lid = r.get("leadid")
            if not lid:
                continue
            vals = dict(
                company_name=(r.get("companyname") or None),
                subject=(r.get("subject") or None),
                website=(r.get("websiteurl") or None),
                city=(r.get("address1_city") or None),
                postal_code=(r.get("address1_postalcode") or None),
                country=(r.get("address1_country") or None),
                statecode=r.get("statecode"),
                state_label=r.get("statecode" + _FV),
                statuscode=r.get("statuscode"),
                status_label=r.get("statuscode" + _FV),
                lead_source=r.get("leadsourcecode" + _FV),
                industry=r.get("industrycode" + _FV),
                quality=r.get("leadqualitycode" + _FV),
                revenue=_num(r.get("revenue")),
                employees=r.get("numberofemployees"),
                parent_account_crm_id=r.get("_parentaccountid_value"),
                owner_id=r.get("_ownerid_value"),
                created_on=_dt(r.get("createdon")),
                modified_on=_dt(r.get("modifiedon")),
                synced_at=dt.datetime.utcnow(),
            )
            vals["company_id"] = resolve(vals["parent_account_crm_id"])
            if lid in have:
                row = s.get(CrmLead, have[lid])
                for k, v in vals.items():
                    setattr(row, k, v)
                updated += 1
            else:
                s.add(CrmLead(lead_id=lid, **vals))
                inserted += 1
        s.commit()
    return {"fetched": len(raw), "inserted": inserted, "updated": updated,
            "since": str(since), "until": str(until)}


def stats() -> dict:
    """Was liegt da — und wie viel davon hängt an einer bekannten Firma?"""
    with SessionLocal() as s:
        total = s.scalar(select(func.count(CrmLead.id))) or 0
        linked = s.scalar(select(func.count(CrmLead.id))
                          .where(CrmLead.company_id.is_not(None))) or 0
        by_state = dict(s.execute(
            select(CrmLead.state_label, func.count(CrmLead.id))
            .group_by(CrmLead.state_label)).all())
        by_source = dict(s.execute(
            select(CrmLead.lead_source, func.count(CrmLead.id))
            .group_by(CrmLead.lead_source)
            .order_by(func.count(CrmLead.id).desc()).limit(10)).all())
    return {"total": total, "mit_firma": linked, "nach_status": by_state,
            "nach_herkunft": by_source}
