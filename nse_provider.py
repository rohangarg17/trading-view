"""Free NSE provider.

Option-chain data is fetched via a persistent Playwright browser (NSE
blocks raw HTTP clients — see nse_playwright.py). Stock prices still
come from jugaad_data.NSELive (its index endpoint isn't firewalled).

Because each Playwright call takes ~300ms and we fetch 48+ symbols, we
run refreshes on a background thread every 5 minutes and serve the web
API from an in-memory snapshot. First-ever refresh after server start
also warms from disk cache so the dashboard isn't empty.
"""

import csv
import io
import json
import os
import threading
import time
from datetime import datetime

from jugaad_data.nse import NSELive

# Optional: curl_cffi bypasses NSE's TLS fingerprinting for the lot-size CSV.
# If it ever goes missing, we fall back to the hardcoded equity index sources.
try:
    from curl_cffi import requests as cf_requests
except ImportError:
    cf_requests = None


REFRESH_INTERVAL_SEC = 300  # 5 minutes

# How many Playwright pages run in parallel during a refresh. NSE tolerates
# a handful of concurrent same-origin XHRs (the real option-chain page fires
# ~6 on first load). Env override clamped to 1-8; default of 4 keeps us well
# inside NSE's tolerance while giving a ~4x speedup on the per-symbol budget.
def _get_refresh_concurrency():
    try:
        n = int(os.getenv("NSE_REFRESH_CONCURRENCY", "4"))
    except ValueError:
        n = 4
    return max(1, min(n, 8))

CACHE_PATH = os.path.join(os.path.dirname(__file__), ".cache", "nse_last.json")


# --- Disk cache (survives server restarts) ---------------------------------

def _load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_cache(payload):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(payload, f)


# --- In-memory snapshot shared with Flask ---------------------------------

_snapshot = None             # dict returned by fetch_data()
_snapshot_lock = threading.Lock()
_refresh_thread = None
_refresh_lock = threading.Lock()


def _find_atm_strike(ltp, strikes):
    return min(strikes, key=lambda s: abs(s - ltp))


def _parse_nse_date(d):
    try:
        return datetime.strptime(d, "%d-%b-%Y")
    except ValueError:
        return datetime.max


# Universe of symbols we want option chains for. Each is a dashboard row.
# - "Equity": every F&O-enabled stock NSE publishes a lot size for.
# - "Indices": five major index option chains NSE publishes.
INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]

# Live-index endpoint names used to look up equity LTPs. Nifty 100 covers the
# large-cap bulk of the F&O list in one call; we fall back to per-symbol
# NSELive.stock_quote for the tail (~100 smaller names not in Nifty 100).
EQUITY_INDEX_SOURCES = ["NIFTY 100", "NIFTY BANK", "NIFTY MIDCAP 150", "NIFTY 500"]

# NSE's public F&O lot-size CSV — also serves as the authoritative list of
# F&O-enabled securities. Updated ~monthly when NSE reviews eligibility.
FO_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
FO_LIST_TTL_SEC = 24 * 60 * 60  # 24 hours

_fo_list_cache = {"symbols": None, "ts": 0}

# Map index option-chain symbol -> NSELive live_index name for getting its LTP.
# Not every index has a matching live_index endpoint, so missing ones fall back
# to 0 and we still fetch the chain.
INDEX_LTP_SOURCE = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FINANCIAL SERVICES",
    "MIDCPNIFTY": "NIFTY MIDCAP SELECT",
    "NIFTYNXT50": "NIFTY NEXT 50",
}


