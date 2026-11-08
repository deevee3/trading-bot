"""Strategy interface + the reference SMA-crossover strategy.

A strategy is a pure function over bars: no network, no orders. It returns
one Signal per bar so the SAME code path drives both the backtester and the
live paper runner — what you backtest is exactly what would paper-trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .market_data import Bar


@dataclass
class Signal:
    date: str
    action: str  # "buy" | "sell" | "hold"
    reason: str = ""


class Strategy(Protocol):
    name: str

    def evaluate(self, bars: list[Bar]) -> list[Signal]:
        """One Signal per input bar, same order."""
        ...


def _sma(values: list[float], window: int, idx: int) -> float | None:
    if idx + 1 < window:
        return None
    return sum(values[idx + 1 - window : idx + 1]) / window


@dataclass
class SmaCrossover:
    """Buy when the fast SMA crosses ABOVE the slow SMA; sell when it
    crosses back below. Long-only, all-in/all-out — deliberately simple so
    its behavior is easy to reason about and audit."""

    fast: int = 10
    slow: int = 30
    name: str = "sma_crossover"

    def evaluate(self, bars: list[Bar]) -> list[Signal]:
        closes = [b.close for b in bars]
        signals: list[Signal] = []
        prev_diff: float | None = None
        for i, bar in enumerate(bars):
            fast = _sma(closes, self.fast, i)
            slow = _sma(closes, self.slow, i)
            if fast is None or slow is None:
                signals.append(Signal(bar.date, "hold", "warming up"))
                continue
            diff = fast - slow
            action, reason = "hold", ""
            if prev_diff is not None:
                if prev_diff <= 0 < diff:
                    action = "buy"
                    reason = (
                        f"SMA{self.fast} crossed above SMA{self.slow} "
                        f"({fast:.2f} > {slow:.2f})"
                    )
                elif prev_diff >= 0 > diff:
                    action = "sell"
                    reason = (
                        f"SMA{self.fast} crossed below SMA{self.slow} "
                        f"({fast:.2f} < {slow:.2f})"
                    )
            signals.append(Signal(bar.date, action, reason))
            prev_diff = diff
        return signals


def build_strategy(config) -> SmaCrossover:
    """Factory so new strategies slot in behind the same interface."""
    return SmaCrossover(fast=config.sma_fast, slow=config.sma_slow)
