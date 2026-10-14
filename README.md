# DVC Deal Hunter

Watches every major Disney Vacation Club resale broker every ~15 minutes, works out which
contracts can cover our Aulani trips (Sept and late July 2027, 5–6 nights, one or two
families), prices the full buy → use → resell plan, and pushes the best new deals to ntfy.

- **Dashboard:** https://cakers88.github.io/dvc-deal-hunter/
- **What we're hunting for:** `config.yaml` (trips, rooms, budget, assumptions, alert thresholds)
- **Data history:** `data/listings.json` (current state + price/status history), `data/events.jsonl` (every new / price change / gone event)

## Sources
Scraped directly: DVC Resale Market, The DVC Store, DVC Shop, DVC Resale Experts, DVCSales.com,
Vacation Club Life, Buy and Sell DVC, DVC by Resale, Magic Resales.
Via the DVC Market aggregator: Fidelity Real Estate (its terms forbid scraping, so it's only
seen through the aggregator), Vacatia, DVC Advantages, plus a cross-check of everyone else.

## How it runs
`.github/workflows/hunt.yml` runs `python -m hunter.run` every 15 minutes:
1. `hunter/scrape.py` – runs every adapter in `hunter/adapters/`, de-duplicates, fetches detail
   pages for new/changed contracts, records history.
2. `hunter/analyze.py` – use-year math, booking windows, costs, dues, resale plan, cash timeline, score.
3. `hunter/alert.py` – ntfy push for new or price-dropped contracts scoring ≥ `alerts.ntfy_min_score`.
4. Commits `data/` when listings change; publishes `docs/` to GitHub Pages.

## Setup
Repo secrets (Settings → Secrets and variables → Actions):
- `NTFY_URL` – full topic URL, e.g. `https://ntfy.example.com/dvc-deals`
- `NTFY_TOKEN` – only if the topic needs auth

Settings → Pages → Source: **GitHub Actions**.

Run locally: `pip install -r requirements.txt && python -m hunter.run`, then open `docs/index.html`
via `python -m http.server -d docs`.

Estimates, not advice: verify points, dues terms and dates with the broker before offering.
