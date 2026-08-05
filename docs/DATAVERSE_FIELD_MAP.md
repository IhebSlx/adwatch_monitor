# AdWatch ⇄ Dataverse `account` — Feld-Mapping

Stand 2026-08-05. Ziel: jede Spalte in `Company` (lokal) auf ihr Dataverse-Feld
abbilden, damit CRM die Excel-Datei als Quelle ersetzen kann.

**Beweisgrade — bitte ernst nehmen:**

| Marke | Bedeutung |
|---|---|
| ✅ **belegt** | Gegen echte Daten verifiziert, funktioniert bereits |
| 🟢 **sicher** | Standard-Dynamics-Feld; der Excel-Spaltenname IST der Anzeigename dieses Feldes |
| 🟡 **wahrscheinlich** | Solarlux-Custom-Feld, Name aus Konvention abgeleitet — **muss abgefragt werden** |
| 🔴 **unbekannt / Risiko** | Existiert evtl. gar nicht als Account-Feld |

`datenlandschaft_inventar.md` (2026-07-29) kartiert `opportunity` vollständig,
`account` aber ausdrücklich **nicht** ("NICHT kartiert — für ICP zwingend
nachzuholen: `account`: keine Spalte, keine Klassifizierung"). Genau diese Tabelle
brauchen wir. Die 🟡/🔴-Zeilen unten sind deshalb offen und mit **einer** Abfrage
zu klären (siehe unten).

---

## 1. Identität und Schlüssel

| `Company` | Dataverse `account` | Grad | Anmerkung |
|---|---|---|---|
| `crm_id` | `accountid` | ✅ **belegt** | Excel-Spalte „(Nicht ändern) Firma". 4.609/4.618 gematcht, 0 Duplikate. Der durable Key. |
| `crm_modified_on` | `modifiedon` | ✅ **belegt** | Excel-Spalte „(Nicht ändern) Geändert am". Werte 2022-10-15 … 2026-07-27 = echte CRM-Zeitstempel, **nicht** Importzeit. Watermark für den Delta-Sync. |
| `sap_number` | ❓ evtl. **nicht auf `account`** | 🔴 | Das Inventar nennt eine eigene Tabelle `sl_debitor` = „SAP-Debitor/Kundennummern". Wenn die SAP-Nummer dort liegt, ist es ein **1:n-Join**, kein Account-Feld — eine Firma kann mehrere Debitoren haben. **Vor dem Sync klären.** |
| `name` | `name` | 🟢 sicher | Excel „Firmenname". |

## 2. Adresse — Standard-Dynamics, hohe Sicherheit

Der Excel-Header **„Adresse 1: Postleitzahl"** ist der Beweis: das ist der
deutsche Anzeigename von `address1_postalcode`. Der Export nutzt also die
Standard-`address1_*`-Felder, nicht Custom-Felder.

| `Company` | Dataverse `account` | Grad |
|---|---|---|
| `street` | `address1_line1` | 🟢 sicher |
| `postal_code` | `address1_postalcode` | 🟢 **sicher** (Header-Beweis) |
| `city` | `address1_city` | 🟢 sicher |
| `country` | `address1_country` | 🟢 sicher |
| `phone` | `telephone1` | 🟢 sicher (Excel „Telefon 1") |
| `email` | `emailaddress1` | 🟢 sicher |
| `fax` | `fax` | 🟢 sicher |
| `website_domain` | `websiteurl` | 🟢 sicher |

⚠ `country` kommt als **Freitext** („Spanien", „Deutschland"), nicht als ISO-Code.
Der lokale `_country_code()`-Mapper bleibt deshalb nötig — genau der Grund, warum
982 spanische Firmen zuerst als DE importiert wurden.

## 3. Klassifizierung — Solarlux-Custom, ALLE offen

| `Company` | Vermutetes Feld | Grad | Problem |
|---|---|---|---|
| `segment` | `sl_customersegment`? | 🟡 | Excel „Kundensegment". Picklist. Logischer Name geraten. |
| `sub_segment` | `sl_customersubsegment`? | 🟡 | Excel „Kundenuntersegment". Picklist. |
| `kv` | `sl_kv`? / `ownerid`? | 🟡 | Excel „KV". Könnte ein Textfeld ODER ein `systemuser`-Lookup sein. Wenn Lookup → Join nötig. |
| `sales_channel` | `sl_sales_channel`? | 🔴 **Konflikt** | Das Inventar belegt `sl_sales_channel` auf **`opportunity`**, nicht auf `account` — und wir wissen: *Vertriebsweg ist eine Eigenschaft des Deals*. Trotzdem steht in der Excel-Datei ein Wert **pro Firma**. Entweder gibt es ein zweites, account-seitiges Feld, oder der Export hat es abgeleitet. **Das ist die wichtigste offene Frage der Klassifizierung.** |

**Picklists:** Über die Web API kommen Picklists als **Integer** (`102690001`),
nicht als Label. Das Inventar liefert die Lösung: *„Der Dataverse-Konnektor in
Power Automate liefert Picklist-Labels automatisch mit als
`<feld>@OData.Community.Display.V1.FormattedValue`."* → **Ein weiteres starkes
Argument für den Power-Automate-Transport**: mit ihm brauchen wir kein
Option-Set-Mapping, mit rohem HTTP schon.

## 4. Umsatz — das größte Risiko

| `Company` | Vermutetes Feld | Grad |
|---|---|---|
| `revenue_y0` … `revenue_y4` | ❓ | 🔴 **unbekannt** |

Excel-Header: „Umsatz aktuelles Jahr", „… -1" bis „… -4". Fünf rollierende
Jahresspalten auf einem Account sind **kein** Standard-Dynamics-Muster. Drei
Möglichkeiten:

1. **Rollup-/Calculated-Fields** auf `account` → per API abfragbar ✅
2. Aus `sl_annual_appraisal` aggregiert (das Inventar: „per-family revenue" +
   `sl_turnover_sales_potential`) → **Join nötig**, nicht ein Feld
3. Nur im Report/Export berechnet → **per API überhaupt nicht verfügbar**

Fall 3 wäre gravierend: der Umsatz ist die **Zielvariable** der ICP und die Basis
von `customer_state` und Divergenz. Träfe er zu, bliebe der Excel-Import für
Umsatz zwingend — der CRM-Sync könnte alles andere aktualisieren, aber nicht das
Wichtigste. **Das ist vor jeder Implementierung zu klären.**

## 5. Rein lokale Felder — CRM darf sie NIE überschreiben

Diese existieren in Dataverse nicht und sind das Eigentum von AdWatch. Der
Feld-Ownership-Map schützt sie:

`description` · `products` · `founded_year` · `employee_hint` ·
`enrichment_status` · `page_id` · `page_name` · `page_url` ·
`resolution_status` · `candidates` · `fit_score` · `opportunity_score` ·
`target_score` · `fit_breakdown` · `scores_updated_at` · `is_intercompany` ·
`customer_state` (abgeleitet) · `imported_at`

**Regel:** CRM gewinnt bei Stammdaten (Abschnitte 1–4). AdWatch gewinnt bei
allem in Abschnitt 5. Genau die Disziplin, die die Anreicherung schon hat
(SAP-/Handeingaben werden nie überschrieben, `manual`-Provenance schlägt den
Extraktor).

---

## 6. Die Abfragen, die das klären

Das Inventar listet dies selbst als offene Abfrage #2. Eine Abfrage beantwortet
fast alles:

```
# Alle Account-Felder mit Typ — beantwortet Abschnitt 3 und 4 auf einmal
/api/data/v9.2/EntityDefinitions(LogicalName='account')/Attributes
  ?$select=LogicalName,AttributeType,IsValidForRead
```

```
# Gezielt: heißt das Umsatzfeld wie vermutet, und ist es gefüllt?
/api/data/v9.2/accounts?$top=1&$select=accountid,name,address1_postalcode,
  telephone1,websiteurl,modifiedon
```

```
# Liegt die SAP-Nummer auf account oder in sl_debitor?
/api/data/v9.2/EntityDefinitions(LogicalName='sl_debitor')/Attributes
  ?$select=LogicalName,AttributeType
```

```
# Hat account ein eigenes Vertriebsweg-Feld?
/api/data/v9.2/EntityDefinitions(LogicalName='account')/Attributes
  ?$filter=contains(LogicalName,'channel') or contains(LogicalName,'segment')
```

## 7. Bilanz

| | Felder |
|---|---|
| ✅ belegt, funktioniert | **2** (die beiden Schlüssel — der wichtigste Teil) |
| 🟢 sicher (Standard-Dynamics) | **9** |
| 🟡 offen (Custom, Name geraten) | **3** |
| 🔴 Risiko (Existenz unklar) | **7** (SAP-Nummer, Vertriebsweg, 5× Umsatz) |
| lokal, nie aus CRM | **18** |

**11 von 21 CRM-Feldern sind belegt oder sicher — inklusive beider Schlüssel.**
Die Adresse, die Kontaktdaten und die Identität können also sofort synchronisiert
werden. Offen sind ausgerechnet die analytisch wertvollsten: Klassifizierung und
Umsatz.