def _fetch_fo_symbol_list():
    """Fetch the F&O-enabled equity symbols (cached 24h).

    Returns a set of equity symbols (filters out the 5 index rows at the top
    of the CSV). On any failure, returns None so callers can fall back.
    """
    now = time.time()
    if _fo_list_cache["symbols"] and now - _fo_list_cache["ts"] < FO_LIST_TTL_SEC:
        return _fo_list_cache["symbols"]
    if cf_requests is None:
        return None
    try:
        resp = cf_requests.Session(impersonate="chrome124").get(
            FO_LIST_URL, timeout=20
        )
        if resp.status_code != 200:
            return None
        symbols = set()
        for row in csv.reader(io.StringIO(resp.text)):
            if len(row) < 2:
                continue
            sym = row[1].strip()
            # Skip header row, blank rows, and CSV sub-header "Symbol"/"SYMBOL"
            if not sym or sym.lower() == "symbol":
                continue
            # Skip index rows — we handle those separately via INDEX_SYMBOLS
            if sym in INDEX_SYMBOLS:
                continue
            symbols.add(sym)
        if not symbols:
            return None
        _fo_list_cache["symbols"] = symbols
        _fo_list_cache["ts"] = now
        return symbols
    except Exception:
        return None


def _get_equity_prices(nse):
    """Fetch live LTPs for every F&O-enabled equity.

    Tries NSE's indexes first (each live_index call returns ~100 symbols at
    once — much cheaper than per-symbol quotes). Falls back to stock_quote
    for the tail of F&O names not covered by any of our indexes.
    """
    fo_symbols = _fetch_fo_symbol_list()

    prices = {}
    for idx_name in EQUITY_INDEX_SOURCES:
        try:
            data = nse.live_index(idx_name)
        except Exception:
            continue
        for stock in data.get("data", []):
            sym = stock.get("symbol", "")
            ltp = stock.get("lastPrice")
            if not sym or " " in sym or not ltp:
                continue  # skip the index's own row (e.g. "NIFTY 100")
            if sym in prices:
                continue
            # If we have an F&O whitelist, only keep tradable names
            if fo_symbols is not None and sym not in fo_symbols:
                continue
            prices[sym] = float(str(ltp).replace(",", ""))

    # Fill the tail: F&O names that none of our indexes covered
    if fo_symbols:
        missing = fo_symbols - prices.keys()
        for sym in missing:
            try:
                q = nse.stock_quote(sym)
            except Exception:
                continue
            ltp = (q.get("priceInfo") or {}).get("lastPrice")
            if ltp:
                prices[sym] = float(str(ltp).replace(",", ""))
    return prices


def _get_index_prices(nse):
    """Fetch LTPs for each of our tracked indexes via their live_index endpoint."""
    prices = {}
    for oc_symbol, src_name in INDEX_LTP_SOURCE.items():
        try:
            data = nse.live_index(src_name)
        except Exception:
            continue
        # The index's own row is the first entry and its symbol matches the
        # index name. lastPrice is the index's LTP.
        for row in data.get("data", []):
            sym = row.get("symbol", "")
            if sym == src_name:
                ltp = row.get("lastPrice")
                if ltp:
                    prices[oc_symbol] = float(str(ltp).replace(",", ""))
                break
    return prices


