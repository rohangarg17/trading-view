"""Backtesting engine: fetches historical data and runs strategies."""

import yfinance as yf
import numpy as np
from dataclasses import asdict
from pricing import historical_volatility
from strategies import STRATEGY_REGISTRY


# Some NSE symbols have different tickers on Yahoo Finance
YAHOO_SYMBOL_MAP = {
    "TATAMOTORS": "TATAMTRDVR.NS",  # try alternate
    "M&M": "M%26M.NS",
    "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
    "NIFTY 50": "^NSEI",
    # Index option-chain symbols used by the live dashboard
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NIFTYNXT50": "^NSMIDCP",
    # Yahoo only carries today's bar for these two (no history)
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
}


def fetch_history(symbol, period="2y"):
    """Fetch historical OHLCV data for an NSE stock via Yahoo Finance.

    Args:
        symbol: NSE symbol (e.g. "RELIANCE")
        period: yfinance period string ("1y", "2y", "5y", "max")

    Returns:
        dict with lists: dates, opens, highs, lows, closes, volumes
    """
    yahoo_sym = YAHOO_SYMBOL_MAP.get(symbol, f"{symbol}.NS")
    ticker = yf.Ticker(yahoo_sym)
    df = ticker.history(period=period)
    if df.empty:
        # Retry with .NS suffix if custom mapping failed
        ticker = yf.Ticker(f"{symbol}.NS")
        df = ticker.history(period=period)
    if df.empty:
        raise ValueError(f"No historical data found for {symbol}. Yahoo may use a different ticker.")

    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    return {
        "dates": dates,
        "opens": df["Open"].tolist(),
        "highs": df["High"].tolist(),
        "lows": df["Low"].tolist(),
        "closes": df["Close"].tolist(),
        "volumes": df["Volume"].tolist(),
    }


def run_backtest(symbol, strategy_name, params, period="2y"):
    """Run a backtest for the given strategy on a symbol.

    Returns:
        dict with trades, summary, and chart data
    """
    if strategy_name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy: {strategy_name}. Available: {list(STRATEGY_REGISTRY.keys())}")

    strategy_cls = STRATEGY_REGISTRY[strategy_name]
    strategy = strategy_cls()

    # Fetch data
    data = fetch_history(symbol, period)
    closes = data["closes"]

    # Calculate historical volatility (20-day rolling)
    vols = historical_volatility(closes, window=20)

    # Run strategy
    trades = strategy.run(
        dates=data["dates"],
        opens=data["opens"],
        highs=data["highs"],
        lows=data["lows"],
        closes=data["closes"],
        volumes=data["volumes"],
        volatilities=vols,
        params=params,
    )

    trade_dicts = [asdict(t) for t in trades]

    # Count total months in the data for "months skipped" calc
    from datetime import datetime
    all_months = set()
    for d in data["dates"]:
        dt = datetime.strptime(d, "%Y-%m-%d")
        all_months.add((dt.year, dt.month))
    total_months = len(all_months)

    trade_dicts = [asdict(t) for t in trades]

    # Compute summary statistics
    summary = _compute_summary(trade_dicts, closes, total_months)

    # Build equity curve
    equity_curve = _build_equity_curve(trade_dicts)

    return {
        "strategy": strategy.name,
        "strategy_description": strategy.description,
        "symbol": symbol,
        "period": period,
        "params": params,
        "trades": trade_dicts,
        "summary": summary,
        "equity_curve": equity_curve,
        "price_data": {
            "dates": data["dates"],
            "closes": [round(c, 2) for c in closes],
        },
    }


def _compute_summary(trades, closes, total_months=None):
    if not trades:
        return {
            "total_trades": 0,
            "total_months": total_months or 0,
            "months_skipped": total_months or 0,
            "message": "No trades generated. Try lowering the min premium % or widening the call scan range.",
        }

    pnls = [t["pnl"] for t in trades]
    pnl_pcts = [t["pnl_pct"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    net_credit_pcts = [t.get("net_credit_pct", 0) for t in trades]

    total_pnl = sum(pnls)
    avg_pnl_pct = np.mean(pnl_pcts)
    win_rate = len(winners) / len(trades) * 100

    # Max drawdown on cumulative P&L
    cum_pnl = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum_pnl)
    drawdown = peak - cum_pnl
    max_drawdown = float(np.max(drawdown)) if len(drawdown) > 0 else 0

    # Monthly stats
    months_traded = len(trades)
    months_skipped = (total_months - months_traded) if total_months else 0
    avg_days = np.mean([t["days_held"] for t in trades])

    # Since this is a monthly strategy, annualize from monthly returns
    annualized_return = float(avg_pnl_pct) * 12

    # Sharpe: monthly return / monthly std, annualized
    std_pnl_pct = np.std(pnl_pcts, ddof=1) if len(pnl_pcts) > 1 else 0
    sharpe = (avg_pnl_pct / std_pnl_pct * np.sqrt(12)) if std_pnl_pct > 0 else 0

    return {
        "total_trades": months_traded,
        "total_months": total_months or months_traded,
        "months_skipped": months_skipped,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(float(np.mean(pnls)), 2),
        "avg_pnl_pct": round(float(avg_pnl_pct), 2),
        "avg_monthly_return_pct": round(float(avg_pnl_pct), 2),
        "avg_net_credit_pct": round(float(np.mean(net_credit_pcts)), 2),
        "win_rate_pct": round(win_rate, 1),
        "winners": len(winners),
        "losers": len(losers),
        "best_trade_pct": round(float(max(pnl_pcts)), 2),
        "worst_trade_pct": round(float(min(pnl_pcts)), 2),
        "max_drawdown": round(max_drawdown, 2),
        "annualized_return_pct": round(float(annualized_return), 2),
        "sharpe_ratio": round(float(sharpe), 2),
        "avg_days_held": round(float(avg_days), 1),
        "avg_iv_at_entry": round(float(np.mean([t["iv_at_entry"] for t in trades])), 4),
    }


def _build_equity_curve(trades):
    """Build cumulative P&L curve from trades."""
    if not trades:
        return []
    cum_pnl = 0
    curve = []
    for t in trades:
        cum_pnl += t["pnl_pct"]
        curve.append({
            "date": t["exit_date"],
            "cum_pnl_pct": round(cum_pnl, 2),
        })
    return curve


def get_strategy_info():
    """Return info about all available strategies and their parameters."""
    info = {}
    for name, cls in STRATEGY_REGISTRY.items():
        strategy = cls()
        info[name] = {
            "name": strategy.name,
            "description": strategy.description,
            "params": strategy.param_schema,
        }
    return info
