"""Backtester: runs a strategy over historical bars, long-only,
all-in/all-out, fractional shares (paper-math simplification, noted in the
PRD). Signals execute at the NEXT bar's open to avoid look-ahead bias.
Pure stdlib — works fully offline on fixture data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .market_data import Bar
from .strategies import Strategy


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str = ""
    exit_price: float = 0.0
    qty: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    start_cash: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.symbol} [{self.strategy}] "
            f"return {self.total_return_pct:+.2f}% | "
            f"max DD {self.max_drawdown_pct:.2f}% | "
            f"win rate {self.win_rate_pct:.0f}% | "
            f"trades {len(self.trades)} | "
            f"equity ${self.final_equity:,.2f}"
        )


def run_backtest(
    symbol: str,
    bars: list[Bar],
    strategy: Strategy,
    start_cash: float = 10_000.0,
) -> BacktestResult:
    signals = strategy.evaluate(bars)
    cash = start_cash
    qty = 0.0
    open_trade: Trade | None = None
    trades: list[Trade] = []
    equity_curve: list[float] = []

    for i, bar in enumerate(bars):
        # Execute YESTERDAY's signal at TODAY's open (no look-ahead).
        if i > 0:
            action = signals[i - 1].action
            if action == "buy" and qty == 0.0 and cash > 0:
                qty = cash / bar.open
                open_trade = Trade(
                    symbol=symbol,
                    entry_date=bar.date,
                    entry_price=bar.open,
                    qty=qty,
                )
                cash = 0.0
            elif action == "sell" and qty > 0.0 and open_trade is not None:
                cash = qty * bar.open
                open_trade.exit_date = bar.date
                open_trade.exit_price = bar.open
                open_trade.pnl = cash - open_trade.qty * open_trade.entry_price
                open_trade.exit_reason = signals[i - 1].reason
                trades.append(open_trade)
                open_trade = None
                qty = 0.0
        equity_curve.append(cash + qty * bar.close)

    # Close any open position at the last close for reporting.
    if open_trade is not None and bars:
        last = bars[-1]
        proceeds = qty * last.close
        open_trade.exit_date = last.date
        open_trade.exit_price = last.close
        open_trade.pnl = proceeds - open_trade.qty * open_trade.entry_price
        open_trade.exit_reason = "end of data (marked to market)"
        trades.append(open_trade)
        cash = proceeds
        qty = 0.0
        if equity_curve:
            equity_curve[-1] = cash

    final_equity = equity_curve[-1] if equity_curve else start_cash
    peak = start_cash
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100.0)
    closed = [t for t in trades]
    wins = [t for t in closed if t.pnl > 0]
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0

    return BacktestResult(
        symbol=symbol,
        strategy=getattr(strategy, "name", "strategy"),
        start_cash=start_cash,
        final_equity=final_equity,
        total_return_pct=(final_equity / start_cash - 1) * 100.0 if start_cash else 0.0,
        max_drawdown_pct=max_dd,
        win_rate_pct=win_rate,
        trades=trades,
        equity_curve=equity_curve,
    )