def _records_to_expiry_list(records, ltp):
    """Convert a single NSE option-chain records dict to our expiry format.

    records.data contains rows per (expiry, strike), each with CE/PE.
    Returns (expiry_results, lot_size, all_expiries, underlying).
    """
    option_rows = records.get("data", []) or []
    expiry_dates = records.get("expiryDates", []) or []
    underlying = records.get("underlyingValue", ltp)
    if isinstance(underlying, str):
        try:
            underlying = float(underlying.replace(",", ""))
        except ValueError:
            underlying = ltp
    if not underlying:
        underlying = ltp

    strike_map = {}
    lot_size = None
    for row in option_rows:
        # v3 uses "expiryDates" (plural) on each row; older v1 used "expiryDate"
        exp = row.get("expiryDates") or row.get("expiryDate", "")
        strike = row.get("strikePrice", 0)
        if not exp or not strike:
            continue
        bucket = strike_map.setdefault(exp, {}).setdefault(strike, {})
        if row.get("CE"):
            bucket["CE"] = row["CE"].get("lastPrice", 0)
            if lot_size is None:
                lot_size = row["CE"].get("marketLot", 1)
        if row.get("PE"):
            bucket["PE"] = row["PE"].get("lastPrice", 0)
    lot_size = lot_size or 1

    expiry_results = []
    all_expiries = []
    for exp in expiry_dates:
        if exp not in strike_map:
            continue
        strikes = list(strike_map[exp].keys())
        if not strikes:
            continue
        atm = _find_atm_strike(underlying, strikes)
        atm_data = strike_map[exp].get(atm, {})
        ce = atm_data.get("CE", 0) or 0
        pe = atm_data.get("PE", 0) or 0
        if ce == 0 and pe == 0:
            continue
        total = ce + pe
        pct = (total / underlying) * 100 if underlying > 0 else 0
        ce_pct = (ce / underlying) * 100 if underlying > 0 else 0
        pe_pct = (pe / underlying) * 100 if underlying > 0 else 0
        all_expiries.append(exp)
        expiry_results.append({
            "expiry": exp,
            "strike": atm,
            "ce_price": round(ce, 2),
            "pe_price": round(pe, 2),
            "ce_pct": round(ce_pct, 2),
            "pe_pct": round(pe_pct, 2),
            "total_premium": round(total, 2),
            "premium_pct": round(pct, 2),
        })
    return expiry_results, lot_size, all_expiries, underlying


