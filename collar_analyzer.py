"""Collar strategy analyzer for the per-stock details page.

Strategy encoded:
    - Buy 1 lot of the stock at spot
    - Sell 1 ATM (or near-ATM) call at some strike K_c >= spot
    - Buy 1 OTM put at some strike K_p <= spot
    - Hold to expiry, then mark-to-market

User constraints:
    - Call strike within 5% above ATM (K_c in [spot, spot * 1.05])
    - Put strike 3-10% below spot
    - Only expiries 20-40 days out (monthly cycle)
    - Ignore pairs with negative net credit

For every valid (K_c, K_p) pair using live market premiums, we compute:

    net_credit       = call_premium - put_premium
    max_profit       = (K_c - spot) + net_credit   (stock called away at K_c)
    max_loss         = (spot - K_p) - net_credit   (stock crashes below K_p)
    worst_return_pct = -max_loss / spot * 100      (floor on the trade)
    best_return_pct  = max_profit / spot * 100     (ceiling on the trade)
    rr               = max_profit / max_loss       (reward-to-risk ratio)

We rank pairs by three independent metrics so the user can compare:
    1. Max worst-case return (safest floor)
    2. Max net credit % (highest monthly income if stock is flat)
    3. Max reward-to-risk ratio (best shape regardless of magnitude)
"""

from datetime import datetime


CALL_MAX_PCT_ABOVE_ATM = 0.05    # call strike K_c in [spot, spot * 1.05]
PUT_MIN_PCT_BELOW = 0.03         # put strike K_p <= spot * 0.97
PUT_MAX_PCT_BELOW = 0.10         # put strike K_p >= spot * 0.90
EXPIRY_MIN_DAYS = 20
EXPIRY_MAX_DAYS = 40


def _parse_nse_date(s):
    """NSE uses 'DD-MMM-YYYY' e.g. '28-Apr-2026'. Returns date or None."""
    try:
        return datetime.strptime(s, "%d-%b-%Y").date()
    except (ValueError, TypeError):
        return None


def pick_target_expiry(expiry_dates, today=None):
    """Pick the first expiry in [today+EXPIRY_MIN_DAYS, today+EXPIRY_MAX_DAYS].

    If none fall in that window, fall back to the expiry closest to 30 days.
    Returns (expiry_str, days_to_expiry) or (None, None).
    """
    today = today or datetime.now().date()
    candidates = []
    for s in expiry_dates or []:
        d = _parse_nse_date(s)
        if d is None:
            continue
        dte = (d - today).days
        if dte < 1:
            continue
        candidates.append((s, dte))

    if not candidates:
        return None, None

    in_window = [c for c in candidates if EXPIRY_MIN_DAYS <= c[1] <= EXPIRY_MAX_DAYS]
    if in_window:
        # prefer closest to 30 days among those in window
        in_window.sort(key=lambda c: abs(c[1] - 30))
        return in_window[0]
    # Fallback — nothing in window, pick whatever is closest to 30 days
    candidates.sort(key=lambda c: abs(c[1] - 30))
    return candidates[0]


def _option_price(leg):
    """LTP if the strike traded today, else bid/ask mid.

    NSE reports lastPrice=0 for any strike with no trade yet today, even when
    there's a live two-sided quote. Away-from-ATM strikes in the put window
    are often untraded, so LTP-only pricing leaves the scan window empty.
    """
    ltp = leg.get("lastPrice") or 0
    if ltp > 0:
        return ltp
    bid = leg.get("buyPrice1") or 0
    ask = leg.get("sellPrice1") or 0
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    return 0


def _build_strike_tables(records, target_expiry):
    """From NSE records.data, extract {strike: price} dicts for calls & puts
    at the target expiry only.
    """
    calls, puts = {}, {}
    for row in records.get("data", []) or []:
        # v3 uses "expiryDates" (singular date, plural key); v1 used "expiryDate"
        exp = row.get("expiryDates") or row.get("expiryDate")
        if exp != target_expiry:
            continue
        strike = row.get("strikePrice")
        if not strike:
            continue
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        ce_price = _option_price(ce)
        pe_price = _option_price(pe)
        if ce_price > 0:
            calls[strike] = ce_price
        if pe_price > 0:
            puts[strike] = pe_price
    return calls, puts


