# vet-market-scrapers

Public scraper rig for **The Veterinary Market** network vertical. Aggregates
real, public-source veterinary-practice-for-sale listings from dedicated vet
practice-sales brokers and publishes a single canonical `listings.json` that the
live sites consume.

**Live sites fed by this repo:**
- https://theveterinarymarket.com (Vercel project `veterinary`)
- https://theveterinarypracticemarket.com (Vercel project `veterinarypractice`)

Everything in this repo is scraper code + public listing data. **No secrets, no
tokens, no seller PII.** Broker office contact details that appear on public
broker listing pages are public business contact info.

## What it does

```
run_all.py  ->  per-source scrapers (omnivet, psbroker, psadvisors, simmons,
                tpsg, vetsales)  ->  output/*_raw.csv  ->  normalizer.py
             ->  listings.json  (canonical, TVM-XXXXX siteIds, deduped)
```

- `utils.py` — real UA + polite 1.5–3.5s delays + price/state helpers.
- `broker_codes.json` — source registry, `site_prefix = TVM`.
- `site_id_registry.json` — persistent TVM- id map. **Never renumber.**
- `listings.json` — the canonical dataset (125 listings, 6 brokers). Tracked on
  purpose; the daily Action regenerates and commits it back here.

## Auto-refresh pipeline (refresh -> live)

`.github/workflows/scrape-veterinary.yml` runs **daily at 08:30 UTC** (plus manual
`workflow_dispatch`). This repo is **PUBLIC**, so GitHub Actions minutes are
unlimited/free.

The Action is **self-contained — it only ever writes to THIS repo:**

1. checkout -> install deps -> `python run_all.py` (scrape + normalize).
2. **Sanity guard:** if `listings.json` collapses below 20 listings (e.g. a
   runner IP gets blocked and scrapers write empty CSVs), the job **fails and
   refuses to commit**, preserving the last-good dataset. The live sites never get
   wiped by a bad scrape.
3. commit `listings.json` + `output/*.csv` + `site_id_registry.json` back to this
   repo using the default `GITHUB_TOKEN` (`permissions: contents: write`). No PAT.

**Why no cross-repo push:** the two site repos are SEPARATE git repos. Instead of
this Action reaching into them (which needs cross-repo credentials and was the
original wiring bug), each **site pulls `listings.json` from this repo's public
raw URL at build time**:

```
https://raw.githubusercontent.com/DentalAI22/vet-market-scrapers/main/listings.json
```

So the refresh-to-live path is:

```
daily Action scrapes  ->  commits listings.json to THIS repo
       ->  a site rebuild (`vercel --prod`, or a site-side prebuild fetch step)
           pulls the fresh raw listings.json  ->  republishes.
```

The public raw file is the single source of truth. No cross-repo push credentials
are required anywhere.

## Re-run locally

```bash
pip install -r requirements.txt
python run_all.py                 # scrape all sources + normalize -> listings.json
python run_all.py --only omnivet  # one source
python run_all.py --normalize     # re-normalize existing CSVs (no network)
```

## Constraints honored

- Read-only against public broker pages only; real browser UA; 1.5–3.5s delays.
- Blocked aggregators (BizBuySell / BizQuest / LoopNet / DealStream / Provide /
  PracticeOrbit) are **never** scraped.
- Honest counts; deduped; no fabricated data.
