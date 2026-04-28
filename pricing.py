"""Black-Scholes option pricing and Greeks for backtesting.

Since free historical option chain data is unavailable, we estimate
option prices from historical stock prices using Black-Scholes.
"""

import math
from scipy.stats import norm


RISK_FREE_RATE = 0.07  # ~7% Indian govt bond yield

# IV skew parameters — empirically, NSE stock options show:
#   - ATM IV ≈ historical vol
#   - OTM calls: IV slightly higher (demand for upside)
#   - OTM puts: IV significantly higher (crash protection demand)
# We model this as: IV(K) = base_iv * (1 + skew_slope * moneyness^2 + put_skew_factor)
SKEW_CURVATURE = 1.5       # quadratic smile steepness
PUT_SKEW_FACTOR = 0.20     # puts get 20% higher IV than calls at same distance from ATM


def skew_adjusted_iv(base_iv, S, K, option_type="CE"):
    """Apply a realistic IV smile/skew to the base (ATM) volatility.

    In real markets, OTM options trade at higher IV than ATM.
    This makes the call scan non-trivial: slightly OTM calls can have
    disproportionately high premiums due to the smile.
    """
    moneyness = (K - S) / S  # positive for OTM calls, negative for OTM puts
    # Quadratic smile: IV increases as you move away from ATM in either direction
    smile_adj = SKEW_CURVATURE * moneyness ** 2
    # Proportional extra skew for puts (crash premium) — scales with base_iv
    put_adj = PUT_SKEW_FACTOR * smile_adj if option_type == "PE" and moneyness < 0 else 0
    return base_iv * (1 + smile_adj + put_adj)


def bs_call(S, K, T, r, sigma):
    """Black-Scholes call price.

    Args:
        S: Current stock price
        K: Strike price
        T: Time to expiry in years (e.g. 30 days = 30/365)
        r: Risk-free rate (annualized)
        sigma: Volatility (annualized)
    """
    if T <= 0 or sigma <= 0:
        return max(S - K, 0)
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def bs_put(S, K, T, r, sigma):
    """Black-Scholes put price."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def historical_volatility(prices, window=20):
    """Calculate rolling annualized volatility from a list of close prices.

    Returns a list of the same length (first `window` entries are None).
    """
    import numpy as np
    prices = np.array(prices, dtype=float)
    log_returns = np.diff(np.log(prices))
    vols = [None] * window
    for i in range(window, len(log_returns) + 1):
        window_returns = log_returns[i - window:i]
        daily_vol = np.std(window_returns, ddof=1)
        annual_vol = daily_vol * math.sqrt(252)
        vols.append(annual_vol)
    return vols


def bs_call_delta(S, K, T, r, sigma):
    """Delta of a call option (probability of finishing ITM, roughly)."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    return norm.cdf(d1)


def expected_collar_return(S, K_call, K_put, T, r, sigma, n_points=200):
    """Estimate expected collar return by integrating over lognormal distribution.

    Simulates many possible exit prices and computes average P&L.
    More realistic than just using the single realized exit price.
    """
    import numpy as np
    # Generate possible exit prices from lognormal distribution
    drift = (r - 0.5 * sigma**2) * T
    diffusion = sigma * math.sqrt(T)
    z = np.linspace(-3.5, 3.5, n_points)
    S_exits = S * np.exp(drift + diffusion * z)
    # Standard normal PDF for weighting
    weights = norm.pdf(z)
    weights /= weights.sum()

    call_prem = bs_call(S, K_call, T, r, sigma)
    put_prem = bs_put(S, K_put, T, r, sigma)

    pnls = []
    for S_exit, w in zip(S_exits, weights):
        stock_pnl = S_exit - S
        call_pnl = call_prem - max(S_exit - K_call, 0)
        put_pnl = max(K_put - S_exit, 0) - put_prem
        pnls.append((stock_pnl + call_pnl + put_pnl) * w)

    expected_pnl = sum(pnls)
    return expected_pnl / S  # as fraction of entry price


def generate_strikes_in_range(price, min_pct, max_pct, step):
    """Generate all valid strikes between price*(1+min_pct) and price*(1+max_pct)."""
    low = price * (1 + min_pct)
    high = price * (1 + max_pct)
    first = math.ceil(low / step) * step
    strikes = []
    k = first
    while k <= high:
        strikes.append(k)
        k += step
    # Ensure at least one strike
    if not strikes:
        strikes.append(round(price * (1 + (min_pct + max_pct) / 2) / step) * step)
    return strikes


def round_to_strike(price, step=50):
    """Round a price to the nearest strike step (NSE uses multiples of 50 for most stocks)."""
    return round(price / step) * step


def get_strike_step(price):
    """Determine appropriate strike step based on stock price level."""
    if price < 100:
        return 2.5
    elif price < 500:
        return 5
    elif price < 1000:
        return 10
    elif price < 2500:
        return 25
    elif price < 5000:
        return 50
    else:
        return 100