def _eval_pair(spot, k_call, call_prem, k_put, put_prem):
    """Compute all P&L metrics for one (K_c, K_p) pair."""
    net_credit = call_prem - put_prem
    max_profit = (k_call - spot) + net_credit         # stock pinned/called at K_c
    max_loss = (spot - k_put) - net_credit            # stock tanks below K_p
    worst_ret_pct = -max_loss / spot * 100 if spot > 0 else 0
    best_ret_pct = max_profit / spot * 100 if spot > 0 else 0
    net_credit_pct = net_credit / spot * 100 if spot > 0 else 0
    rr = (max_profit / max_loss) if max_loss > 0 else float("inf")
    return {
        "call_strike": k_call,
        "put_strike": k_put,
        "call_premium": round(call_prem, 2),
        "put_premium": round(put_prem, 2),
        "net_credit": round(net_credit, 2),
        "net_credit_pct": round(net_credit_pct, 2),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "best_return_pct": round(best_ret_pct, 2),
        "worst_return_pct": round(worst_ret_pct, 2),
        "rr_ratio": round(rr, 2) if rr != float("inf") else None,
    }


def analyze(records, spot, expiry=None, top_n=5):
    """Main entrypoint: given NSE records + spot price, return analyzer output.

    If `expiry` is given it is used as-is (user picked it); otherwise one is
    chosen from records.expiryDates via pick_target_expiry.

    Returns dict shaped for the UI:
      {
        "expiry": str | None,
        "days_to_expiry": int | None,
        "spot": float,
        "pair_count": int,
        "top_by_worst_case": [...],
        "top_by_net_credit": [...],
        "top_by_reward_risk": [...],
      }
    """
    empty = {
        "expiry": None, "days_to_expiry": None, "spot": spot,
        "pair_count": 0, "top_by_worst_case": [],
        "top_by_net_credit": [], "top_by_reward_risk": [],
        "reason": "no suitable expiry",
    }
    if not records or not spot or spot <= 0:
        return empty

    if expiry:
        d = _parse_nse_date(expiry)
        dte = (d - datetime.now().date()).days if d else None
    else:
        expiry, dte = pick_target_expiry(records.get("expiryDates", []))
    if not expiry:
        return empty

    calls, puts = _build_strike_tables(records, expiry)
    if not calls or not puts:
        return {**empty, "expiry": expiry, "days_to_expiry": dte,
                "reason": "no live premiums at this expiry"}

    # Filter call strikes within [spot, spot * 1.05]
    valid_calls = [
        (k, calls[k]) for k in sorted(calls.keys())
        if spot <= k <= spot * (1 + CALL_MAX_PCT_ABOVE_ATM)
    ]
    # Filter put strikes within [spot * 0.90, spot * 0.97]
    valid_puts = [
        (k, puts[k]) for k in sorted(puts.keys())
        if spot * (1 - PUT_MAX_PCT_BELOW) <= k <= spot * (1 - PUT_MIN_PCT_BELOW)
    ]
    if not valid_calls or not valid_puts:
        return {**empty, "expiry": expiry, "days_to_expiry": dte,
                "reason": "no strikes in configured window"}

    pairs = []
    for k_call, call_prem in valid_calls:
        for k_put, put_prem in valid_puts:
            m = _eval_pair(spot, k_call, call_prem, k_put, put_prem)
            # Skip pairs with no credit — you said 1% is the floor even in the
            # best case, so paying a net debit is always a losing setup.
            if m["net_credit"] <= 0:
                continue
            pairs.append(m)

    if not pairs:
        return {**empty, "expiry": expiry, "days_to_expiry": dte,
                "reason": "all candidate pairs are net debit"}

    # Rank by three independent objectives. Each "top" is up to top_n entries.
    top_worst = sorted(pairs, key=lambda p: p["worst_return_pct"], reverse=True)[:top_n]
    top_credit = sorted(pairs, key=lambda p: p["net_credit_pct"], reverse=True)[:top_n]
    # For RR, treat None (infinite = put > spot - put_prem adjusted) as best,
    # but realistically we want the highest finite ratio.
    finite_rr = [p for p in pairs if p["rr_ratio"] is not None]
    top_rr = sorted(finite_rr, key=lambda p: p["rr_ratio"], reverse=True)[:top_n]

    return {
        "expiry": expiry,
        "days_to_expiry": dte,
        "spot": spot,
        "pair_count": len(pairs),
        "top_by_worst_case": top_worst,
        "top_by_net_credit": top_credit,
        "top_by_reward_risk": top_rr,
    }
