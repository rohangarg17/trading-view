"""Orchestrator for the per-stock details page.

Pulls OHLCV via backtester.fetch_history, computes technical indicators,
and layers on yfinance .info / .recommendations / .news. Each section is
best-effort: a failure in one section returns a null slot rather than
breaking the whole payload.
"""

import time
from datetime import datetime, timezone

import yfinance as yf

from backtester import fetch_history, YAHOO_SYMBOL_MAP
from pricing import historical_volatility
from indicators import rsi, macd, sma, bollinger, atr, monte_carlo_cone
from collar_analyzer import analyze as analyze_collar, pick_target_expiry


_CACHE = {}
_CACHE_TTL_SEC = 60


def _yahoo_symbol(symbol):
    return YAHOO_SYMBOL_MAP.get(symbol, f"{symbol}.NS")


def _last_valid(series):
    for v in reversed(series):
        if v is not None and not (isinstance(v, float) and v != v):  # skip NaN
            return v
    return None


def _rsi_signal(value):
    if value is None:
        return "neutral"
    if value >= 70:
        return "sell"
    if value <= 30:
        return "buy"
    return "neutral"


def _macd_signal(hist_last, hist_prev):
    if hist_last is None or hist_prev is None:
        return "neutral"
    if hist_prev <= 0 < hist_last:
        return "buy"
    if hist_prev >= 0 > hist_last:
        return "sell"
    return "bullish" if hist_last > 0 else "bearish"


def _ma_signal(price, ma50, ma200):
    if price is None or ma50 is None or ma200 is None:
        return "neutral"
    if price > ma50 > ma200:
        return "buy"
    if price < ma50 < ma200:
        return "sell"
    return "neutral"


def _bb_signal(price, upper, lower):
    if price is None or upper is None or lower is None:
        return "neutral"
    if price >= upper:
        return "sell"
    if price <= lower:
        return "buy"
    return "neutral"


def _analyst_data(ticker):
    try:
        info = ticker.info or {}
    except Exception:
        return None
    fields = {
        "target_mean": info.get("targetMeanPrice"),
        "target_high": info.get("targetHighPrice"),
        "target_low": info.get("targetLowPrice"),
        "recommendation": info.get("recommendationKey"),
        "num_analysts": info.get("numberOfAnalystOpinions"),
    }
    if all(v is None for v in fields.values()):
        return None
    return fields


def _recommendations(ticker):
    try:
        df = ticker.recommendations
    except Exception:
        return None
    if df is None or df.empty:
        return None
    cols = ["strongBuy", "buy", "hold", "sell", "strongSell"]
    if not any(c in df.columns for c in cols):
        return None
    # Most recent period is usually row 0 with period='0m'
    row = df.iloc[0]
    return {c: int(row[c]) for c in cols if c in df.columns}


def _news(ticker, limit=5):
    try:
        items = ticker.news or []
    except Exception:
        return []
    out = []
    for item in items[:limit]:
        # yfinance has shipped two shapes over time: flat dict and {"content": {...}}
        content = item.get("content") or item
        title = content.get("title")
        link = content.get("link") or content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else content.get("link")
        publisher = content.get("publisher") or (content.get("provider") or {}).get("displayName")
        ts = content.get("providerPublishTime") or content.get("pubDate")
        if isinstance(ts, str):
            try:
                ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
            except ValueError:
                ts = None
        if title and link:
            out.append({
                "title": title,
                "link": link,
                "publisher": publisher,
                "publish_ts": ts,
            })
    return out


def _build_indicators(data):
    closes = data["closes"]
    highs = data["highs"]
    lows = data["lows"]

    rsi_vals = rsi(closes, 14)
    macd_vals = macd(closes)
    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200)
    bb = bollinger(closes, 20, 2)
    atr_vals = atr(highs, lows, closes, 14)

    last_price = closes[-1]
    rsi_last = _last_valid(rsi_vals)
    macd_hist = macd_vals["histogram"]
    hist_last = _last_valid(macd_hist)
    hist_prev = macd_hist[-2] if len(macd_hist) >= 2 else None
    ma50_last = _last_valid(ma50)
    ma200_last = _last_valid(ma200)
    upper_last = _last_valid(bb["upper"])
    lower_last = _last_valid(bb["lower"])
    atr_last = _last_valid(atr_vals)

    return {
        "values": {
            "rsi": round(rsi_last, 2) if rsi_last is not None else None,
            "macd": round(_last_valid(macd_vals["macd"]), 4) if _last_valid(macd_vals["macd"]) is not None else None,
            "macd_signal": round(_last_valid(macd_vals["signal"]), 4) if _last_valid(macd_vals["signal"]) is not None else None,
            "macd_hist": round(hist_last, 4) if hist_last is not None else None,
            "ma50": round(ma50_last, 2) if ma50_last is not None else None,
            "ma200": round(ma200_last, 2) if ma200_last is not None else None,
            "bb_upper": round(upper_last, 2) if upper_last is not None else None,
            "bb_lower": round(lower_last, 2) if lower_last is not None else None,
            "atr": round(atr_last, 2) if atr_last is not None else None,
        },
        "signals": {
            "rsi": _rsi_signal(rsi_last),
            "macd": _macd_signal(hist_last, hist_prev),
            "ma_cross": _ma_signal(last_price, ma50_last, ma200_last),
            "bollinger": _bb_signal(last_price, upper_last, lower_last),
        },
        "series": {
            "bb_upper": [round(v, 2) if v is not None else None for v in bb["upper"]],
            "bb_middle": [round(v, 2) if v is not None else None for v in bb["middle"]],
            "bb_lower": [round(v, 2) if v is not None else None for v in bb["lower"]],
        },
    }


