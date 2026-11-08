"""One evaluation cycle: for every watchlist symbol, fetch bars, run the
strategy, and — only when the LATEST bar's signal is a fresh buy/sell that
differs from the last logged action — place a PAPER order, log everything,
and alert. Same strategy code as the backtester.

The cycle REPORTS like a Moon-Dev desk agent, not a cron job: the Bot HQ
event is a narrated briefing (bot/analyze.py) — per-symbol reads with the
trend, the lines, and what would flip the call — under a headline that
says whether anything actually happened.
"""
from __future__ import annotations

import logging

from . import market_data
from .alerts import send_alert
from .analyze import Analysis, analyze, briefing
from .strategies import build_strategy

log = logging.getLogger(__name__)

HISTORY_DAYS = 180  # enough bars for the slow SMA + context


def run_cycle(config, db, paper_client) -> list[str]:
    strategy = build_strategy(config)
    mode = "paper-api" if paper_client.live_calls else "mock"
    summary: list[str] = []
    analyses: list[Analysis] = []
    acted: list[str] = []
    # Bot HQ bridge — no-op when disabled, fail-silent always.
    try:
        bridge = config.make_bridge()
    except Exception:  # noqa: BLE001
        bridge = None

    for symbol in db.list_watchlist():
        try:
            bars = market_data.get_bars(symbol, HISTORY_DAYS, config)
            if len(bars) < config.sma_slow + 2:
                summary.append(f"{symbol}: not enough data ({len(bars)} bars)")
                analyses.append(Analysis(symbol=symbol, bars_count=len(bars)))
                continue
            analyses.append(analyze(symbol, bars, config))
            signals = strategy.evaluate(bars)
            latest, last_bar = signals[-1], bars[-1]
            line = (
                f"{symbol}: close {last_bar.close:.2f} on {last_bar.date} "
                f"-> {latest.action}"
            )
            if latest.action == "hold":
                summary.append(line)
                continue
            if db.last_action(symbol) == latest.action:
                summary.append(line + " (repeat signal, skipped)")
                continue

            qty = round(config.trade_notional_usd / last_bar.close, 4)
            db.log_decision(symbol, latest.action, last_bar.close, latest.reason, mode)
            order = paper_client.submit_order(
                symbol, latest.action, qty, last_bar.close
            )
            db.log_order(order)
            acted.append(f"{latest.action.upper()} {symbol}")
            msg = (
                f"📈 PAPER {latest.action.upper()} {symbol}: "
                f"{qty} sh @ ~${last_bar.close:.2f} [{order['status']}, {mode}]\n"
                f"Reason: {latest.reason}"
            )
            if bridge is not None:
                try:
                    bridge.log_event(
                        "paper_order", f"PAPER {latest.action} {symbol}", msg
                    )
                except Exception:  # noqa: BLE001 - fail-silent
                    pass
            send_alert(config, msg)
            summary.append(line + f" -> ORDER {order['status']}")
        except Exception as exc:
            log.exception("Cycle failed for %s", symbol)
            summary.append(f"{symbol}: ERROR {exc}")

    # Panel book pass (D'Vaughn's 2026-10-22 directive): the LLM panel
    # paper-trades its own virtual book on consensus, and the
    # graduation evaluator runs with it. Fail-silent, fail-closed —
    # it can never break or block the strategy cycle above.
    try:
        from .panelbook import evaluate_panel_cycle

        for line in evaluate_panel_cycle(config, db, bridge):
            log.info("Panel: %s", line)
            summary.append(line)
    except Exception:  # noqa: BLE001
        log.warning("panel cycle pass failed", exc_info=True)

    log.info("Cycle complete: %s", " | ".join(summary))
    if bridge is not None:
        headline, body = briefing(
            analyses, mode, config.sma_fast, config.sma_slow, acted=acted
        )
        try:
            bridge.heartbeat("online", headline[:500])
        except Exception:  # noqa: BLE001 - fail-silent
            pass
        try:
            bridge.log_event("cycle", headline, body)
        except Exception:  # noqa: BLE001 - fail-silent
            pass
    return summary
