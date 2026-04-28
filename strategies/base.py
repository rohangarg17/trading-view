"""Base class for all trading strategies."""

from dataclasses import dataclass, field


@dataclass
class Trade:
    """A single trade entry."""
    entry_date: str
    exit_date: str
    stock_price_entry: float
    stock_price_exit: float
    call_strike: float = 0
    call_premium_received: float = 0
    call_value_at_exit: float = 0
    put_strike: float = 0
    put_premium_paid: float = 0
    put_value_at_exit: float = 0
    net_credit: float = 0        # call premium - put premium
    net_credit_pct: float = 0    # net credit as % of stock price
    pnl: float = 0
    pnl_pct: float = 0
    iv_at_entry: float = 0
    days_held: int = 0


@dataclass
class BacktestResult:
    """Results of a backtest run."""
    strategy_name: str
    symbol: str
    params: dict
    trades: list  # list of Trade dicts
    summary: dict = field(default_factory=dict)


class Strategy:
    """Base strategy class. Subclass this to create new strategies."""

    name = "base"
    description = "Base strategy"
    param_schema = []  # list of {name, type, default, label, min, max, step}

    def run(self, dates, opens, highs, lows, closes, volumes, volatilities, params):
        """Run the strategy on historical data.

        Args:
            dates: list of date strings (YYYY-MM-DD)
            opens/highs/lows/closes/volumes: lists of float
            volatilities: list of annualized vol (None for first N entries)
            params: dict of strategy parameters

        Returns:
            list of Trade objects
        """
        raise NotImplementedError
