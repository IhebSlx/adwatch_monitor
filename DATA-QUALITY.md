# Data quality — what was wrong, what was changed, what you must not misread

This file records every deliberate change made to the data between the source
systems and what the app shows. It exists because most of the numbers in AdWatch
are only interpretable if you know which of them are *measured*, which are
*estimated*, and which are *quoted rather than earned*.

Re-runnable repairs live in `adwatch/dataquality.py` — `audit()` reports,
`repair()` applies. Everything there is idempotent.

---

## 1. Where the data comes from

| Source | Contributes | Trust |
|---|---|---|
| Dataverse (CRM) export — Excel | 46.145 Firmen, 57.776 Verkaufschancen, Belege, Angebote | Master data. Authoritative for address, segment, revenue. |
| Dataverse Web API — read-only | `slx_product` line items, entity discovery | Authoritative, and richer than any export. |
| `marktanalyse-2026-08-06.csv` | 469 spanish companies | A colleague's research. No CRM identity. |
| Company websites | description, products, brands, grades | Only as good as the identity check behind it. |
| Meta / Google Ad Libraries | ad activity | Only for companies with a proven page/domain. |

The single most expensive lesson of this project: **an export is not the data.**
Product line items, opportunity addresses and every custom Solarlux table were
invisible for weeks because they were absent from the spreadsheets, not because
they were absent from the CRM.

---

## 2. Problems found and what was done

### 2.1 Enrichment kept on companies whose website was never proven
**117 rows, 295 fields cleared.**

The pipeline writes extracted facts and the identity verdict in one run, so they
normally agree. They drift when a verdict is **revised later**: a domain that
passed once and was demoted to `conflict` by a better check keeps the
description, products and brands it produced.

*D3 Outdoor Girona* still carried a full profile — products, Corradi as an
installed brand — read off `d3barcelona.com`, a site the checker had already
ruled was not theirs. Fifteen further rows sat on `not_found`: a description
with no website at all.

**Changed:** all website-derived fields cleared where `identity_status` is
`conflict`, `not_found` or `unreachable`; `enrichment_status` reset to `none`.
**Deliberately kept:** the domain and the verdict. They are the evidence the
check happened, downstream consumers already exclude `conflict`, and deleting
them would only invite the same wrong domain to be rediscovered tomorrow.

### 2.2 E-mail addresses stored as websites
**1.262 rows normalised, 80 of them containing `@`.**

Values like `info@holz9.com` and `http://am@am2.es` sat in `website_domain`. The
crawler always coped — it normalises before fetching — but the Explorer, the
export and the PDF report render the raw column, so a colleague saw a mailbox
where a website should be and could not tell whether we held one at all.

**Changed:** the column now stores the canonical domain (`holz9.com`).
One row remains unparseable and was left untouched rather than guessed at.

### 2.3 Product "families" that are products, sub-lines or brochures
**Families reduced from 40 to 21.**

`slx_product`'s family column mixes four different things:

- real families — *Glas-Faltwand*, *cero*, *Wintergarten*
- the same family under two naming conventions — `cero | cero` **and** `cero`,
  `Wintergarten | Wintergarden` **and** `Wintergarten (Wintergarden)`
- individual products — *Highline*, *SL 25*, *Proline S*, *Varianda*
- marketing collateral — *Digitale Produktbroschüre*, *Beratungs PDF TD*

Left raw, Wintergarten was split across three families and a brochure download
counted as a product interest.

**Changed:** the ` | English` and ` (English)` suffixes stripped; all `cero*`
folded to `cero`; collateral dropped (22 rows); individual products folded into
their parent family via an explicit table in `dataquality._FAMILY_PARENT`
(Highline/SL 25/Proline S/Ecoline → Glas-Faltwand, SDL Atrium/Acubis →
Wintergarten, Varianda → Glashaus und Terrassendach).

### 2.4 Product euros were being averaged out of an empty column
**Not a cleanup — a bug that made the first import read €430 per company.**

`slx_product` reaches a company two ways, and they carry different things:

| Link | Rows | Rows with a value |
|---|---|---|
| account (`slx_accountid`) | 199.541 | **23** |
| opportunity (`slx_opportunityid`) | 156.401 | 30.114 (€411,7 Mio) |

The account link is a **catalogue relationship**, not a transaction. The first
version split the (empty) account value across families proportionally and
produced nonsense.

**Changed:** positions now come from the account link, euros from the opportunity
link resolved to the company through the deal's Käufer.

### 2.5 Companies sharing one website — reported, never merged
**786 groups over 1.9xx rows. Only 46 groups (92 rows) are genuine duplicates.**

