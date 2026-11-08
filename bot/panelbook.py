"""The panel's own paper book + the graduation threshold.

D'Vaughn's directive (2026-10-22): relax the strategy-only principle —
let the Moon-style LLM panel paper-trade on its consensus, and let GT
*request real funds* when, and only when, a hard evidence threshold
says the panel has earned it.

Design, kept honest:

  * SEPARATE BOOK. The panel trades a virtual $10,000 account in its
    own tables, never touching the strategy book. Two track records,
    comparable side by side. Entries are fee-adjusted (0.1%/side) so
    the record isn't flattered by frictionless math.
  * GUARDRAILS ON EVERY ENTRY. Fresh data only (latest bar <= 3 days
    old), consensus must be BUY with average confidence >= the floor
    (default 70), the backtested strategy must not be voting SELL, one
    position per symbol, fixed notional — and the whole thing fails
    CLOSED: model server down = no panel trades that cycle, ever.
  * EXITS. Consensus SELL, or the strategy itself firing SELL.
  * GRADUATION IS A REQUEST, NOT A SWITCH. When every criterion below
    holds at once, GT files a funding request into the Bot HQ approval
    queue with the full evidence pack, capped at a $500 first tranche.
    D'Vaughn approves or denies it there. Even an approval does not
    connect any real-money rail by itself — it authorizes a human to
    set one up deliberately. "Won't lose money" in D'Vaughn's usage
    (2026-10-22) means positive expected value — the point where it
    would be ADVANTAGEOUS to have GT trade real money, not a promise
    of zero losing trades. These criteria are that definition made
    measurable: a real sample, a real win rate, positive net P&L after
    fees, contained drawdown, calibrated confidence, over real time.

Criteria (all must hold simultaneously):
  >= 20 closed panel trades
  >= 30 days since the first panel trade
  win rate >= 55%
  net P&L > 0 (after fee adjustment)
  realized max drawdown <= 10% of the starting book
  calibration: mean entry confidence of winners >= losers
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import market_data
from .analyze import analyze
from .panel import build_context_pack, consensus, run_panel, strategy_vote

log = logging.getLogger(__name__)

BOOK_START_CASH = 10_000.0
FEE_RATE = 0.001  # 0.1% per side, applied to entry and exit prices
MAX_DATA_AGE_DAYS = 3
REQUESTED_TRANCHE_USD = 500

GRAD_MIN_TRADES = 20
GRAD_MIN_DAYS = 30
GRAD_MIN_WIN_RATE = 55.0
GRAD_MAX_DRAWDOWN_PCT = 10.0


def _cash(db) -> float:
    raw = db.panel_meta_get("cash")
    if raw is None:
        db.panel_meta_set("cash", str(BOOK_START_CASH))
        return BOOK_START_CASH
    try:
        return float(raw)
    except ValueError:
        return BOOK_START_CASH


def _set_cash(db, value: float) -> None:
    db.panel_meta_set("cash", f"{value:.2f}")


def book_stats(db, prices: dict[str, float] | None = None) -> dict:
    """The panel book's scoreboard numbers."""
    trades = db.panel_closed_trades()
    positions = db.panel_positions_all()
    cash = _cash(db)
    unrealized = 0.0
    for pos in positions:
        px = (prices or {}).get(pos["symbol"], pos["avg_price"])
        unrealized += (px - pos["avg_price"]) * pos["qty"]
    net_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    # Realized equity curve drawdown (start + cumulative closed P&L).
    equity = BOOK_START_CASH
    peak = equity
    max_dd = 0.0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    days = 0
    if trades:
        first = datetime.fromisoformat(trades[0]["opened_ts"])
        days = (datetime.now(timezone.utc) - first).days
    conf_w = (
        sum(t["entry_confidence"] or 0 for t in wins) / len(wins) if wins else None
    )
    conf_l = (
        sum(t["entry_confidence"] or 0 for t in losses) / len(losses)
        if losses
        else None
    )
    return {
        "cash": cash,
        "equity": cash + sum(
            p["qty"] * (prices or {}).get(p["symbol"], p["avg_price"])
            for p in positions
        ),
        "open_positions": len(positions),
        "closed_trades": len(trades),
        "wins": len(wins),
        "win_rate": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "net_pnl": net_pnl,
        "unrealized": unrealized,
        "max_drawdown_pct": max_dd,
        "days_active": days,
        "conf_winners": conf_w,
        "conf_losers": conf_l,
    }