def _refresh_once(browser, concurrency):
    """One full pass: fetch prices + option chains, build & cache snapshot.

    Dispatches the whole universe to browser.fetch_chains_concurrent, which
    runs N fetches in parallel across N warmed pages sharing one context.
    """
    nse = NSELive()
    equity_prices = _get_equity_prices(nse)
    index_prices = _get_index_prices(nse)
    if not equity_prices and not index_prices:
        return None  # Can't build anything useful

    # Build task list. Each task is (symbol, chain_type, max_expiries, key)
    # where `key` is the metadata we need to build the result row.
    tasks = []
    for sym in sorted(index_prices.keys()):
        max_exp = 6 if sym == "NIFTY" else 4
        key = {"symbol": sym, "type": "index", "ltp": index_prices[sym]}
        tasks.append((sym, "Indices", max_exp, key))
    for sym in sorted(equity_prices.keys()):
        key = {"symbol": sym, "type": "equity", "ltp": equity_prices[sym]}
        tasks.append((sym, "Equity", 4, key))

    fetched = browser.fetch_chains_concurrent(tasks, concurrency=concurrency)

    results = []
    all_expiries_set = set()
    errors = []
    option_data_available = False

    for key, records in fetched:
        sym = key["symbol"]
        ltp = key["ltp"]
        row = {
            "symbol": sym,
            "type": key["type"],
            "ltp": round(ltp, 2),
            "lot_size": 1,
            "expiries": [],
            "nearest_premium_pct": 0,
        }
        if not records or not records.get("data"):
            errors.append(f"{sym}: option chain empty")
            results.append(row)
            continue
        expiry_results, lot_size, exps, _ = _records_to_expiry_list(records, ltp)
        row["lot_size"] = lot_size
        row["expiries"] = expiry_results
        row["nearest_premium_pct"] = expiry_results[0]["premium_pct"] if expiry_results else 0
        results.append(row)
        if expiry_results:
            option_data_available = True
            all_expiries_set.update(exps)

    all_expiries = sorted(all_expiries_set, key=_parse_nse_date)
    results.sort(key=lambda x: x["nearest_premium_pct"], reverse=True)

    payload = {
        "status": "success",
        "data": results,
        "all_expiries": all_expiries,
        "source": "nse",
        "errors": errors,
        "market_note": "" if option_data_available else
            "Option chain data is only available during market hours (Mon-Fri, ~9 AM - 3:30 PM IST). Showing stock prices only.",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    if option_data_available:
        _save_cache(payload)
    return payload


def _background_refresh_loop():
    """Daemon thread: refresh the in-memory snapshot on a timer.

    Uses one NSEBrowser (async playwright on its own event-loop thread).
    Each refresh pass dispatches N concurrent fetches over a pool of pages
    all sharing one BrowserContext (single bot-detection solve).
    """
    from nse_playwright import NSEBrowser

    concurrency = _get_refresh_concurrency()
    browser = NSEBrowser(headless=True)
    try:
        browser.start()
    except Exception as e:
        print(f"[nse_provider] Playwright failed to start: {e}")
        return

    print(f"[nse_provider] refresh loop ready — concurrency={concurrency}")

    while True:
        try:
            t0 = time.time()
            payload = _refresh_once(browser, concurrency)
            if payload is not None:
                with _snapshot_lock:
                    global _snapshot
                    _snapshot = payload
                n_rows = len(payload["data"])
                n_opts = len([r for r in payload["data"] if r["expiries"]])
                n_err = len(payload.get("errors", []))
                print(f"[nse_provider] refresh complete in {time.time()-t0:.1f}s: "
                      f"{n_rows} rows, {n_opts} with options, {n_err} errors")
        except Exception as e:
            print(f"[nse_provider] refresh error: {type(e).__name__}: {e}")
        time.sleep(REFRESH_INTERVAL_SEC)


def start_background_refresh():
    """Start the refresh loop once. Safe to call multiple times."""
    global _refresh_thread
    with _refresh_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        _refresh_thread = threading.Thread(
            target=_background_refresh_loop,
            name="nse-refresh",
            daemon=True,
        )
        _refresh_thread.start()


def _bootstrap_snapshot_from_cache():
    """Seed the in-memory snapshot from disk so the first request isn't empty.

    The cached option data may be stale — overlay fresh prices if we can get
    them cheaply. If not, just return the raw cache.
    """
    cached = _load_cache()
    if not cached:
        return
    nse = NSELive()
    try:
        prices = {**_get_equity_prices(nse), **_get_index_prices(nse)}
    except Exception:
        prices = {}
    if prices:
        by_symbol = {row["symbol"]: row for row in cached.get("data", [])}
        merged = []
        all_symbols = set(prices.keys()) | set(by_symbol.keys())
        for sym in sorted(all_symbols):
            ltp = prices.get(sym)
            cached_row = by_symbol.get(sym)
            if cached_row:
                row = dict(cached_row)
                if ltp:
                    row["ltp"] = round(ltp, 2)
                merged.append(row)
            elif ltp:
                merged.append({
                    "symbol": sym, "ltp": round(ltp, 2), "lot_size": 1,
                    "expiries": [], "nearest_premium_pct": 0,
                })
        merged.sort(key=lambda x: x["nearest_premium_pct"], reverse=True)
        payload = dict(cached)
        payload["data"] = merged
        payload["market_note"] = (
            f"Showing cached option premiums from {cached.get('fetched_at', 'earlier')}. "
            f"Fresh data refreshing in background…"
        )
        payload["status"] = "success"
    else:
        payload = dict(cached)
        payload["market_note"] = (
            f"Showing cached data from {cached.get('fetched_at', 'earlier')}."
        )

    with _snapshot_lock:
        global _snapshot
        _snapshot = payload


def fetch_data():
    """Return the most recent snapshot. Triggers bootstrap on first call."""
    with _snapshot_lock:
        snap = _snapshot
    if snap is None:
        # No in-memory snapshot yet — seed from disk cache
        _bootstrap_snapshot_from_cache()
        with _snapshot_lock:
            snap = _snapshot
    if snap is None:
        return {
            "status": "success",
            "data": [],
            "all_expiries": [],
            "source": "nse",
            "errors": [],
            "market_note":
                "First refresh in progress… reload in a minute for live data.",
        }
    return snap
