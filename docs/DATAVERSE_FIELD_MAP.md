# AdWatch ⇄ Dataverse `account` — Feld-Mapping

Stand 2026-08-05. **Verifiziert** an einem echten Account-Datensatz
(`accounts?$top=1`, alle Felder). Keine Vermutungen mehr in Teil 1–4.

Referenzdatensatz: `IB Segelbacher GmbH`, `accountid`
`a7dbc4f6-a2f9-40cc-beb9-0000e0ee6272` — ⚠ ein **deaktivierter** Account
(`statecode: 1`, Name beginnt mit „DEAKTIV"), deshalb sind viele Werte `null`.
Die **Feldnamen** sind damit belegt, die **Füllstände** noch nicht.

---

## 1. Identität und Schlüssel — alle belegt

| `Company` | Dataverse `account` | Beispielwert |
|---|---|---|
| `crm_id` | `accountid` | `a7dbc4f6-…` |
| `crm_modified_on` | `modifiedon` | `2025-07-28T12:48:55Z` |
| `name` | `name` | `DEAKTIV IB Segelbacher GmbH …` |
| `sap_number` | **`accountnumber`** | `0005009967` (10-stellig, gepolstert) |

**SAP-Nummer geklärt:** liegt direkt auf `account`, kein `sl_debitor`-Join nötig.
Zwei Varianten — `accountnumber` = `0005009967` (gepolstert),
`slx_accountnumber_short` = `5009967` (ohne Nullen). Unsere lokalen Werte sind
gepolstert → **`accountnumber` nehmen**. (`_sl_debitorid_value` existiert
zusätzlich als Lookup, brauchen wir nicht.)

## 2. Adresse und Kontakt — alle belegt, wie vorhergesagt

| `Company` | Dataverse | Beispielwert |
|---|---|---|
| `street` | `address1_line1` | `Vogelherdbogen 27` |
| `postal_code` | `address1_postalcode` | `88069` |
| `city` | `address1_city` | `Tettnang` |
| `country` | `address1_country` | `Deutschland` |
| `phone` | `telephone1` | `+4975428292` |
| `email` | `emailaddress1` | `info@ib-segelbacher.de` |
| `fax` | `fax` | `+49 7542 53294` |
| `website_domain` | `websiteurl` | `null` |

⚠ `country` ist **Freitext auf Deutsch** („Deutschland"), kein ISO-Code — der
lokale `_country_code()`-Mapper bleibt zwingend nötig. Genau der Grund, warum 982
spanische Firmen zuerst als DE landeten.

Bonus: `address1_latitude` / `address1_longitude` sind gefüllt (47.65818 / 9.58557)
→ **Geo-Clustering ohne Geocoding-Dienst möglich.**

## 3. Klassifizierung — alle belegt

| `Company` | Dataverse | Beispielwert |
|---|---|---|
| `segment` | **`sl_customer_segment`** | `102` |
| `sub_segment` | **`sl_customer_sub_segment`** | `102002` |
| `sales_channel` | **`sl_sales_channel`** | `102690003` |
| `kv` | `_ownerid_value` (Lookup → `systemuser`) | `f3922506-…` |

**Vertriebsweg-Konflikt geklärt:** `account` hat ein **eigenes**
`sl_sales_channel`-Feld, mit demselben Option-Set wie `opportunity`
(102690003 = Architektenberatung — passend, die Firma ist ein Ingenieurbüro).
Es gibt also beides: Kanal je Firma **und** je Deal. Für Firmen-ICPs das
Account-Feld, für Deal-Analysen das Opportunity-Feld.

**KV:** kein `sl_kv`-Feld vorhanden. Unsere Werte („Gimenez, Juan") sind
Personennamen → der Export hat `ownerid` aufgelöst. Über die API kommt nur die
GUID; für den Namen braucht es `$expand=ownerid($select=fullname)` **oder** den
PA-Konnektor, der `_ownerid_value@…FormattedValue` mitliefert.

**Picklists:** kommen als Integer. Der PA-Dataverse-Konnektor liefert Labels
automatisch als `FormattedValue` mit — mit rohem HTTP bräuchten wir ein
Option-Set-Mapping. Praktisches Argument für den PA-Transport.

## 4. Umsatz — geklärt, und das war das größte Risiko

| `Company` | Dataverse |
|---|---|
| `revenue_y0` | **`slx_revenue_current_year`** |
| `revenue_y1` | **`slx_revenue_current_year_1`** |
| `revenue_y2` | **`slx_revenue_current_year_2`** |
| `revenue_y3` | **`slx_revenue_current_year_3`** |
| `revenue_y4` | **`slx_revenue_current_year_4`** |

**Die Felder existieren als echte Account-Spalten.** Genau fünf, genau passend zu
unseren fünf. Damit ist das Worst-Case-Szenario („Umsatz nur im Export berechnet,
per API nicht verfügbar") **ausgeschlossen** — der Umsatz, also die Zielvariable
der ICP, ist synchronisierbar.

⚠ **Noch offen:** im Referenzdatensatz sind alle fünf `null` — plausibel, weil der
Account deaktiviert ist und `revenue: 0.0000` hat. **Vor dem Sync an einem aktiven
Händler mit bekanntem Umsatz gegenprüfen** (Abfrage unten). Es gibt zusätzlich die
Standard-Dynamics-Felder `revenue` und `openrevenue` — nicht verwechseln, das sind
andere Größen.

## 5. Felder, die wir teuer anreichern — und die es in CRM schon gibt ⚠

**Wirtschaftlich der wichtigste Fund.** Diese Spalten existieren auf `account`,
und wir bezahlen Haiku dafür, sie von Websites zu extrahieren:

| Lokal (angereichert) | Dataverse-Feld |
|---|---|
| `employee_hint` | `numberofemployees` |
| `founded_year` | `sl_founding_date` |
| `legal_form` (in `CompanyEnrichment`) | `sl_corporate_form` |

Im Referenzdatensatz alle `null` — **Füllstand also unbedingt prüfen, bevor weiter
angereichert wird.** Sind sie gepflegt, sparen wir die Anreicherung für genau die
Merkmale, die der ICP heute fehlen (Betriebsgröße 3 %, Firmenalter 7 % Abdeckung).
Sind sie leer, ist die Anreicherung bestätigt richtig.

## 6. CRM-Felder, die wir noch nicht nutzen — und sollten 【ICP】

| Feld | Beispielwert | Warum relevant |
|---|---|---|
| **`sl_customer_or_prospect`** | `102690001` | **Trennt Kunde von Interessent.** Direkt gegen unser Kernproblem: die ICP kann nicht ranken, weil die Basisquote bei Händlern 87 % ist — es fehlen Negativbeispiele. Dieses Feld liefert sie evtl. schon. |
| `sl_target_customer` | `false` | Ein **Zielkunden-Flag im CRM** — genau das, was unser `target_score` berechnet. Vergleich = externe Validierung. |
| `sl_active_partner` + `sl_active_partner_since` | `false` / `null` | Partnerprogramm-Status und -Dauer |
| `sl_key_account` | `false` | Key-Account-Kennzeichnung |
| `sl_cero_partner`, `sl_bifolding_door` | `false` | **Produkt-Partnerstatus** → Cross-Sell ohne Showroom-Join |
| `sl_showroom`, `sl_showroom_size` | `null` | Ausstellung direkt am Account |
| `ax_top_attributes` | `"Statikbüro, Bauphysik"` | **Freitext-Geschäftsmerkmale** — fachlich näher als unsere Website-Ableitung |
| `slx_architectclassification` | `809202003` | Architekten-Klassifizierung |
| `statecode` / `statuscode` | `1` / `102690000` | **Aktiv/deaktiviert** — s. Warnung unten |

## 7. Rein lokale Felder — CRM darf sie NIE überschreiben

`description` · `products` · `enrichment_status` · `page_id` · `page_name` ·
`page_url` · `resolution_status` · `candidates` · `fit_score` ·
`opportunity_score` · `target_score` · `fit_breakdown` · `scores_updated_at` ·
`is_intercompany` · `customer_state` (abgeleitet) · `imported_at`

`employee_hint` / `founded_year` sind **Grenzfälle** (s. Abschnitt 5): ist das
CRM-Feld gefüllt, gewinnt CRM; ist es leer, bleibt unsere Anreicherung stehen.

**Regel:** CRM gewinnt bei Stammdaten (1–4). AdWatch gewinnt bei 7. Dieselbe
Disziplin, die die Anreicherung schon hat.

## 8. ⚠ Warnung: deaktivierte Accounts

Der Referenzdatensatz hat `statecode: 1` (inaktiv) und „DEAKTIV" im Namen. Unsere
lokale Basis enthält solche Datensätze also mit. Der Sync sollte `statecode`
mitziehen und deaktivierte Firmen aus Zielisten ausschließen — sonst stehen
deaktivierte Accounts auf der Anrufliste. **Kein Löschen**, nur kennzeichnen.

## 9. Nächste Abfragen

Alles auf **einer** Zeile, einfache `'`, `LogicalName` case-sensitive.

Umsatzfelder an einem **aktiven** Händler prüfen:
```
https://slxcrowd.crm4.dynamics.com/api/data/v9.2/accounts?$top=3&$filter=statecode eq 0 and slx_revenue_current_year ne null&$select=name,accountnumber,slx_revenue_current_year,slx_revenue_current_year_1,sl_customer_segment,sl_sales_channel
```

Füllstand der Felder, die wir anreichern:
```
https://slxcrowd.crm4.dynamics.com/api/data/v9.2/accounts?$top=5&$filter=numberofemployees ne null&$select=name,numberofemployees,sl_founding_date,sl_corporate_form
```

Gibt es Interessenten (= die fehlenden Negativbeispiele)?
```
https://slxcrowd.crm4.dynamics.com/api/data/v9.2/accounts?$top=3&$filter=sl_customer_or_prospect ne 102690001&$select=name,sl_customer_or_prospect,sl_customer_segment,slx_revenue_current_year
```

## 10. Bilanz

| | Felder |
|---|---|
| ✅ **belegt** (Name am echten Datensatz bestätigt) | **21 von 21** |
| ⚠ Füllstand noch zu prüfen | Umsatz (5), Größe/Alter/Rechtsform (3) |
| lokal, nie aus CRM | 16 |

**Das Mapping ist vollständig.** Alle drei roten Risiken sind aufgelöst:
SAP-Nummer liegt auf `account`, `sl_sales_channel` existiert account-seitig,
und die Umsatzfelder sind echte, abfragbare Spalten.