def graduation_checklist(stats: dict) -> list[tuple[str, bool, str]]:
    """(criterion, met?, current value) — the scoreboard GT shows."""
    cal_ok = (
        stats["conf_winners"] is not None
        and stats["conf_losers"] is not None
        and stats["conf_winners"] >= stats["conf_losers"]
    )
    return [
        (
            f"Closed panel trades >= {GRAD_MIN_TRADES}",
            stats["closed_trades"] >= GRAD_MIN_TRADES,
            f"{stats['closed_trades']} closed",
        ),
        (
            f"Track record >= {GRAD_MIN_DAYS} days",
            stats["days_active"] >= GRAD_MIN_DAYS,
            f"{stats['days_active']} days",
        ),
        (
            f"Win rate >= {GRAD_MIN_WIN_RATE:.0f}%",
            stats["win_rate"] >= GRAD_MIN_WIN_RATE and stats["closed_trades"] > 0,
            f"{stats['win_rate']:.0f}% win rate",
        ),
        (
            "Net P&L positive after fees",
            stats["net_pnl"] > 0,
            f"${stats['net_pnl']:+,.2f} net",
        ),
        (
            f"Max drawdown <= {GRAD_MAX_DRAWDOWN_PCT:.0f}%",
            stats["max_drawdown_pct"] <= GRAD_MAX_DRAWDOWN_PCT,
            f"{stats['max_drawdown_pct']:.1f}% worst drawdown",
        ),
        (
            "Confidence calibrated (winners entered more confident than losers)",
            cal_ok,
            (
                f"winners {stats['conf_winners']:.0f} vs losers {stats['conf_losers']:.0f}"
                if stats["conf_winners"] is not None
                and stats["conf_losers"] is not None
                else "needs both winning and losing trades to judge"
            ),
        ),
    ]


def graduation_ready(stats: dict) -> bool:
    return all(met for _, met, _ in graduation_checklist(stats))


