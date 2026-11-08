"""Telegram admin interface: /watchlist, /backtest, /paper_status, /help.

Admin-only (ADMIN_USER_ID), same silent-ignore pattern as the Prism bot.
python-telegram-bot is imported here only — the data/backtest/paper core
runs fine without it.
"""
from __future__ import annotations

import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from . import market_data
from .backtest import run_backtest
from .strategies import build_strategy

log = logging.getLogger(__name__)


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        config = context.application.bot_data["config"]
        user = update.effective_user
        if user is None or user.id != config.admin_user_id:
            return  # silently ignore non-admins
        return await func(update, context, *args, **kwargs)

    return wrapper


@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .chat import HELP_TEXT

    await update.message.reply_text(
        HELP_TEXT
        + "\n\nTelegram extras:\n"
        "/watchlist add SYMBOL — add a symbol\n"
        "/watchlist del SYMBOL — remove a symbol"
    )


@admin_only
async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio

    from .chat import handle_chat_text

    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    paper = context.application.bot_data["paper"]
    bridge = context.application.bot_data.get("bridge")
    text = "/analyze " + (context.args[0] if context.args else "")
    answer = await asyncio.to_thread(
        handle_chat_text, config, db, paper, bridge, text
    )
    await update.message.reply_text(answer)


@admin_only
async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio

    from .chat import handle_chat_text

    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    paper = context.application.bot_data["paper"]
    bridge = context.application.bot_data.get("bridge")
    answer = await asyncio.to_thread(
        handle_chat_text, config, db, paper, bridge, "/briefing"
    )
    await update.message.reply_text(answer)


@admin_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-English messages get the same conversational GT as Bot HQ
    chat (bot/chat.py) — the Moon-Dev way: talk to the bot, don't just
    command it. Admin-only like everything else here."""
    import asyncio

    from .chat import handle_chat_text

    if update.message is None or not update.message.text:
        return
    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    paper = context.application.bot_data["paper"]
    bridge = context.application.bot_data.get("bridge")
    answer = await asyncio.to_thread(
        handle_chat_text, config, db, paper, bridge, update.message.text
    )
    await update.message.reply_text(answer)


@admin_only
async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = context.application.bot_data["db"]
    args = context.args or []
    if len(args) >= 2 and args[0].lower() == "add":
        added = db.add_to_watchlist(args[1])
        await update.message.reply_text(
            f"{args[1].upper()} {'added' if added else 'already on the list'}."
        )
    elif len(args) >= 2 and args[0].lower() == "del":
        removed = db.remove_from_watchlist(args[1])
        await update.message.reply_text(
            f"{args[1].upper()} {'removed' if removed else 'was not on the list'}."
        )
    symbols = db.list_watchlist()
    await update.message.reply_text(
        "Watchlist: " + (", ".join(symbols) if symbols else "(empty)")
    )


@admin_only
async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    if not context.args:
        await update.message.reply_text("Usage: /backtest SYMBOL")
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"Running backtest for {symbol}…")
    bars = market_data.get_bars(symbol, 180, config)
    result = run_backtest(symbol, bars, build_strategy(config))
    db.log_backtest(result, params=f"fast={config.sma_fast},slow={config.sma_slow}")
    # Bot HQ history event — guard with .get so headless/--once never break.
    try:
        bridge = None
        try:
            bridge = context.application.bot_data.get("bridge")
        except Exception:
            bridge = None
        if bridge is None:
            make_bridge = getattr(config, "make_bridge", None)
            if callable(make_bridge):
                bridge = make_bridge()
        if bridge is not None:
            bridge.log_event("backtest", f"Backtest {symbol}", result.summary())
    except Exception:  # noqa: BLE001 - fail-silent
        pass
    lines = [result.summary()]
    for t in result.trades:
        lines.append(
            f"  {t.entry_date} buy @ {t.entry_price:.2f} -> "
            f"{t.exit_date} sell @ {t.exit_price:.2f}  PnL ${t.pnl:+,.2f}"
        )
    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_paper_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    paper = context.application.bot_data["paper"]
    prices = {}
    for symbol in db.list_watchlist():
        bars = market_data.get_bars(symbol, 5, config)
        if bars:
            prices[symbol] = bars[-1].close
    account = paper.get_account(last_prices=prices)
    lines = [
        f"PAPER account [{account.mode}] — base {paper.base_url}",
        f"Cash ${account.cash:,.2f} | Equity ${account.equity:,.2f} | "
        f"Orders logged: {db.order_count()}",
    ]
    if account.positions:
        lines.append("Positions:")
        for sym, pos in sorted(account.positions.items()):
            lines.append(
                f"  {sym}: {pos['qty']:.4f} sh @ avg ${pos['avg_price']:.2f} "
                f"(now ${pos['market_price']:.2f})"
            )
    else:
        lines.append("Positions: none")
    decisions = db.recent_decisions(5)
    if decisions:
        lines.append("Recent decisions:")
        for d in decisions:
            lines.append(f"  {d['ts'][:16]} {d['symbol']} {d['action']} @ {d['price']}")
    await update.message.reply_text("\n".join(lines))
