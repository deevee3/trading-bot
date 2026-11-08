"""Trading Bot — Hermes bot team, Bot #4.

PAPER-TRADING ONLY assistant: watches a watchlist, evaluates a pluggable
strategy locally, backtests, and paper-trades through the Alpaca PAPER
API. The Alpaca base URL is hard-locked to the paper host in code; there
is no live-trading code path in this package.

Modules:
    config       .env loading + paper-only guard
    market_data  yfinance / Alpha Vantage / offline fixtures, CSV cache
    strategies   strategy interface + SMA crossover reference strategy
    backtest     offline backtester (same strategy code as the runner)
    paper        Alpaca paper client (mock by default) + guards
    database     SQLite watchlist / decisions / orders / backtests
    runner       one evaluation cycle
    alerts       Telegram alert sender
    telegram_bot admin commands (/watchlist, /backtest, /paper_status)
"""

__version__ = "0.1.0"
