# Trading Bot — PRD (Hermes Bot Team, Bot #4)

**Status:** v0.1 built 2026-10-20. **Cost:** $0/month. **Deploy target:**
Mac Studio or iMac (Docker, or an hourly cron/systemd timer running
`python -m bot --once`).

## ⚠️ PAPER ONLY — read this first

This bot **cannot trade real money, ever.** It talks only to Alpaca's
paper-trading sandbox:

- The Alpaca base URL is hard-locked in code to
  `https://paper-api.alpaca.markets`. Any other host — including the live
  Alpaca API — makes the bot refuse to start.
- Alpaca live API keys (they start with `AK`) are refused at startup;
  paper keys start with `PK`.
- Default mode is **mock**: orders are simulated against a local fake
  $100k account and no network call is made at all. Real paper-API calls
  require paper keys in `.env` **and** `ALPACA_PAPER_ENABLED=true`.
- There is no live-trading code path in this package. Turning this into a
  live bot would be a separate project with a separate explicit decision.

## What it does

Watches a watchlist, evaluates a strategy locally on daily bars, and when
the strategy fires a *fresh* signal, places a paper order, logs it, and
sends a Telegram alert. The same strategy code drives the backtester, so
what you backtest is exactly what would paper-trade.

Pipeline: market data → strategy evaluation (local) → Alpaca paper API →
Telegram alerts + SQLite performance log.

## Watchlist model

- Stored in SQLite (`watchlist` table), seeded from `WATCHLIST` in `.env`
  (default `AAPL,MSFT,SPY`).
- Managed live from Telegram: `/watchlist`, `/watchlist add SYM`,
  `/watchlist del SYM`. Admin-only (ADMIN_USER_ID).
- Daily bars only in v0.1. One position decision per symbol per signal —
  repeat signals are skipped (the last logged non-hold action is the
  guard), so a persistent trend doesn't spam orders.

## Market data

- **Primary:** yfinance (free, no key). Bars cached as CSV in
  `data/cache/` for 6h so cycles and backtests don't re-download.
- **Optional:** Alpha Vantage free tier (`ALPHAVANTAGE_API_KEY`), used
  only if yfinance fails.
- **Offline:** bundled fixture CSV (`fixtures/FIXTURE_daily.csv`, 250
  synthetic bars). `DATA_MODE=fixture` forces offline mode; any symbol
  without live data falls back to fixtures so the bot never hard-fails.

## Strategy interface

A strategy is a pure function: `evaluate(bars) -> list[Signal]`, one
`buy`/`sell`/`hold` signal per bar, no network, no side effects (see
`bot/strategies.py`). Reference implementation: **SMA crossover**
(fast 10 / slow 30, configurable via `SMA_FAST`/`SMA_SLOW`) — buy when the
fast average crosses above the slow, sell when it crosses below.
Long-only. New strategies implement the same interface and plug into both
the backtester and the runner unchanged.

## Backtest approach

- Long-only, all-in/all-out, fractional shares (a paper-math
  simplification — noted so results aren't over-trusted).
- Signals execute at the **next bar's open** (no look-ahead bias).
- Reports: total return, max drawdown, win rate, trade list, final
  equity. Every run is logged to SQLite (`backtests` table).
- Runs fully offline on fixture data; `/backtest SYMBOL` in Telegram or
  the smoke test both exercise it.

## Alert rules

Telegram alert to ADMIN_CHAT_ID on: every paper order placed (symbol,
side, qty, price, reason, mock vs paper-api), and nothing else — holds and
repeat signals stay silent to avoid alert fatigue. Decisions (including
holds) are all in SQLite for review via `/paper_status` and the DB.

## Order sizing

Fixed notional per trade: `TRADE_NOTIONAL_USD` (default $1000) ÷ latest
close, fractional shares allowed on paper. Deliberately small and boring.

## Risk notes

- **Paper results are not predictive.** No slippage/fees are modeled;
  fixture data is synthetic. Treat output as research, not advice.
- Daily-bar strategies react slowly; this is a learning/research tool,
  not an income system.
- yfinance is an unofficial free source — it can break or lag; the cache
  + fixture fallback keeps the bot running but data may be stale (the bot
  logs which source served each run).
- Secrets live only in `.env` on his machine. Nothing in this package
  contains a real key.
- Kill switch: `docker compose down` (or stop the cron). Mock mode is
  the safe default if keys are ever in doubt.

## Acceptance (v0.1)

- [x] Backtester runs offline on fixtures and produces sane metrics.
- [x] Guard rejects live URL / live key / look-alike host (smoke-tested).
- [x] Mock paper round-trip (buy → position → sell) verified.
- [x] Telegram admin commands: /watchlist, /backtest, /paper_status.
- [x] SQLite log of decisions, orders, backtests.
