# Session notes — 2026-07-09

A single long working session that took AdWatch from "Meta-only ad tracker with a
broken email" to a unified Companies/Customers explorer with Google Ads support,
in-app scheduling, and resumable bulk-fetch jobs. This file is a record of what
changed and why — see git log for the actual diffs (mostly one commit so far:
`f59533d` and earlier; this session's work is uncommitted as of writing).

## 1. Power Automate email delivery — fixed

- **Root cause of corrupted PDF attachments**: none of the app's code — the
  Power Automate flow's `To`/`Subject` fields were hardcoded test values,
  never wired to the trigger's dynamic content. Fixed in the flow itself
  (not in this repo).
- App now sends a `week` field (`"KW 29"`) alongside `subject`
  (`"Bericht-KW-29"`) so the flow's email body can reference the week number
  without parsing the filename. See `adwatch/emailer.py`, `adwatch/report.py`
  (`subject_for_filename`, `week_str_for_filename`).
- Fixed a real 422 bug in the fetch button: `$("#fetchBtn").addEventListener("click", startFetch)`
  was passing the raw DOM click `Event` as `companyId` — wrapped in an arrow
  function like the other fetch buttons.

## 2. In-app scheduling

- `adwatch/scheduler.py` — APScheduler running inside the `serve` process.
  Configurable fetch day/time/sources and send day/time/report-type from the
  dashboard's Reports tab, persisted in `ScheduleConfig`.
- Fixed a gap where the scheduler's auto-fetch only ever ran Meta — now reads
  `fetch_sources` and dispatches to whichever of `meta`/`google` are enabled.

## 3. Google Ads integration

- **Key finding**: Google's Ads Transparency Center actor
  (`google-ads-scraper`, id `N8vqwV9wL9wpIsLDz` on Apify) has no name search —
  identity resolution is by **website domain** instead
  (`?region=DE&domain=example.com` auto-resolves the advertiser). Confirmed
  live against real Apify pricing: Meta $0.00075/ad, Google $0.0019/ad
  (`PAY_PER_EVENT`).
- `adwatch/collect/google_source.py` — `GoogleAdSource(AdSource)`, mirrors
  `meta_source.py`'s structure.
- `identity/resolver.py` — `resolve_and_record_google()`, domain-based and
  exact (no "ambiguous" state, unlike Meta's fuzzy name match).
- No landing-URL data from Google, so the Meta-style partner-hub sweep has no
  equivalent for Google ads.
- Dashboard: KPI tiles split into Meta/Google/Total active ads; company table
  gained Meta ads/Google ads columns, full column filtering, and sortable
  headers (`services._merge_week_rows` merges per-source `WeeklyCompanyMetric`
  rows into one combined view per company per week).
- Fetch UI: source checkboxes (Meta/Google) next to "Fetch latest ads",
  multi-source progress bar that shows combined progress and current source
  instead of resetting to 0% between sources.

## 4. Railway deployment prep

- `cli.py serve` now binds `0.0.0.0` and reads `PORT` from the environment.
- `Procfile`, `.python-version` added.
- `ADWATCH_DATA_DIR` / `ADWATCH_OUTPUT_DIR` env overrides added so a Railway
  Volume can be mounted outside the app directory for persistent SQLite +
  generated PDFs.
- **Not done yet**: auth. The customer data added later in this session
  (SAP numbers, revenue, contacts) must not go on a public Railway URL
  without a login gate first — see "Known follow-ups" below.

## 5. Customer Explorer → merged into Companies (the big one)

Original ask: bulk-import ~3000 customer records (SAP Nummer, segment,
revenue, contacts, website) from an Excel export, browse/filter/select a
subset, and fetch ads for just that subset.

**First cut (since revised — see below)**: a separate `Customer` table/tab,
linked to `Company` via `Company.customer_id`, with an explicit "promote to
tracking" step to bridge the two.

**User feedback mid-session**: "the companies are the customers" — there's no
real distinction. Reworked into one entity:

- `Customer` model removed entirely. All master-data fields (sap_number, kv,
  segment, sub_segment, sales_channel, street, postal_code, city, phone,
  email, fax, revenue_y0..y4, imported_at) now live directly on `Company`
  (see `models.py`'s `Company` docstring).
- No promotion step — every company row can be selected and fetched directly;
  a row is "tracked" the moment its `resolution_status != 'pending'`, i.e.
  the first time it's actually fetched.
- `adwatch/customers.py` rewritten to operate on `Company`: `parse_excel`,
  `upsert_companies` (upsert keyed on sap_number, falls back to exact-name
  match for pre-existing hand-added companies, disambiguates genuine name
  collisions rather than crashing on the `Company.name` unique constraint),
  `query_companies`, `top_ids`, `filter_options`, `export_xlsx`.
- `db.py` migration folds any leftover rows from the old separate `customers`
  table into `companies` (additive, idempotent, the old table is never
  dropped) — this only matters for installs that ran the first-cut version;
  fresh installs never create the old table at all.
- One tab: "Companies". Explorer table (filter/sort/paginate over the full
  set, multi-select persists across pages) + expandable per-row "Pages"
  panel (rename/domain/fetch-one/delete + the existing Meta page
  search/confirm/manual-link flow, reused unchanged from before the merge).
- Selection is by row `id` now, not `sap_number` (not every company has one —
  the original 9 hand-added companies don't).

### Phase 2 — scoped, resumable fetch jobs

- `adwatch/jobs.py` — `FetchJob` model persists progress after **every**
  (company, source) unit, not just at the end. Sequential by design (SQLite
  has one writer; concurrency would only add contention, not speed).
- Pre-flight estimate (`jobs.estimate`) shown inline before starting: company
  count, estimated time, estimated Apify cost — using each company's own
  last known ad count when available, else a dataset-wide average.
- Resumability actually tested: killed a job mid-run (simulated crash),
  restarted the app, confirmed it flagged `interrupted` (not silently stuck),
  hit Resume, watched it continue exactly from the cursor with zero
  duplicate work.
- Found and fixed a real bug during that test: `reconcile_on_startup()`
  originally only checked whichever DB (mock/live) was active *at boot*, so
  a job stuck in the other mode would never get reconciled. Now checks both.
- Shared busy-lock (`jobs.try_acquire`/`release`) between scoped jobs and the
  old manual "Fetch latest ads" button — they can't run concurrently and
  corrupt each other via simultaneous SQLite writes.

### Real-world validation

The whole pipeline (import → filter → sort → select-top → estimate → fetch
job) was verified against the user's actual ~3,622-row company export
(imported into the Mock database, live data untouched): 3,620/3,622 rows got
a SAP number, 0 forced name-disambiguations needed, all real segment/revenue/
website data parsed correctly, umlauts intact (an earlier `�` was just a
terminal codepage display issue, not real corruption).

## Known follow-ups / things to do before relying on this further

1. **Two duplicate-risk companies on the real Live import.** The user's
   original hand-added "Linara Münsterland" and "Wild & Kienle" don't
   exact-match the real Excel spellings ("Linara Münsterland GmbH" and
   "Wild u. Kienle Bauelemente GmbH" respectively) — importing the real file
   into Live will create 2 duplicate rows unless those 2 are renamed first to
   match the Excel exactly.
2. **Auth before hosting customer data.** Revenue/contact data must not sit on
   the public Railway URL without a login — deferred per earlier decision to
   stay local-only for now.
3. **Mode-scoping.** Companies/customers/fetch-jobs are all stored per-mode
   (mock vs live, separate SQLite files) — always do real work in **Live**
   mode. This bit us once already (a real file got imported while in Mock).
4. **Phase 3 (not started)**: per-selection reports with provenance (which
   exact customers/filter a report covered, so past reports are auditable)
   and segment-based scheduling/distribution (different report/cadence/
   recipients per saved segment).
5. **Scale.** `services.list_companies()`/`latest_metrics()` used by
   `/api/state` are unpaginated — fine at 9-ish companies, will slow down
   as more of the real ~3,622 get tracked. Not yet a problem since only a
   filtered subset will ever be fetched, but worth watching.
