"""Trading Bot (PAPER ONLY) — entry point.

Run modes:
  python -m bot --once    one evaluation cycle, then exit (cron-friendly;
                          standard library only — no Telegram dependency)
  python -m bot           full service: Telegram admin bot + scheduled
                          cycles via the job queue (needs BOT_TOKEN)
  python -m bot --headless  scheduled cycles with no Telegram interface

Every mode passes through the paper-only guard in config.load_config()
before anything else happens.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from .config import load_config
from .database import Database
from .paper import PaperClient
from .runner import run_cycle

log = logging.getLogger(__name__)


def build_runtime():
    config = load_config()
    db = Database(config.db_path)
    db.seed_watchlist(config.watchlist_seed)
    paper = PaperClient(
        api_key=config.alpaca_api_key,
        api_secret=config.alpaca_api_secret,
        base_url=config.alpaca_base_url,
        enabled=config.alpaca_paper_enabled,
        mock_state_path=config.mock_state_path,
    )
    return config, db, paper


def main() -> int:
    parser = argparse.ArgumentParser(prog="trading-bot")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument(
        "--headless", action="store_true", help="loop cycles without Telegram"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        config, db, paper = build_runtime()
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if args.once:
        for line in run_cycle(config, db, paper):
            print(line)
        return 0

    if args.headless or not config.bot_token:
        if not config.bot_token:
            log.info("No BOT_TOKEN set — running headless (alerts log-only)")
        log.info(
            "Trading Bot (PAPER ONLY) looping every %d min", config.poll_minutes
        )
        # Bot HQ chat: check inbox once per cycle (after run_cycle).
        try:
            _headless_bridge = config.make_bridge()
        except Exception:  # noqa: BLE001
            _headless_bridge = None
        while True:
            run_cycle(config, db, paper)
            if _headless_bridge is not None:
                try:
                    from .chat import process_inbox

                    process_inbox(config, db, paper, _headless_bridge)
                except Exception:  # noqa: BLE001 - fail-silent
                    log.warning("Bot HQ chat poll failed", exc_info=True)
            time.sleep(config.poll_minutes * 60)

    # Full service: Telegram admin commands + scheduled cycles.
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    from . import telegram_bot

    app = ApplicationBuilder().token(config.bot_token).build()
    app.bot_data["config"] = config
    app.bot_data["db"] = db
    app.bot_data["paper"] = paper
    try:
        app.bot_data["bridge"] = config.make_bridge()
    except Exception:  # noqa: BLE001
        app.bot_data["bridge"] = None
    app.add_handler(CommandHandler("help", telegram_bot.cmd_help))
    app.add_handler(CommandHandler("start", telegram_bot.cmd_help))
    app.add_handler(CommandHandler("watchlist", telegram_bot.cmd_watchlist))
    app.add_handler(CommandHandler("backtest", telegram_bot.cmd_backtest))
    app.add_handler(CommandHandler("paper_status", telegram_bot.cmd_paper_status))
    app.add_handler(CommandHandler("analyze", telegram_bot.cmd_analyze))
    app.add_handler(CommandHandler("briefing", telegram_bot.cmd_briefing))
    # Plain English, not just commands — same GT brain as Bot HQ chat.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_bot.on_text)
    )

    async def _cycle_job(context) -> None:
        lines = run_cycle(config, db, paper)
        if lines:
            app.bot_data["last_cycle"] = "cycle: " + " | ".join(lines)[:500]

    app.job_queue.run_repeating(
        _cycle_job, interval=config.poll_minutes * 60, first=10
    )

    async def _heartbeat_job(context) -> None:
        # Presence: Bot HQ marks a bot idle when its heartbeat ages out, and
        # run_cycle only heartbeats once per (hourly) cycle — so without this
        # job the bot showed "idle" 59 of every 60 minutes. Beat every
        # minute, carrying the last cycle summary as the detail.
        # (Added 2026-10-22, mirroring Community Manager's heartbeat loop.)
        bridge = app.bot_data.get("bridge")
        if bridge is None:
            return
        detail = app.bot_data.get("last_cycle") or "Trading Bot running"
        try:
            await asyncio.to_thread(bridge.heartbeat, "online", detail)
        except Exception:  # noqa: BLE001 - fail-silent
            log.warning("Bot HQ heartbeat job failed", exc_info=True)

    app.job_queue.run_repeating(_heartbeat_job, interval=60, first=5)

    async def _chat_job(context) -> None:
        bridge = app.bot_data.get("bridge")
        if bridge is None:
            return
        try:
            from .chat import process_inbox

            await asyncio.to_thread(process_inbox, config, db, paper, bridge)
        except Exception:  # noqa: BLE001 - fail-silent
            log.warning("Bot HQ chat job failed", exc_info=True)

    app.job_queue.run_repeating(_chat_job, interval=15, first=5)
    log.info("Trading Bot (PAPER ONLY) starting (long polling)")
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
