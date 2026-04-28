"""Technical indicators and forecasting helpers built on numpy."""

import math
import numpy as np
from scipy.stats import norm


def _nan_pad(arr, total_len):
    """Left-pad a short result with Nones so it aligns with the source series."""
    pad = total_len - len(arr)
    return [None] * pad + list(arr)


def sma(closes, window):
    """Simple moving average. Returns list of length len(closes); first window-1 entries are None."""
    if len(closes) < window:
        return [None] * len(closes)
    arr = np.asarray(closes, dtype=float)
    kernel = np.ones(window) / window
    smoothed = np.convolve(arr, kernel, mode="valid")
    return [None] * (window - 1) + smoothed.tolist()


def ema(closes, period):
    """Exponential moving average (seeded with SMA of the first `period` values)."""
    n = len(closes)
    if n < period:
        return [None] * n
    arr = np.asarray(closes, dtype=float)
    alpha = 2 / (period + 1)
    out = [None] * (period - 1)
    seed = float(arr[:period].mean())
    out.append(seed)
    for i in range(period, n):
        out.append(out[-1] + alpha * (arr[i] - out[-1]))
    return out


def rsi(closes, period=14):
    """Wilder's RSI. Returns list of length len(closes); leading entries are None."""
    n = len(closes)
    if n <= period:
        return [None] * n
    arr = np.asarray(closes, dtype=float)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    rsis = [None] * period  # RSI not defined for the first `period` closes

    def _rsi_from(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1 + rs)

    rsis.append(_rsi_from(avg_gain, avg_loss))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsis.append(_rsi_from(avg_gain, avg_loss))
    return rsis


def macd(closes, fast=12, slow=26, signal=9):
    """MACD line, signal line, and histogram. Each list length == len(closes)."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    # Signal line: EMA of MACD values (skip leading Nones)
    valid = [v for v in macd_line if v is not None]
    signal_vals = ema(valid, signal) if len(valid) >= signal else []
    signal_line = [None] * (len(macd_line) - len(signal_vals)) + signal_vals
    histogram = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def bollinger(closes, window=20, num_std=2):
    """Bollinger Bands. Returns dict of three lists length len(closes)."""
    n = len(closes)
    if n < window:
        return {"upper": [None] * n, "middle": [None] * n, "lower": [None] * n}
    arr = np.asarray(closes, dtype=float)
    middle = sma(closes, window)
    upper = [None] * (window - 1)
    lower = [None] * (window - 1)
    for i in range(window - 1, n):
        window_slice = arr[i - window + 1:i + 1]
        std = float(window_slice.std(ddof=0))
        upper.append(middle[i] + num_std * std)
        lower.append(middle[i] - num_std * std)
    return {"upper": upper, "middle": middle, "lower": lower}


def atr(highs, lows, closes, period=14):
    """Average True Range using Wilder's smoothing."""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])

    out = [None] * period
    first = float(tr[1:period + 1].mean())
    out.append(first)
    for i in range(period + 1, n):
        out.append((out[-1] * (period - 1) + float(tr[i])) / period)
    return out


def monte_carlo_cone(spot, sigma, days=60, percentiles=(5, 50, 95), r=0.07):
    """Project price percentiles under a lognormal GBM.

    Returns dict with 'days' (0..days) and one list per percentile. Uses 252
    trading days/year for time scaling.
    """
    out = {"days": list(range(days + 1))}
    for p in percentiles:
        z = norm.ppf(p / 100)
        series = []
        for d in out["days"]:
            T = d / 252
            drift = (r - 0.5 * sigma ** 2) * T
            diffusion = sigma * math.sqrt(T) * z
            series.append(round(spot * math.exp(drift + diffusion), 2))
        out[f"p{p}"] = series
    return out
