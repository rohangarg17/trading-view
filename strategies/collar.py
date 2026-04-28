"""Collar Strategy: Buy stock + Sell OTM Call + Buy OTM Put.

Monthly cycle:
  - At the start of each month, scan for the best call strike
  - Check if net credit (call premium - put cost) >= minimum threshold
  - If yes: enter the collar, hold till month end, square off
  - If no: skip that month entirely

The call strike is dynamically selected by scanning a range (e.g. 0-10%
above entry) and picking the strike that maximizes the chosen objective.
"""

import math
from datetime import datetime
from strategies.base import Strategy, Trade
from pricing import (
    bs_call, bs_put, bs_call_delta, get_strike_step,
    expected_collar_return, generate_strikes_in_range,
    skew_adjusted_iv, RISK_FREE_RATE,
)


OBJECTIVES = {
    "max_return_on_risk": "Max reward/risk ratio of the collar",
    "max_net_credit": "Max net credit (call - put premium)",
    "max_premium": "Max call premium (picks lowest strike in range)",
    "max_premium_efficiency": "Max premium per unit assignment risk",
    "max_expected_return": "Max expected return (favors high upside)",
}


def _find_month_boundaries(dates):
    """Group trading day indices by (year, month).

    Returns list of (month_label, first_idx, last_idx) tuples.
    """
    months = []
    current_month = None
    start_idx = 0

    for i, d in enumerate(dates):
        dt = datetime.strptime(d, "%Y-%m-%d")
        ym = (dt.year, dt.month)
        if ym != current_month:
            if current_month is not None:
                label = f"{current_month[0]}-{current_month[1]:02d}"
                months.append((label, start_idx, i - 1))
            current_month = ym
            start_idx = i

    # Last month
    if current_month is not None:
        label = f"{current_month[0]}-{current_month[1]:02d}"
        months.append((label, start_idx, len(dates) - 1))

    return months


def _score_call_strike(entry_price, k_call, put_strike, put_premium, T, vol, objective):
    """Score a candidate call strike based on the chosen objective."""
    call_iv = skew_adjusted_iv(vol, entry_price, k_call, "CE")
    call_prem = bs_call(entry_price, k_call, T, RISK_FREE_RATE, call_iv)

    if objective == "max_return_on_risk":
        net_credit = call_prem - put_premium
        max_profit = (k_call - entry_price) + net_credit
        max_loss = (entry_price - put_strike) - net_credit
        return max_profit / max_loss if max_loss > 0 else max_profit

    elif objective == "max_net_credit":
        return call_prem - put_premium

    elif objective == "max_premium":
        return call_prem

    elif objective == "max_premium_efficiency":
        delta = bs_call_delta(entry_price, k_call, T, RISK_FREE_RATE, call_iv)
        return call_prem / delta if delta > 0.01 else 0

    else:  # max_expected_return
        return expected_collar_return(
            entry_price, k_call, put_strike, T, RISK_FREE_RATE, call_iv
        )


