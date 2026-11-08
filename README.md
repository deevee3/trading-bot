# Trading Bot (PAPER ONLY) — Hermes Bot Team, Bot #4

A paper-trading assistant: watches a watchlist, runs a simple strategy
(SMA crossover) on daily bars, backtests it, paper-trades it through
Alpaca's **paper** sandbox, and reports to you on Telegram.

> **PAPER ONLY.** This bot cannot place a real order. The Alpaca URL is
> hard-locked in code to `https://paper-api.alpaca.markets`; a live URL
> or a live API key makes it refuse to start. Default mode doesn't even
> call Alpaca — orders are simulated locally (mock mode) until you add
> paper keys and explicitly enable them.

## Deploy on the iMac (or Mac Studio) — click by click

### Step 1 — Create the Telegram bot (phone)

1. Open Telegram → search **@BotFather** (blue checkmark) → START.
2. Send `/newbot`, name it e.g. `Glenride Trading Bot`, username e.g.
   `glenride_trading_bot`.
3. Copy the token BotFather gives you into your Notes app. Treat it
   like a password.
4. Message **@userinfobot** → it replies with your user ID. Note it.

### Step 2 — Get FREE Alpaca PAPER keys (browser)

1. Go to **alpaca.markets** and create a free account.
2. In the dashboard, switch to **Paper Trading** mode (toggle at the
   top — make sure it says Paper).
3. Find **API Keys** → Generate Key. Paper keys start with **PK**.
4. Copy the key ID and secret into your Notes app.
   (If a key ever starts with **AK**, that's a live key — the bot will
   refuse it, and so should you. Regenerate in Paper mode.)

### Step 3 — Copy this folder to the machine

Put the `trading-bot` folder at `~/projects/trading-bot` on the iMac
(same as the Prism bot — unzip the package there).

### Step 4 — Fill in .env (terminal)

```bash
cd ~/projects/trading-bot
cp .env.example .env
mkdir -p data
nano .env
```

- `BOT_TOKEN=` → your BotFather token
- `ADMIN_USER_ID=` → your @userinfobot number
- `ADMIN_CHAT_ID=` → same number (alerts to your DM)
- `ALPACA_API_KEY=` / `ALPACA_API_SECRET=` → your **PK** paper keys
- Leave `ALPACA_PAPER_ENABLED=false` for the first run (mock mode).
  Flip it to `true` only when you want real paper-API orders.
- Save: Ctrl+O, Enter, Ctrl+X.

### Step 5 — Start it

```bash
docker compose up -d --build
docker compose logs -f
```

You should see `PaperClient ready: base=https://paper-api.alpaca.markets`
and `Trading Bot (PAPER ONLY) starting`. Ctrl+C stops watching logs; the
bot keeps running.

### Step 6 — Talk to it

Open your bot in Telegram, tap START, then:

- `/watchlist` — see / change the watchlist (`add` / `del`)
- `/backtest AAPL` — run the strategy backtest
- `/paper_status` — paper cash, positions, recent decisions

The bot evaluates the watchlist every `POLL_MINUTES` (default 60) and
alerts you only when it places a paper order.

## Bot HQ (web interface)

This bot can also report to the Bot HQ web dashboard. The bridge is
optional and fail-silent — if Bot HQ is down or unconfigured, the bot
keeps running exactly as before.

### Env vars

```bash
# Where alerts go: telegram (default) | web | both
TRANSPORT=telegram
# Bot HQ dashboard base URL and this bot's key (sent as X-Bot-Key).
# Leave both empty to disable the bridge entirely.
BOT_HQ_URL=
BOT_HQ_KEY=
```

- `TRANSPORT=telegram` — alerts go to Telegram only (default).
- `TRANSPORT=web` — alerts are mirrored to the dashboard as alert events.
- `TRANSPORT=both` — alerts go to both Telegram and the dashboard.

TRANSPORT only decides where alerts flow. Heartbeats, history events,
and dashboard chat work whenever `BOT_HQ_URL` + `BOT_HQ_KEY` are set,
regardless of TRANSPORT — history is transport-independent.

### What flows where

- **Heartbeat** — after every evaluation cycle:
  `heartbeat("online", "cycle: <summary>")` with capabilities `["chat"]`.
- **Alerts** — paper-order alerts are mirrored to the dashboard as
  `kind="alert"` events when TRANSPORT includes web (`web` or `both`),
  via `bridge.notify("Trading alert", text)`. Telegram behaviour and
  `send_alert`'s return value are unchanged.
- **History events** (`POST /api/events`):
  - `cycle` — "Evaluation cycle ran" after each cycle, with the summary.
  - `paper_order` — `PAPER <action> <symbol>` whenever a paper order is placed.
  - `backtest` — `Backtest <symbol>` whenever a backtest runs (Telegram
    command or dashboard chat), with the result summary line.

### Dashboard chat commands

When the bridge is enabled, the dashboard chat is polled — every 15s in
the full Telegram service, and once per cycle in `--headless` mode — and
answered with `bridge.reply(...)`:

- `/watchlist` — watchlist symbols with the last action per symbol
- `/paper_status` — paper account cash/equity, positions, recent decisions
- `/backtest SYM` — actually runs the SMA backtest (180 days) and returns the headline metrics
- `/help` — list these commands (any other text also returns help)

Chat commands are the same read-only/reporting commands as Telegram —
chat can never enable live trading (nothing can; paper-only is
hard-locked in code in `bot/config.py` and `bot/paper.py`).

## No-Docker option (cron)

```bash
cd ~/projects/trading-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m bot --once   # one cycle, prints a summary
```

Add `--once` to an hourly cron for the lightest possible footprint.

## Offline test

```bash
python3 tests/smoke_test.py
```

Runs the backtester on bundled fixture data and verifies the paper-only
guard rejects live URLs/keys. No network, no orders.

## Files

| File | What |
|---|---|
| `bot/config.py` | .env loading + the paper-only guard |
| `bot/market_data.py` | yfinance / Alpha Vantage / fixtures + cache |
| `bot/strategies.py` | strategy interface + SMA crossover |
| `bot/backtest.py` | offline backtester |
| `bot/paper.py` | Alpaca paper client (mock default) |
| `bot/database.py` | SQLite watchlist / decisions / orders / backtests |
| `bot/runner.py` | one evaluation cycle |
| `bot/telegram_bot.py` | /watchlist, /backtest, /paper_status |
| `data/` | SQLite DB, cache, mock account (survives restarts) |

## Updating

Edit `.env` or the watchlist (via Telegram), then
`docker compose restart`. Code updates: `docker compose up -d --build`.
Your data in `./data/` is never touched by rebuilds.

To stop everything: `docker compose down`.
