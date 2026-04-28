"""Paid data provider using Zerodha Kite Connect APIs."""

import os
from kiteconnect import KiteConnect

NIFTY50_SYMBOLS = [
    "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BPCL", "BHARTIARTL", "BRITANNIA",
    "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TCS", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TECHM",
    "TITAN", "TRENT", "ULTRACEMCO", "WIPRO", "ETERNAL",
]


def _get_kite():
    kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
    kite.set_access_token(os.getenv("KITE_ACCESS_TOKEN"))
    return kite


def _find_atm_strike(ltp, strike_list):
    return min(strike_list, key=lambda s: abs(s - ltp))


def fetch_data():
    """Fetch Nifty 50 option chain data from Kite Connect."""
    kite = _get_kite()

    # 1. Get all NFO instruments for Nifty 50 stocks
    nfo_instruments = kite.instruments("NFO")
    options = {}
    expiries_set = set()
    lot_sizes = {}

    for inst in nfo_instruments:
        sym = inst["name"]
        if sym in NIFTY50_SYMBOLS and inst["instrument_type"] in ("CE", "PE"):
            options.setdefault(sym, []).append(inst)
            expiries_set.add(str(inst["expiry"]))
            if sym not in lot_sizes:
                lot_sizes[sym] = inst["lot_size"]

    # 2. Fetch live equity prices
    equity_symbols = [f"NSE:{s}" for s in NIFTY50_SYMBOLS]
    equity_quotes = kite.quote(equity_symbols)

    all_expiries = sorted(expiries_set)

    # 3. Build option map: symbol -> expiry -> strike -> {CE: inst, PE: inst}
    option_map = {}
    for sym, insts in options.items():
        option_map[sym] = {}
        for inst in insts:
            exp = str(inst["expiry"])
            strike = inst["strike"]
            otype = inst["instrument_type"]
            option_map[sym].setdefault(exp, {}).setdefault(strike, {})[otype] = inst

    # 4. Find ATM options and collect quote requests
    option_tokens = []
    atm_map = {}

    for sym in NIFTY50_SYMBOLS:
        eq_key = f"NSE:{sym}"
        if eq_key not in equity_quotes or sym not in option_map:
            continue
        ltp = equity_quotes[eq_key]["last_price"]

        for exp in all_expiries:
            if exp not in option_map[sym]:
                continue
            strikes = list(option_map[sym][exp].keys())
            if not strikes:
                continue
            atm_strike = _find_atm_strike(ltp, strikes)
            strike_data = option_map[sym][exp].get(atm_strike, {})

            ce_inst = strike_data.get("CE")
            pe_inst = strike_data.get("PE")
            if ce_inst and pe_inst:
                ce_ts = f"NFO:{ce_inst['tradingsymbol']}"
                pe_ts = f"NFO:{pe_inst['tradingsymbol']}"
                option_tokens.extend([ce_ts, pe_ts])
                atm_map[(sym, exp)] = {
                    "ce_ts": ce_ts, "pe_ts": pe_ts, "strike": atm_strike,
                }

    # 5. Fetch option quotes in batches
    option_quotes = {}
    for i in range(0, len(option_tokens), 400):
        batch = option_tokens[i:i + 400]
        option_quotes.update(kite.quote(batch))

    # 6. Build response
    results = []
    for sym in NIFTY50_SYMBOLS:
        eq_key = f"NSE:{sym}"
        if eq_key not in equity_quotes:
            continue
        ltp = equity_quotes[eq_key]["last_price"]
        lot_size = lot_sizes.get(sym, 1)

        expiry_data = []
        for exp in all_expiries:
            key = (sym, exp)
            if key not in atm_map:
                continue
            info = atm_map[key]
            ce_quote = option_quotes.get(info["ce_ts"])
            pe_quote = option_quotes.get(info["pe_ts"])
            if not ce_quote or not pe_quote:
                continue

            ce_price = ce_quote["last_price"]
            pe_price = pe_quote["last_price"]
            total_premium = ce_price + pe_price
            premium_pct = (total_premium / ltp) * 100 if ltp > 0 else 0

            expiry_data.append({
                "expiry": exp,
                "strike": info["strike"],
                "ce_price": round(ce_price, 2),
                "pe_price": round(pe_price, 2),
                "total_premium": round(total_premium, 2),
                "premium_pct": round(premium_pct, 2),
            })

        if expiry_data:
            results.append({
                "symbol": sym,
                "ltp": round(ltp, 2),
                "lot_size": lot_size,
                "expiries": expiry_data,
                "nearest_premium_pct": expiry_data[0]["premium_pct"],
            })

    results.sort(key=lambda x: x["nearest_premium_pct"], reverse=True)

    return {
        "status": "success",
        "data": results,
        "all_expiries": all_expiries,
        "source": "kite",
    }
