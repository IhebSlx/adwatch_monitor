# Phase 0 — what this data would make you believe that is not true

Measured 2026-08-10 against `data/adwatch.db`. Companion to `OVERVIEW.md` (what
is in the app) and `DATA-QUALITY.md` (what was cleaned). This file is the third
question: **what is still wrong, and what would it have cost.**

Re-runnable: `adwatch/audit.py` — `report()` reports, writes nothing.
`dataquality.audit()` / `repair()` remain the place for things that can be fixed.

```bash
python -c "from adwatch import audit; import json; print(json.dumps(audit.report(), indent=1, default=str, ensure_ascii=False))"
```

---

## 1. Fixed in this pass

### 1.1 Fourteen own-group companies were not flagged
**Was:** `is_intercompany` set on 8 rows (the Linara entities). Fourteen more
matched the same name patterns and carried no flag.

| Firma | Beleg-Summe |
|---|---:|
| Nana Wall Systems Inc. | 39,7 Mio € |
| Solarlux Nederland B.V. | 21,9 Mio € |
| Solarlux Nana Manufacturing L.L.C. | 13,4 Mio € |
| Solarlux Schweiz AG | 4,9 Mio € |
| Solarlux Systems Ltd · GmbH · Austria · France · Scandinavia · España · SH-Plus u. a. | 1,9 Mio € |

**Cause:** `customers.flag_intercompany()` states in its own docstring that it is
"run on every import". It was called by nothing outside a test.

**What it would have made you believe:** that Solarlux Nederland B.V. is our
biggest Dutch customer. It is our Dutch subsidiary, and it accounts for 98 % of
everything invoiced in the Netherlands — which is also why NL shows 22,2 Mio €
of revenue against 0 € of won Verkaufschancen-Wert. The dealers hold the VCs, the
subsidiary receives the Beleg, and the join breaks between them.

**Fixed:** `upsert_accounts()` now re-flags at the end of every account import
and reports `intercompany_reflagged`. Flagged: 22.

### 1.2 The ICP's own-group guard never ran on the profile the app builds
**Was:** `build_profile()` applied `is_intercompany.is_(False)` only
`if not filters.get("ids")`. The default winners path sets
`filters = {"ids": material_buyer_ids(), …}`, so the guard was skipped exactly
there. 7 of the 8 then-flagged companies were in the default winners set; with
1.1 fixed it would have been 18 of 22.

**What it would have made you believe:** a profile trained partly on our own
subsidiaries — large, ideal-looking, and impossible to acquire. The comment three
lines above the bug says precisely this must never happen.

**Fixed:** the exclusion is unconditional. An id list is a choice of population,
never consent. Test: `test_own_group_is_excluded_even_when_the_filter_names_ids`.
The pre-existing intercompany test missed it because its fixture has no Belege
and therefore fell through to the id-less fallback filter.

### 1.3 Out-of-scope rows carried rankings
**Was:** all 1.665 Private Endkunden had a `fit_score` (stamped 2026-07-30, while
every in-scope segment had been rescored 2026-08-05) and 1.449 had a
`winback_score` — the latter written on every run, because `rfm.recompute()`
iterated `select(Company)` with no scope filter. 21 own-group companies likewise.

**What it would have made you believe:** nothing, as long as you only ever looked
through `overdue_customers()`, which filters correctly. That is what kept it
invisible. Any direct read of the column — an export, a report, a new query —
got consumers and subsidiaries.

**Fixed:** `rfm.recompute()` writes `winback_score` only for in-scope,
non-intercompany rows; `dataquality.clear_out_of_scope_scores()` removes what was
already there (1.665 + 21 rows, 5.030 values). Two rules, because the exclusions
differ: out of the business → nothing at all; own group → descriptive `fit_score`
stays, rankings go. `health` is kept in both cases — it is a fact about the row.

### 1.4 `architect_crm_id` is the buyer itself six times out of ten
**Was:** the field mirrors `slx_executingarchitect_accountid` — the *ausführender*
Architekt, whoever planned the job. A dealer planning in-house enters itself.

