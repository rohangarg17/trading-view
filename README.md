# Nifty Options Dashboard

Flask app that serves two related features for Indian-market options:

1. **Live dashboard** — ATM call/put premiums across the Nifty 50, ranked by nearest-expiry premium %. Pluggable data providers (free NSE scraping or paid Zerodha Kite).
2. **Backtester** — runs option strategies (e.g. covered-call collars) on historical stock data. Option prices are *synthesized* via Black-Scholes with an IV smile/skew model because free historical option-chain data does not exist.

Runs locally on `http://localhost:5050`.

## Quick start

```bash
./setup.sh                    # create venv, install deps, install Chromium for Playwright
source venv/bin/activate
cp .env.example .env          # default DATA_SOURCE=nse is free and works out of the box
python app.py
```

Open `http://localhost:5050`.

## Docs

- [SETUP.md](SETUP.md) — one-time setup, environment variables, Kite token refresh.
- [USAGE.md](USAGE.md) — how to use the dashboard, stock detail view, and backtester; what each feature expects.
- [CLAUDE.md](CLAUDE.md) — architecture notes (read this before making code changes).

## Pages

| Path | What it does |
| --- | --- |
| `/` | Live dashboard (Nifty 50 ATM premiums, sorted by nearest-expiry premium %) |
| `/stock/<SYMBOL>` | Per-stock detail — chart, indicators, full option chain |
| `/backtest` | Strategy backtester UI (auto-rendered from each strategy's `param_schema`) |
| `/guide` | In-app explainer |

## APIs

- `GET /api/data` — live ATM premiums (uses selected provider)
- `GET /api/source` — `{ "source": "nse" | "kite" }`
- `GET /api/stock/<symbol>` — details for the per-stock page
- `GET /api/strategies` — strategy registry + param schemas
- `POST /api/backtest` — `{ symbol, strategy, params, period }` → trades, summary, equity curve, price series
