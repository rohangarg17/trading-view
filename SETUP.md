# Setup

## Prerequisites

- Python 3.10+
- macOS or Linux (the Playwright Chromium install assumes a POSIX shell; `setup.sh` is bash)
- Internet access for NSE / Yahoo Finance / Kite endpoints

## First-time install

```bash
./setup.sh
```

What it does:
1. Creates `venv/` and activates it.
2. `pip install -r requirements.txt` (Flask, jugaad-data, kiteconnect, yfinance, numpy, scipy, playwright, …).
3. `playwright install chromium` — the free NSE provider falls back to a headless Chromium scrape when NSE's public JSON endpoint rate-limits or blocks.

Every subsequent shell session:
```bash
source venv/bin/activate
python app.py
```

## Environment variables (`.env`)

Copy `.env.example` to `.env` and edit as needed.

| Variable | Values | Required when |
| --- | --- | --- |
| `DATA_SOURCE` | `nse` (default) / `kite` | always |
| `KITE_API_KEY` | Zerodha app key | `DATA_SOURCE=kite` |
| `KITE_API_SECRET` | Zerodha app secret | `DATA_SOURCE=kite` |
| `KITE_ACCESS_TOKEN` | daily token (see below) | `DATA_SOURCE=kite` |

Defaults to `nse`, which is free and needs no credentials.

## Data source: NSE (free)

No credentials. The provider (`nse_provider.py`) pulls the option chain from NSE's public endpoints.

Gotchas:
- **Only works during market hours** — Mon–Fri, ~09:00–15:30 IST. Outside those hours stock prices still load but `expiries` is empty and the UI shows a `market_note`.
- **Rate-limits itself** (`time.sleep(0.5)` between per-symbol chain requests). Do not remove — NSE throttles aggressively and will start returning empty chains.
- If the first two symbols return no option chain, the provider short-circuits and skips the rest for that refresh cycle.
- Playwright/Chromium is used as a fallback scrape path; that is why `setup.sh` installs Chromium.

## Data source: Kite (paid, Zerodha account required)

1. Create a Kite Connect app at <https://kite.trade/> and put `KITE_API_KEY` + `KITE_API_SECRET` in `.env`.
2. Generate a fresh access token **every trading day**:
   ```bash
   source venv/bin/activate
   python auth.py
   ```
   Follow the browser flow, paste the `request_token` back into the terminal. The script prints an access token — copy it into `KITE_ACCESS_TOKEN` in `.env`.
3. Start the app. `kite_provider.py` fetches all NFO instruments once, then batches option `quote()` calls in groups of 400 (Kite's limit).

Access tokens expire at ~06:00 IST every morning, so step 2 is a daily chore if you want Kite data.

## Troubleshooting

- **`No module named ...`** — you forgot `source venv/bin/activate`.
- **Empty dashboard after hours** — expected on the NSE provider. Check `/api/source` to confirm which provider is active.
- **`market_note` showing but it *is* market hours** — NSE is throttling. Wait a couple of minutes; the background refresh thread will retry.
- **Kite `TokenException`** — access token expired. Re-run `python auth.py` and update `.env`.
- **Playwright errors on launch** — rerun `playwright install chromium` inside the venv.
