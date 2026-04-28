# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# First-time setup (creates venv, installs deps)
./setup.sh

# Activate venv before running anything
source venv/bin/activate

# Run the Flask dashboard (http://localhost:5050)
python app.py

# Kite Connect access token refresh (required daily when DATA_SOURCE=kite)
python auth.py
```

There is no test suite, linter, or build step. Changes are validated by running the app.

## Architecture

The app is a Flask server (`app.py`, port 5050) that serves two distinct features backed by separate subsystems. Understanding the split is important:

### 1. Live dashboard — pluggable data providers

`app.py` selects a provider at request time based on the `DATA_SOURCE` env var:

- `nse` (default, free) → `nse_provider.py` uses `jugaad_data.nse.NSELive`.
- `kite` (paid) → `kite_provider.py` uses Zerodha Kite Connect.

Both providers expose a single `fetch_data()` returning the same shape: list of `{symbol, ltp, lot_size, expiries: [{expiry, strike, ce_price, pe_price, total_premium, premium_pct}], nearest_premium_pct}`, sorted by `nearest_premium_pct` desc. If you add a provider, preserve that contract — `templates/index.html` depends on it.

Provider-specific gotchas:
- **NSE provider** depends on NSE's public option-chain endpoint, which is only populated during market hours (Mon–Fri, ~9:00–15:30 IST). Outside those hours the provider degrades gracefully: stock prices still come back, but `expiries` is empty and a `market_note` is added. It also rate-limits itself with `time.sleep(0.5)` between per-symbol chain requests — don't remove that; NSE throttles aggressively. If the first two symbols return no option chain, it short-circuits and stops fetching option chains for the rest.
- **Kite provider** requires a fresh `KITE_ACCESS_TOKEN` every trading day. `auth.py` is the one-time-per-day flow to generate it. It fetches all NFO instruments in one call, then batches option `quote()` requests in groups of 400 (Kite's limit).

The NIFTY 50 symbol list is duplicated in both providers (`nse_provider.py` and `kite_provider.py`). Keep them in sync when constituents change.

### 2. Backtester — synthetic option prices via Black-Scholes

Free historical option-chain data does not exist, so the backtester **synthesizes option prices from historical stock prices** using Black-Scholes. This is the load-bearing design decision of the whole backtesting subsystem:

- `backtester.py` fetches OHLCV from Yahoo Finance (`yfinance`, NSE symbols suffixed with `.NS`; special cases in `YAHOO_SYMBOL_MAP`), computes 20-day rolling historical volatility via `pricing.historical_volatility`, and dispatches to a strategy from `STRATEGY_REGISTRY`.
- `pricing.py` is the numerics layer: Black-Scholes call/put/delta, an IV smile/skew model (`skew_adjusted_iv` — OTM puts get an extra skew factor on top of the quadratic smile to model crash premium), and an expected-return integrator (`expected_collar_return`) that weights outcomes by a lognormal distribution of exit prices.
- Because option prices are *modeled*, the IV input matters a lot. Strategies expose two `iv_mode`s: `fixed` (a single IV number, meant to approximate India VIX) and `vrp` (historical vol × premium factor). Defaults favor `fixed` — it matches what users see on the live option chain.

### 3. Strategy plugin system

Strategies live in `strategies/`, registered in `strategies/__init__.py:STRATEGY_REGISTRY`. A strategy subclasses `strategies.base.Strategy` and implements `run(dates, opens, highs, lows, closes, volumes, volatilities, params) -> list[Trade]`. It also declares a `param_schema` (list of dicts: `name`, `type`, `default`, `label`, `min`, `max`, `step`, or `choices`/`choice_labels` for `choice` type) — the frontend (`templates/backtest.html`) auto-renders the param form from this, so adding a parameter is a one-file change.

The existing `CollarStrategy` operates on monthly cycles: `_find_month_boundaries` groups trading days by `(year, month)`, and for each month it scans a range of call strikes (`generate_strikes_in_range`), scores them by one of five objectives (`OBJECTIVES` dict), and enters the collar only if the net credit % clears the `min_premium_pct` threshold — skipped months contribute to `months_skipped` in the summary, not to P&L.

### API surface

- `GET /api/data` — live ATM premiums across Nifty 50 (uses selected provider)
- `GET /api/source` — returns which provider is active
- `GET /api/strategies` — strategy registry + param schemas, used to build the backtest UI
- `POST /api/backtest` — body `{symbol, strategy, params, period}` → trades + summary + equity curve + price series

## Environment variables

Configured via `.env` (see `.env.example`):
- `DATA_SOURCE` — `nse` or `kite`
- `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_ACCESS_TOKEN` — only needed when `DATA_SOURCE=kite`