def _price_summary(data):
    closes = data["closes"]
    volumes = data["volumes"]
    last = closes[-1]
    window = closes[-252:] if len(closes) >= 252 else closes
    w_high = max(window)
    w_low = min(window)
    position = ((last - w_low) / (w_high - w_low) * 100) if w_high > w_low else 0
    vol_recent = volumes[-1] if volumes else 0
    vol_avg = (sum(volumes[-20:]) / min(20, len(volumes))) if volumes else 0
    return {
        "last": round(last, 2),
        "week52_high": round(w_high, 2),
        "week52_low": round(w_low, 2),
        "week52_position_pct": round(position, 1),
        "volume_latest": int(vol_recent),
        "volume_avg_20d": int(vol_avg),
        "volume_ratio": round(vol_recent / vol_avg, 2) if vol_avg > 0 else None,
    }


def _collar(symbol, spot):
    """Run the collar analyzer for this stock using the background NSEBrowser.

    Picks an expiry in the 20-40 day window, fetches its full option chain,
    then runs collar_analyzer.analyze. Returns None if the browser isn't up
    yet or NSE returned nothing usable — the UI shows a graceful empty state.
    """
    try:
        from nse_provider import get_shared_browser, _snapshot, _snapshot_lock
    except Exception:
        return None
    browser = get_shared_browser()
    if browser is None:
        return {"reason": "option-chain browser not ready yet — refresh in a minute"}

    # The background snapshot already tells us which expiries exist for this
    # symbol. No need to re-probe NSE just to learn expiry dates.
    with _snapshot_lock:
        snap = _snapshot
    expiry_str = None
    chain_type = "Equity"
    if snap:
        for row in snap.get("data", []):
            if row.get("symbol") == symbol:
                expiries = [e["expiry"] for e in row.get("expiries", [])]
                expiry_str, _ = pick_target_expiry(expiries)
                if row.get("type") == "index":
                    chain_type = "Indices"
                break
    if not expiry_str:
        return {"reason": "no suitable 20-40 day expiry found for this symbol"}

    try:
        records = browser.fetch_full_chain(symbol, expiry_str, chain_type=chain_type)
    except Exception as e:
        return {"reason": f"chain fetch failed: {type(e).__name__}"}
    if not records:
        return {"reason": f"NSE returned empty chain for {expiry_str}"}

    return analyze_collar(records, spot=spot, top_n=5)


def _forecast(data, analyst):
    closes = data["closes"]
    vols = historical_volatility(closes, window=20)
    sigma = _last_valid(vols)
    if sigma is None or sigma <= 0:
        return None
    cone = monte_carlo_cone(closes[-1], sigma, days=60)
    return {
        "cone": cone,
        "sigma_annual": round(sigma, 4),
        "analyst_targets": {
            "mean": analyst.get("target_mean") if analyst else None,
            "high": analyst.get("target_high") if analyst else None,
            "low": analyst.get("target_low") if analyst else None,
        } if analyst else None,
    }


def fetch_details(symbol):
    """Fetch all data needed to render the stock details page."""
    now = time.time()
    cached = _CACHE.get(symbol)
    if cached and now - cached["ts"] < _CACHE_TTL_SEC:
        return cached["payload"]

    data = fetch_history(symbol, period="2y")
    if not data["closes"]:
        raise ValueError(f"No price data for {symbol}")

    ticker = yf.Ticker(_yahoo_symbol(symbol))

    indicators_block = _build_indicators(data)
    analyst = _analyst_data(ticker)
    price = _price_summary(data)
    forecast = _forecast(data, analyst)
    collar = _collar(symbol, price["last"])

    payload = {
        "status": "success",
        "symbol": symbol,
        "price": price,
        "indicators": indicators_block,
        "analyst": analyst,
        "recommendations": _recommendations(ticker),
        "news": _news(ticker),
        "forecast": forecast,
        "collar": collar,
        "price_history": {
            "dates": data["dates"][-252:],
            "closes": [round(c, 2) for c in data["closes"][-252:]],
            "bb_upper": indicators_block["series"]["bb_upper"][-252:],
            "bb_lower": indicators_block["series"]["bb_lower"][-252:],
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    _CACHE[symbol] = {"ts": now, "payload": payload}
    return payload