def evaluate_panel_cycle(config, db, bridge) -> list[str]:
    """One panel-trading pass over the watchlist. Fail-closed and
    fail-silent: returns human lines for what it did (usually [])."""
    if not (getattr(config, "panel_trading", False) and config.llm_enabled):
        return []
    lines: list[str] = []
    min_conf = getattr(config, "panel_min_confidence", 70)
    for symbol in db.list_watchlist():
        try:
            bars = market_data.get_bars(symbol, 180, config)
            if not bars:
                continue
            a = analyze(symbol, bars, config)
            if a.stale_days > MAX_DATA_AGE_DAYS:
                continue  # never trade on old data
            pack = build_context_pack(
                a, fast=config.sma_fast, slow=config.sma_slow,
                recent_closes=", ".join(f"{b.close:,.2f}" for b in bars[-10:]),
            )
            llm_votes = run_panel(config, a, pack)
            if not llm_votes:
                continue  # model server down/unparseable = no trades
            strat = strategy_vote(a)
            cons = consensus(llm_votes + [strat])
            if cons is None:
                continue
            votes_txt = json.dumps(
                [
                    {"agent": v.agent, "word": v.word, "conf": v.confidence}
                    for v in llm_votes + [strat]
                ]
            )
            pos = db.panel_position(symbol)
            price = a.close
            if pos is None and cons.word == "BUY":
                if (cons.confidence or 0) < min_conf:
                    continue
                if strat.word == "SELL":
                    continue  # the backtested rule vetoes panel buys
                notional = min(config.trade_notional_usd, _cash(db))
                if notional < 50:
                    continue
                qty = notional * (1 - FEE_RATE) / price
                db.panel_open_position(
                    symbol, qty, price * (1 + FEE_RATE), cons.confidence, votes_txt
                )
                _set_cash(db, _cash(db) - notional)
                msg = (
                    f"🧠 PANEL PAPER BUY {symbol}: {qty:.4f} sh @ ~${price:,.2f} "
                    f"(consensus BUY, confidence {cons.confidence}%, "
                    f"fee-adjusted entry ${price * (1 + FEE_RATE):,.2f})"
                )
                lines.append(msg)
                _event(bridge, "panel_trade", f"Panel paper BUY {symbol}", msg)
                db.log_decision(symbol, "buy", price, "panel consensus", "panel")
            elif pos is not None and (cons.word == "SELL" or strat.word == "SELL"):
                exit_price = price * (1 - FEE_RATE)
                trade = db.panel_close_position(
                    symbol,
                    exit_price,
                    "panel consensus SELL" if cons.word == "SELL" else "strategy SELL",
                )
                if trade:
                    proceeds = trade["qty"] * exit_price
                    _set_cash(db, _cash(db) + proceeds)
                    msg = (
                        f"🧠 PANEL PAPER SELL {symbol}: @ ~${price:,.2f} — "
                        f"P&L ${trade['pnl']:+,.2f} "
                        f"(entered at {trade['entry_confidence']}% confidence)"
                    )
                    lines.append(msg)
                    _event(bridge, "panel_trade", f"Panel paper SELL {symbol}", msg)
                    db.log_decision(symbol, "sell", price, "panel exit", "panel")
        except Exception as exc:  # noqa: BLE001 - one symbol must not kill the pass
            log.warning("panel cycle failed for %s: %s", symbol, exc)
    # Graduation check after any trading pass.
    try:
        maybe_file_graduation(config, db, bridge)
    except Exception as exc:  # noqa: BLE001
        log.warning("graduation check failed: %s", exc)
    return lines


def _event(bridge, kind, title, detail) -> None:
    if bridge is None:
        return
    try:
        bridge.log_event(kind, title, detail)
    except Exception:  # noqa: BLE001 - fail-silent
        pass