This was nearly a bad automated fix. Most shared domains are **corporate
groups**, not errors: Lindner has 9 legal entities on one website, Drees &
Sommer 8, Geiger 7, Instone 5 — each with its own SAP number and its own
revenue. Merging them would destroy real distinctions.

The genuine duplicates turned out to be almost entirely **punctuation variants
of one legal entity**:

```
Lindner Facades Ltd.                    == Lindner Facades Ltd
Geiger Schlüsselfertigbau GmbH & Co. KG == Geiger Schlüsselfertigbau GmbH & Co KG
B&O Wohnungswirtschaft GmbH & Co. KG    == B&O Wohnungswirtschaft GmbH + Co. KG
```

Plus the imported market list, where the same firm appears under two spellings
(CBF / Calvia Balear Fachadas, LUCOR twice, Schüco five times) — 73 rows there.

**Changed:** nothing. `find_domain_duplicates()` reports the colliding **pairs**,
not the group. Flagging the whole group would send a human to re-check eight
correct Lindner records to find one duplicate. Detection strips punctuation
*before* matching legal forms — `S.L.` only reads as `sl` after the dots are
gone, and without that step "CBF" and "CBF S.L." look like different companies,
which is precisely the duplicate being hunted. Merging still needs a human.

---

## 3. Numbers you must not misread

**Product euros are QUOTED, not invoiced.** €384,1 Mio across won *and* lost
opportunities, against €391,8 Mio of actual recorded revenue. This is
deliberate: the gap between what a company **asks for** and what it **buys** is
the interesting part. Never label these as turnover.

**Some product families carry positions but no euros at all** — *Service*,
*Balkon- und Fassadengestaltung* (3.192 companies), *Fenster*, *Haustüren*,
*Markisen*. They come exclusively through the account link, which has no value.
Read them as "this company deals in this category", never as volume.

**`solarlux_relevance` and `solarlux_fit` are judgements, not facts.** They are
inferred from project types and stored with confidence 0.5 and their own
provenance, separate from extracted fields at 0.85.

**Belege of €0 are real.** ~25% of them (14.049 events) are warranty, samples
and replacements — genuine contact, not revenue. Three are negative
(Storno/Retoure) and are preserved rather than clipped.

**"Won" means Solarlux got the order.** The Firma on a won Verkaufschance is the
dealer who bought from us, not a competitor who beat us.

**A project can be won through a lost Verkaufschance.** In Objektvertrieb several
VCs belong to one `sl_primary_opportunityid`; one win makes the project won.
Project-level wins (8.189) exceed VC-level wins (7.684) for this reason.

---

## 4. Known gaps — measured, not guessed

| Gap | Size | Why |
|---|---|---|
| Verkaufschancen with no address in our DB | 57.776 (100%) | `sl_city`/`sl_postalcode`/`sl_street1` are 92–98% filled **in the CRM**; the Excel export omitted them |
| `CrmOpportunity.name` | 100% empty | dead column; `project_name` is the populated one |
| `building_type`, `total_amount`, `crm_modified_on` | 100% empty | never mapped from the export |
| Endkunde pointing at an unknown Firma | 14.675 of 25.945 (57%) | those accounts are outside the imported window |
| Käufer pointing at an unknown Firma | 5.008 of 54.445 (9%) | same |
| Who we lost to | unavailable | 257 `competitor` records exist, **zero** are linked to any opportunity |
| Quote / order / invoice line tables | HTTP 403 | our account lacks read privilege; would need an admin grant |
| `opportunityproducts`, `incidents` | 0 rows | unused in this org |
| Enrichment coverage | 1.563 of 48.239 companies | the paid step; deliberately not run at population scale yet |

---

## 5. Scope rules applied everywhere

- **Private Endkunden (1.665 companies) are excluded** from every count, list,
  report, product profile and ICP feature. They will never run an ad campaign or
  resell a system, and including them made every ratio wrong.
- **Competitors (68) are flagged and excluded** from prospect lists.
- **Intercompany entities (8)** — Linara, NanaWall, Solarlux sales offices —
  are flagged; Linara Kaufbeuren alone ranked #3 by revenue before this.
- **No personal data.** Company-level facts only: no contact names, no employee
  names, no person identifiers, even where the CRM offers them.

---

## 6. Re-running

```bash
python -c "from adwatch import dataquality as dq; import json; print(json.dumps(dq.audit(), indent=1, default=str))"
python -c "from adwatch import dataquality as dq; print(dq.repair())"
```

`audit()` changes nothing. `repair()` applies §2.1, §2.2 and §2.3 only —
duplicates are never merged automatically.