class CollarStrategy(Strategy):

    name = "collar"
    description = (
        "Monthly collar: Buy stock + Sell Call + Buy Put. "
        "Squares off each month. Skips months where net premium is below threshold."
    )

    param_schema = [
        {"name": "min_premium_pct", "type": "float", "default": 1.0,
         "label": "Min net credit % to enter (call prem - put cost, skip if below)",
         "min": 0, "max": 5, "step": 0.25},
        {"name": "call_scan_min_pct", "type": "float", "default": 0.0,
         "label": "Call scan range: min % above entry",
         "min": 0, "max": 15, "step": 0.5},
        {"name": "call_scan_max_pct", "type": "float", "default": 10.0,
         "label": "Call scan range: max % above entry",
         "min": 1, "max": 20, "step": 0.5},
        {"name": "call_objective", "type": "choice", "default": "max_return_on_risk",
         "label": "Call strike selection objective",
         "choices": list(OBJECTIVES.keys()),
         "choice_labels": list(OBJECTIVES.values())},
        {"name": "put_offset_pct", "type": "float", "default": 5.0,
         "label": "Put strike offset % below entry",
         "min": 0, "max": 15, "step": 0.5},
        {"name": "iv_mode", "type": "choice", "default": "fixed",
         "label": "IV source for option pricing",
         "choices": ["fixed", "vrp"],
         "choice_labels": [
             "Fixed IV — set implied vol directly (recommended)",
             "VRP — multiply historical vol by a factor",
         ]},
        {"name": "fixed_iv_pct", "type": "float", "default": 35.0,
         "label": "Fixed implied volatility % (used when IV source = Fixed)",
         "min": 5, "max": 80, "step": 1},
        {"name": "iv_premium_pct", "type": "float", "default": 50.0,
         "label": "VRP: IV premium over historical vol % (used when IV source = VRP)",
         "min": 0, "max": 300, "step": 10},
        {"name": "min_iv", "type": "float", "default": 0.0,
         "label": "Min historical IV to enter trade (0 = always enter)",
         "min": 0, "max": 1.0, "step": 0.05},
        {"name": "lot_size", "type": "int", "default": 1,
         "label": "Number of lots (for P&L scaling)",
         "min": 1, "max": 100, "step": 1},
    ]

    def run(self, dates, opens, highs, lows, closes, volumes, volatilities, params):
        min_premium_pct = params.get("min_premium_pct", 1.0) / 100
        scan_min = params.get("call_scan_min_pct", 0.0) / 100
        scan_max = params.get("call_scan_max_pct", 10.0) / 100
        objective = params.get("call_objective", "max_return_on_risk")
        put_offset_pct = params.get("put_offset_pct", 5.0) / 100
        iv_mode = params.get("iv_mode", "fixed")
        fixed_iv = params.get("fixed_iv_pct", 35.0) / 100
        iv_premium_pct = params.get("iv_premium_pct", 50.0) / 100
        min_iv = params.get("min_iv", 0.0)
        lot_size = params.get("lot_size", 1)

        months = _find_month_boundaries(dates)
        trades = []

        for month_label, entry_idx, exit_idx in months:
            # Need at least a few trading days in the month
            if exit_idx - entry_idx < 3:
                continue

            hist_vol = volatilities[entry_idx] if entry_idx < len(volatilities) else None
            if hist_vol is None or hist_vol <= 0:
                continue

            # IV filter (on raw historical vol)
            if min_iv > 0 and hist_vol < min_iv:
                continue

            # Determine implied vol for option pricing
            if iv_mode == "fixed":
                # Use a fixed IV directly — simplest and most realistic
                # Matches what you see in the option chain (India VIX level)
                vol = fixed_iv
            else:
                # VRP mode: multiply historical vol by a premium factor
                vol = hist_vol * (1 + iv_premium_pct)

            entry_price = closes[entry_idx]
            exit_price = closes[exit_idx]
            step = get_strike_step(entry_price)
            days_in_month = exit_idx - entry_idx
            T = days_in_month / 365.0

            # Put strike — fixed offset below entry
            raw_put_strike = entry_price * (1 - put_offset_pct)
            put_strike = round(raw_put_strike / step) * step
            put_iv = skew_adjusted_iv(vol, entry_price, put_strike, "PE")
            put_premium = bs_put(entry_price, put_strike, T, RISK_FREE_RATE, put_iv)

            # Scan call strikes and pick the best
            candidate_strikes = generate_strikes_in_range(
                entry_price, scan_min, scan_max, step
            )

            best_strike = None
            best_score = -math.inf

            for k_call in candidate_strikes:
                score = _score_call_strike(
                    entry_price, k_call, put_strike, put_premium, T, vol, objective
                )
                if score > best_score:
                    best_score = score
                    best_strike = k_call

            if best_strike is None:
                continue

            call_strike = best_strike
            call_iv = skew_adjusted_iv(vol, entry_price, call_strike, "CE")
            call_premium = bs_call(entry_price, call_strike, T, RISK_FREE_RATE, call_iv)

            # --- Premium gate: skip month if net credit too low ---
            net_credit = call_premium - put_premium
            net_credit_pct = net_credit / entry_price

            if net_credit_pct < min_premium_pct:
                # Not enough premium this month, skip
                continue

            # Realized P&L at month-end square off
            call_exit_value = max(exit_price - call_strike, 0)
            put_exit_value = max(put_strike - exit_price, 0)

            stock_pnl = exit_price - entry_price
            call_pnl = call_premium - call_exit_value
            put_pnl = put_exit_value - put_premium
            total_pnl = stock_pnl + call_pnl + put_pnl
            pnl_pct = (total_pnl / entry_price) * 100

            trades.append(Trade(
                entry_date=dates[entry_idx],
                exit_date=dates[exit_idx],
                stock_price_entry=round(entry_price, 2),
                stock_price_exit=round(exit_price, 2),
                call_strike=call_strike,
                call_premium_received=round(call_premium, 2),
                call_value_at_exit=round(call_exit_value, 2),
                put_strike=put_strike,
                put_premium_paid=round(put_premium, 2),
                put_value_at_exit=round(put_exit_value, 2),
                net_credit=round(net_credit, 2),
                net_credit_pct=round(net_credit_pct * 100, 2),
                pnl=round(total_pnl * lot_size, 2),
                pnl_pct=round(pnl_pct, 2),
                iv_at_entry=round(hist_vol, 4),
                days_held=days_in_month,
            ))

        return trades