def maybe_file_graduation(config, db, bridge) -> bool:
    """File the funding request into Bot HQ approvals when (and only
    when) every criterion holds and no request is already open/denied
    within the cooldown. Returns True when a request was filed."""
    if db.panel_meta_get("graduation_approval_id"):
        # A request exists — check for a decision to announce.
        _poll_graduation_decision(db, bridge)
        return False
    filed_ts_raw = db.panel_meta_get("graduation_denied_ts")
    if filed_ts_raw:
        try:
            denied = datetime.fromisoformat(filed_ts_raw)
            if (datetime.now(timezone.utc) - denied).days < 30:
                return False  # 30-day cooldown after a denial
        except ValueError:
            pass
    stats = book_stats(db)
    if not graduation_ready(stats):
        return False
    checklist = graduation_checklist(stats)
    evidence = "\n".join(
        f"✓ {name} — {value}" for name, _, value in checklist
    )
    body = (
        f"GT (Trading Bot) requests a real-funds graduation tranche of "
        f"${REQUESTED_TRANCHE_USD}.\n\n"
        f"Basis: the Moon-style agent panel's OWN paper book, kept "
        f"separate from the strategy book, fee-adjusted at 0.1%/side:\n"
        f"{evidence}\n"
        f"Book equity ${stats['equity']:,.2f} on $10,000 virtual start | "
        f"{stats['closed_trades']} closed trades | "
        f"net P&L ${stats['net_pnl']:+,.2f}.\n\n"
        "What graduation means, in the agreed definition: on this "
        "record the panel's expected value is positive — it would be "
        "advantageous, not risk-free, to trade real funds. Losing "
        "trades will still happen; the criteria show a positive, "
        "calibrated paper edge over a real sample. Approving this request "
        "does NOT connect any real-money rail; it authorizes a human to "
        "set one up deliberately, capped at the tranche above, with "
        "the same guardrails (fresh data, confidence floor, strategy "
        "veto) carried over."
    )
    approval_id = None
    if bridge is not None:
        try:
            approval_id = bridge.request_approval(
                "funding_request",
                f"GT requests real-funds graduation — ${REQUESTED_TRANCHE_USD} starter tranche",
                body,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("graduation filing failed: %s", exc)
    db.panel_meta_set("graduation_filed_ts", datetime.now(timezone.utc).isoformat())
    if approval_id:
        db.panel_meta_set("graduation_approval_id", str(approval_id))
    _event(
        bridge,
        "graduation",
        "🎓 GT filed a real-funds graduation request",
        body,
    )
    log.info("Graduation request filed (approval_id=%s)", approval_id)
    return True


def _poll_graduation_decision(db, bridge) -> None:
    if bridge is None:
        return
    raw = db.panel_meta_get("graduation_approval_id")
    if not raw:
        return
    try:
        decision = bridge.get_decision(int(raw))
    except Exception:  # noqa: BLE001
        return
    if not decision:
        return
    status = (decision.get("status") or "").lower()
    if status in ("approved", "approve"):
        db.panel_meta_set("graduation_state", "approved")
        db.panel_meta_set("graduation_approval_id", "")
        _event(
            bridge, "graduation",
            "🎓 Graduation APPROVED by D'Vaughn",
            "The $500 tranche request was approved. Next step is a human "
            "deliberately setting up the real rail — GT changes nothing "
            "on its own.",
        )
    elif status in ("denied", "deny", "rejected", "skipped", "skip"):
        db.panel_meta_set("graduation_state", "denied")
        db.panel_meta_set("graduation_denied_ts", datetime.now(timezone.utc).isoformat())
        db.panel_meta_set("graduation_approval_id", "")
        _event(
            bridge, "graduation",
            "Graduation request denied — 30-day cooldown",
            "GT keeps paper-trading and can re-file after 30 days if the "
            "criteria still hold.",
        )


def panel_status_text(config, db) -> str:
    """The /panel scoreboard: book state + graduation checklist."""
    prices = {}
    for pos in db.panel_positions_all():
        try:
            bars = market_data.get_bars(pos["symbol"], 5, config)
            if bars:
                prices[pos["symbol"]] = bars[-1].close
        except Exception:  # noqa: BLE001
            pass
    stats = book_stats(db, prices)
    lines = [
        "🧠 GT's panel book (the Moon-style agents' own paper account):",
        f"Equity ${stats['equity']:,.2f} on $10,000 virtual | "
        f"Cash ${stats['cash']:,.2f} | Open positions: {stats['open_positions']} | "
        f"Closed trades: {stats['closed_trades']} | "
        f"Win rate {stats['win_rate']:.0f}% | Net P&L ${stats['net_pnl']:+,.2f}",
    ]
    for pos in db.panel_positions_all():
        px = prices.get(pos["symbol"], pos["avg_price"])
        lines.append(
            f"  {pos['symbol']}: {pos['qty']:.4f} sh @ avg ${pos['avg_price']:.2f} "
            f"(now ${px:.2f}, entered at {pos['entry_confidence']}% confidence)"
        )
    lines.append("\n🎓 Graduation scoreboard — GT may request real funds when ALL are met:")
    for name, met, value in graduation_checklist(stats):
        lines.append(f"  {'✓' if met else '✗'} {name} — {value}")
    state = db.panel_meta_get("graduation_state")
    if db.panel_meta_get("graduation_approval_id"):
        lines.append("  → Request FILED and waiting in the Bot HQ approval queue.")
    elif state == "approved":
        lines.append("  → Graduation was APPROVED. Real rail setup is a deliberate human step.")
    elif graduation_ready(stats):
        lines.append("  → All criteria met — request files on the next panel cycle.")
    else:
        lines.append("  → Not yet. The panel keeps paper-trading and building the record.")
    return "\n".join(lines)