```
gefüllt                7.331 von 57.776 (12,7 %)
davon Architekt == Käufer  4.447 (60,7 %) -> Handel 2.295 · Verarbeiter 912 ·
                                             Baudienstleister 834 · Architekten 54
davon Dritter              2.884 (39,3 %) -> Architekten 2.589 · Baudienstleister 136
```

**What it would have made you believe:** that an architecture practice is involved
in 12,7 % of Verkaufschancen. The real figure is **2.884 of 57.776 = 5,0 %**, a
2,5-fold overstatement. `models.py` asserted the 12,7 % reading and has been
corrected. The imported `companies.arch_projects` / `arch_won` columns inherit the
same conflation and were **not** recomputed — they come from a CRM export.

**Fixed:** `insights/projekte.specifying_architect()` returns the architect only
when it differs from the Käufer; the Objekte list column uses it. `detail()`
deliberately does not — showing a dealer in its own Architekt role is the truth
about that deal, and hiding it would make the drawer disagree with Dynamics.

**Effect on the Architekten-Label (task #1): none worth worrying about.** The
self-referential rows are almost never `segment = Architekten` (54 of 4.447), so
restricting to genuine architects moves the label from 975 → 962 decided offices
and leaves 249 winners: base rate 25,5 % → 25,9 %.

---

## 2. Open — reported, not repaired

### 2.1 54 % of `crm_opportunity_products` points at Verkaufschancen we do not have
78.732 of 145.865 rows. 111.829 distinct GUIDs, of which 48.220 resolve. Orphan
value 11,6 Mio €.

**Cause:** the product lines were pulled over a wider period than the 2023+
opportunity window. Not corruption — a population mismatch.

**What it makes you believe:** the Objekt drawer joins on `opportunity_guid` and
therefore shows the 46 % that resolve, while `OVERVIEW.md` §5 aggregates the whole
table. cero reads as 15.723 Positionen / 104,6 Mio € over everything but
7.475 / 101,7 Mio € within the window — positions roughly double, euros barely
move, because the orphan rows are mostly value-less.

**Proposal:** either widen the opportunity pull or clip the product import to the
window. Until then §5 carries a population note. Deleting the rows would throw
away data that becomes valid the moment the window widens.

### 2.2 One deal in seven cannot be attributed to a company
```
VC ohne Käufer                      3.331
Käufer zeigt auf unbekannte Firma   5.008  (9,2 % der gesetzten)
zusammen                            8.339  (14,4 %)
gewonnener Auftragswert darin      28,4 Mio €  = 20,1 % des gesamten
```
Also `end_customer_crm_id`: 14.675 of 25.945 unresolvable (56,6 %).

**What it makes you believe:** any "per Firma" analysis silently drops a fifth of
won value. The channel mix of the unattributable rows is Direktvertrieb 2.633 and
Architektenberatung 2.361 — the motions where the counterparty is an end customer
or a planner who was never set up as an account. Consistent, not random.

### 2.3 Three columns are the outcome, sitting beside the features

| Spalte | gewonnen | verloren | Verhältnis |
|---|---:|---:|---:|
| `order_value` | 91,9 % | 0,0 % | 2955× |
| `invoiced_value` | 92,1 % | 0,2 % | 538× |
| `sap_order_numbers` | 92,1 % | 0,2 % | 538× |
| `quoted_value` | 76,6 % | 54,0 % | 1,42× |
| `end_customer_budget` | 3,6 % | 9,1 % | **0,39× (invers)** |

`end_customer_budget` is the interesting one: filled *more often on losses*.
Value comparisons between won and lost must use `estimated_value` (81,7–95,2 %
everywhere). `audit.outcome_leakage()` names them on every run.

### 2.4 Right-censoring is unevenly distributed
Open share by creation year: 2023 2,3 % · 2024 6,0 % · 2025 23,6 % · 2026 67,4 %.
Open share by Verkaufschancen per Objekt: 1 VC 18,8 % · 2 VCs 29,8 % ·
3–4 31,6 % · 5–9 37,2 %.

The second is the trap — censoring bites hardest exactly where the
win-rate-by-VC-count signal is read. Mature cohorts today: **2023 and 2024**.

### 2.5 Smaller things
| | Zeilen |
|---|---:|
| gewonnene VCs ohne `order_value` | 620 |
| `beleg_count = 0` trotz Order-Events (veraltetes Aggregat) | 3.616 |
| `conversion_rate` > 1 | 507 |
| `offen` mit gesetztem `closed_on` | 52 |
| `enrichment_status = enriched` ohne Website | 8 |
| negative `estimated_value` | 3 |
| `crm_showrooms` — in `models.py` über 15 Zeilen beschrieben, nie importiert | **0** |

---

## 3. Checked and clean

No duplicate keys anywhere (`companies.crm_id`, `sap_number`, `name`;
`crm_opportunities.crm_id`, `opportunity_guid`, `number` — 0 each). No orphan
order events. No `closed_on < created_on`, no future dates. No "gewonnen mit
Verlustgrund", no "verloren ohne Grund". Category values stable across the whole
window — the only late arrival is the Linara origins from 2023-10, which is a real
organisational change, not an import artefact. The 14.049 zero-euro events and 3
negative ones are documented and correctly preserved.

---

## 4. Two claims re-measured

**Win rate by German postcode zone — confirmed.** 49xxx 31,2 % (n=1.429) ·
37xxx 8,4 % (n=239), against 15,2 % across the tested set. Caveats: only 26.136
of 46.255 decided Verkaufschancen carry a usable five-digit German PLZ (the VC's
own `country` column is 100 % NULL, so the country has to be inferred from the
format), and n=239 is thin for the worst zone.

**Win rate by Verkaufschancen per Objekt — survives three attacks.**

| Angriff | Ergebnis |
|---|---|
| Kohorte (Zensierung) | monoton in jedem Jahr einzeln — 2023: 16,7 → 32,7 → 44,1 → 64,1 % |
| Vertriebsweg | hält innerhalb Fachhandel (17,5 → 35,0 → 44,2 → 55,2 %) und innerhalb Objektvertrieb (19,3 → 44,4 → 53,8 → 76,7 %) |
| Projektgröße | hält in jedem Quartil; Q4 (größte): 12,7 % bei 1 VC → 35,1 % bei 2 |

Both confounders are real — the median value per VC rises from 15.370 € to
73.116 €, and the channel mix flips from 87 % Fachhandel at one VC to 48 %
Objektvertrieb at five to nine — but neither explains the effect away.

The third confounder from the brief — whether sibling VCs get registered
preferentially on deals we already expect to win — is **not testable from this
data**: it needs the creation date of the siblings against the point of decision,
and members' `created_on` almost always falls on the same day.

---

## 5. Vocabulary: Objekt ≠ Objektvertrieb

The Objekte tab groups by `sl_primary_opportunityid`, which every Verkaufschance
has — a standalone deal points at itself. So the tab produces **52.796 "Objekte",
of which 49.216 are a single Fachhandels-Verkaufschance** nobody at Solarlux would
call an Objekt. Only 3.580 hold more than one VC, and even those are mostly
Fachhandelsvertrieb (2.276) rather than Objektvertrieb (898).

The grouping concept comes from Objektvertrieb, where several dealers bid on one
building. Applying the word to all 52.796 is the same failure as "Sammelprojekt"
and "Chancen" (`OVERVIEW.md` §6): a real Solarlux term stretched past its meaning.
Belongs in task #28 — it changes the tab's headline number, not just a label.

For the record, since the impression is easy to form: Verkaufschancen are **not**
Objektvertrieb. `vc_type` is Vertriebs-VC 52.381 (90,7 %) vs Architekten-VC 3.302
(5,7 %); `Vertriebsweg` is Fachhandelsvertrieb 47.070 (81,5 %) vs Objektvertrieb
3.987 (6,9 %). The VC's own `sales_channel` disagrees with the Käufer's on 3.312
deals, so it is genuinely the deal's channel, not the firm's.
