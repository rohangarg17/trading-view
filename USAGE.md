# Usage

## Running the app

```bash
source venv/bin/activate
python app.py
```

Then open <http://localhost:5050>.

Flask runs in debug mode, so file edits auto-reload. The NSE background refresh thread only starts in the reloader child (`WERKZEUG_RUN_MAIN=true`), so you won't see two Chromium instances.

## Live dashboard — `/`

Lists Nifty 50 stocks with ATM call + put prices for the nearest expiries, sorted by **nearest-expiry premium %** (call + put premium as a % of the spot).

What each column means:
- `LTP` — last traded price of the underlying.
- `Lot size` — contract size on NFO.
- For each expiry: `CE` (call price), `PE` (put price), `Total premium` (CE + PE × lot size), `Premium %` (total premium ÷ (LTP × lot size)).

Notes:
- The data source is whichever `DATA_SOURCE` is set to in `.env`. Check `/api/source` to confirm.
- **Outside market hours** the NSE provider returns stock prices but no option chains, and shows a `market_note` banner. Use Kite if you need after-hours data.
- Click a symbol to open the per-stock detail page.

## Stock detail — `/stock/<SYMBOL>`

Shows a chart, indicators, and the full option chain for one symbol. Powered by `stock_detail.py` via `GET /api/stock/<symbol>`.

## Backtester — `/backtest`

Pick a symbol, a strategy, tweak parameters, run it. Results:
- **Summary** — P&L, win rate, months traded, months skipped, etc.
- **Trades table** — one row per cycle.
- **Equity curve** — cumulative P&L.
- **Price series** — the underlying's close over the tested period (for context).

### Data model — important caveat

Free historical option-chain data does not exist, so the backtester **synthesizes** option prices from historical stock prices using Black-Scholes (`pricing.py`). This means:
- Results depend heavily on the IV input you pick.
- Strategies expose an `iv_mode`:
  - `fixed` — a single IV number (meant to approximate India VIX). **This is the default and matches what you see on the live option chain most closely.**
  - `vrp` — historical vol × premium factor.
- OTM puts get an extra skew factor layered on the quadratic smile to model crash premium. See `pricing.skew_adjusted_iv`.

Treat backtester P&L as directional, not precise.

### Built-in strategies

- **`collar`** — monthly covered call + protective put. For each month it scans a range of call strikes, scores them against one of five objectives (`OBJECTIVES` in `strategies/collar.py`), and enters only if net credit % clears `min_premium_pct`. Skipped months go into `months_skipped` in the summary.

### Adding a strategy

1. Create a file in `strategies/` that subclasses `strategies.base.Strategy`.
2. Implement `run(dates, opens, highs, lows, closes, volumes, volatilities, params) -> list[Trade]`.
3. Declare `param_schema` — a list of dicts with `name`, `type`, `default`, `label`, optional `min`/`max`/`step`, or `choices`/`choice_labels` for `choice` type. The frontend (`templates/backtest.html`) auto-renders the form from this, so no UI changes are needed.
4. Register it in `strategies/__init__.py` under `STRATEGY_REGISTRY`.

### `POST /api/backtest` payload

```json
{
  "symbol": "RELIANCE",
  "strategy": "collar",
  "params": { "iv_mode": "fixed", "iv_fixed": 0.18, "min_premium_pct": 0.8 },
  "period": "2y"
}
```

`symbol` is a plain NSE symbol (no `.NS`); `backtester.YAHOO_SYMBOL_MAP` handles special cases (e.g. renamed tickers).

## Daily chores

- **NSE provider:** none. It just works during market hours.
- **Kite provider:** run `python auth.py` once per trading day to refresh `KITE_ACCESS_TOKEN` in `.env` (tokens expire around 06:00 IST). See SETUP.md.

## Validating changes

There is no test suite, linter, or build step. The workflow is:
1. Edit.
2. Flask auto-reloads.
3. Hit the relevant page / API and check the response.

Provider contract: both `nse_provider.fetch_data()` and `kite_provider.fetch_data()` must return the same shape (`[{symbol, ltp, lot_size, expiries: [{expiry, strike, ce_price, pe_price, total_premium, premium_pct}], nearest_premium_pct}]`, sorted by `nearest_premium_pct` desc). `templates/index.html` depends on it — if you add a provider, preserve that shape.
