# Markets Dashboard

A single auto-updating web page tracking two things:

- **Gold (GC) June 2026 settlement odds** from Polymarket (all price brackets,
  full market history + day-over-day changes).
- **Micron (MU)** daily close with 9/21/48-day EMAs, plus crossover signals and
  day-over-day change.

Both charts are interactive (Plotly — zoom, pan, hover). The page is
regenerated **daily by GitHub Actions** and served by **GitHub Pages**, so it
keeps updating with no computer running locally.

Live: https://ishaansrivastava1.github.io/markets-dashboard/

## How it works

- `gold_tracker.py` / `mu_tracker.py` — data + chart modules (fetch, maintain a
  CSV history, build a Plotly figure and an HTML summary). No email/SMTP.
- `build_site.py` — refreshes both trackers and writes `docs/index.html`.
- `data/*.csv` — the durable history stores (committed each run, since Actions
  runners are ephemeral).
- `.github/workflows/update.yml` — runs `build_site.py` daily at 22:00 UTC (and
  on manual dispatch), then commits any changed data/site back to the repo.

Data sources — Polymarket gamma API (live odds) + CLOB API (historical
backfill), and Yahoo Finance's public chart endpoint (MU OHLCV). All public,
so no API keys or secrets are needed.

## Run it locally

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python3 build_site.py
open docs/index.html
```

## Trigger a cloud update manually

```bash
gh workflow run update.yml
gh run watch
```

For personal/informational use — not financial advice.
