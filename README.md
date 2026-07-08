# AdWatch — Ad Activity Monitor (Meta, live)

Pulls ad activity for a tracked list of companies from the Meta Ad Library via Apify, auto-matches
each company name to its real Facebook page (so a "0 ads" result is trustworthy,
not a wrong-name guess), classifies each ad (hiring / selling / brand / event),
estimates weekly ad spend as a **modelled low–high interval**, stores every run
in a local database, and shows it in a dashboard + downloadable PDF.

## Quick start

```bash
cd adwatch_monitor
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt

python run.py serve      # → http://127.0.0.1:8000
```

Open the dashboard and click **"Fetch latest ads."** That's it — one command,
one button. No need to run anything on the command line first.

`.env` already has your Apify token and actor ID (`curious_coder/facebook-ads-library-scraper`)
wired in, in `ADWATCH_MODE=live`. **Treat that file as a secret — it's git-ignored,
but don't paste it anywhere else, and rotate the token if it's ever exposed again.**

## How company resolution works

Each company starts as **pending**. The first time you click "Fetch latest ads,"
the app searches the Meta Ad Library by that exact name, groups whatever ads
come back by their actual Facebook page, and picks the best name match —
**one Apify run does both the identity check and the data pull**, so this costs
nothing extra. From then on:

- 🟢 **confirmed** — page locked in; future runs fetch that exact page. A
  `0 active ads` result from here on is a real fact, not a guess.
- 🟡 **ambiguous** — more than one page matched about equally well; the best
  guess is used but flagged. Check the dot's tooltip for the matched name.
- 🔴 **no ads found** — the name search returned nothing at all. This could mean
  the exact name doesn't match how they appear in the Ad Library, or they
  genuinely have no ad presence. Worth a manual check on
  [facebook.com/ads/library](https://www.facebook.com/ads/library/) if it
  matters — this is exactly the "is it the wrong page or really zero ads"
  problem the tool exists to catch.
- ⚪ **pending** — not fetched yet.

Renaming a company resets it to pending (a new name means a fresh search).

## Commands
| Command | Does |
|---|---|
| `python run.py serve` | dashboard + the "Fetch latest ads" button (`--port 8000`) |
| `python run.py run` | same fetch, from the command line |
| `python run.py report` | write a PDF to `output/` |
| `python run.py init-db` | create DB + seed companies (also happens automatically) |
| `python run.py reseed` | reset the company list back to `config/companies.yaml` |

Company add/rename/remove also works live in the dashboard and saves straight
to the database — `config/companies.yaml` is only the initial seed.

## Cost & timing

Each "Fetch" click runs one Apify actor call per company (~$0.75/1,000 ads
scraped — a few cents per week for this list). Apify runs aren't instant:
expect the button to take 1–3 minutes for ~9 companies, since each call is
started, polled, and its results retrieved before moving to the next company.

## Notes on data quality

- **Spend is a modelled estimate**, shown as a low–high range — see
  `config/spend_assumptions.yaml`. Meta only discloses real spend for regulated
  categories (political/housing/employment); everything else is estimated from
  reach (when present) or ad counts.
- Ad classification uses a deterministic keyword classifier by default. Set
  `ANTHROPIC_API_KEY` in `.env` to upgrade to Claude Haiku for better "what are
  they selling" extraction.
- Every raw scraped item is stored (`Ad.source_raw` in the DB) even though it's
  not all surfaced in the UI — useful if a field mapping needs correcting later
  without re-scraping.
- Switch to `ADWATCH_MODE=mock` in `.env` to run fully offline on generated
  sample data (useful for demos or UI work without spending Apify credits).

## Layout
```
adwatch/
  config.py        env + yaml loading, mode flag
  models.py        SQLAlchemy schema (source-agnostic; raw ads kept per run)
  db.py            engine/session
  sources/
    base.py        AdSource interface (Google/LinkedIn slot in here later)
    meta.py         Meta adapter: live Apify calls, resolve+fetch, mock fallback
  mockdata.py      deterministic offline sample-ad generator (mock mode only)
  classify.py      deterministic + Claude Haiku ad classifier
  spend.py         low–high interval spend model
  aggregate.py     ads -> weekly metric
  pipeline.py      seed + run_once orchestration (resolve-then-fetch logic)
  services.py      company CRUD + dashboard queries
  report.py        PDF generation
  web.py           FastAPI dashboard
  cli.py           command entrypoints
config/
  companies.yaml         initial seed (edits after that live in the DB)
  spend_assumptions.yaml
```

## Not yet built
- Scheduling — this runs on demand via the button. Add a weekly cron later.
- Email delivery — PDF only for now.
- Google Ads / LinkedIn — the `AdSource` interface is where they plug in
  without touching the DB, classifier, aggregator, or report.
