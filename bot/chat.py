"""Conversational dashboard chat for Bot HQ.

Moon-Dev-style bots are *talked to*, not just commanded: a named agent
with a read on the market that answers plain English, explains its own
decisions, and narrates what it's watching. That's this module's job.

The bot's name is GT. ``handle_chat_text`` understands:

  * slash commands (unchanged, still exact): /watchlist, /paper_status,
    /backtest SYM, /help — plus /analyze SYM and /briefing
  * plain English: greetings, "how's AAPL looking?", "why are you
    holding SPY?", "what are you watching?", "explain your strategy",
    "backtest MSFT", "how's the account?", and bare symbol questions

Every answer is grounded in the same strategy/backtest/paper code the
runner uses — GT never invents numbers, never gives financial advice,
and never stops saying the quiet part out loud: PAPER ONLY. Chat can
never enable live trading (nothing can; the bot is hard-locked in code).

Used by both run loops via ``process_inbox`` (HQ dashboard) and shared
with the Telegram interface (bot/telegram_bot.py) so GT sounds like the
same person on both surfaces.
"""
from __future__ import annotations

import logging
import re

from . import market_data
from .analyze import analyze, briefing, commentary_line, trend_sentence, verdict_block
from .backtest import run_backtest
from .strategies import build_strategy

log = logging.getLogger(__name__)

BOT_NAME = "GT"

HELP_TEXT = (
    f"Hey — {BOT_NAME} here, your Glenride trading desk bot. PAPER ONLY. "
    "This bot can never place a real order.\n\n"
    "Talk to me in plain English (\"how's AAPL looking?\", \"why are you "
    "holding?\", \"what are you watching?\") or use commands:\n"
    "/watchlist — show the watchlist\n"
    "/paper_status — paper account, positions, recent decisions\n"
    "/analyze SYMBOL — my full chart read: trend, lines, last signal, "
    "what would flip me\n"
    "/backtest SYMBOL — run the SMA strategy backtest (180 days)\n"
    "/briefing — the desk briefing on the whole watchlist, right now\n"
    "/panel — the agent panel's own paper book + graduation scoreboard\n"
    "/help — show this help"
)

GREETING = (
    f"Hey! {BOT_NAME} on the desk. 📈 Paper account's open, real money is "
    "never on the table — I trade a strategy, not feelings.\n\n"
    "Ask me things like:\n"
    "• \"How's AAPL looking?\" — I'll walk the chart\n"
    "• \"Why are you holding?\" — I'll show my work\n"
    "• \"What's the account at?\" — paper status, positions, decisions\n"
    "• \"Backtest SPY\" — 180 days of my strategy vs the tape\n"
    "Or /help for the command list."
)

STRATEGY_TEXT = (
    "My whole game is one rule, run without emotion — very Moon Dev of me:\n"
    "I watch a fast and a slow simple moving average on daily closes. "
    "When the fast line crosses ABOVE the slow line, momentum just flipped "
    "up and I buy (paper). When it crosses back BELOW, I sell. No cross, "
    "no trade — I sit on my hands, sometimes for weeks.\n"
    "Research → backtest → implement: the exact code deciding live paper "
    "trades is the code the backtester runs, so what I quote you is what "
    "I'd actually do. Long-only, one position per name, fixed notional "
    "per trade. Boring on purpose."
)

_SYMBOL_RE = re.compile(r"\b([A-Z]{1,5})\b")
_STOPWORDS = {
    "I", "A", "THE", "YOU", "ARE", "HOW", "WHY", "WHAT", "WHEN", "IS", "IT",
    "ME", "MY", "ON", "IN", "TO", "DO", "GO", "NO", "OK", "HI", "HEY", "VS",
    "AND", "FOR", "YOUR", "THIS", "THAT", "WITH", "FROM", "BEEN", "MUCH",
}


def _find_symbol(text: str, watchlist: list[str]) -> str:
    """Best symbol guess in free text.

    A watchlist name matches however it's typed ("aapl" counts). Any
    OTHER ticker must be typed like a ticker — ALL CAPS or $-prefixed
    ("AAPL", "$SPY") — because normal sentences are full of short words
    that merely *look* like tickers once uppercased ("hello" -> "HELLO"
    was the bug this rule exists to prevent).
    """
    upper = (text or "").upper()
    for sym in watchlist:
        if re.search(rf"\b{re.escape(sym)}\b", upper):
            return sym
    for tok in _SYMBOL_RE.findall(text or ""):
        if tok not in _STOPWORDS and len(tok) >= 2:
            return tok
    m = re.search(r"\$([A-Za-z]{1,5})\b", text or "")
    if m and m.group(1).upper() not in _STOPWORDS:
        return m.group(1).upper()
    return ""


def _watchlist_text(db) -> str:
    symbols = db.list_watchlist()
    if not symbols:
        return "My watchlist is empty right now — nothing on the desk."
    lines = [f"On my desk ({len(symbols)} names):"]
    for symbol in symbols:
        try:
            last = db.last_action(symbol)
        except Exception:
            last = None
        lines.append(f"  {symbol} — last action: {last if last else 'none'}")
    lines.append("Ask me about any of them and I'll walk the chart.")
    return "\n".join(lines)


def _paper_status_text(config, db, paper) -> str:
    prices = {}
    for symbol in db.list_watchlist():
        try:
            bars = market_data.get_bars(symbol, 5, config)
        except Exception:
            bars = []
        if bars:
            prices[symbol] = bars[-1].close
    account = paper.get_account(last_prices=prices)
    lines = [
        f"Here's where the paper book stands [{account.mode}] — {paper.base_url}:",
        f"PAPER account [{account.mode}] — base {paper.base_url}",
        f"Cash ${account.cash:,.2f} | Equity ${account.equity:,.2f} | "
        f"Orders logged: {db.order_count()}",
    ]
    if account.positions:
        lines.append("Positions I'm holding:")
        for sym, pos in sorted(account.positions.items()):
            lines.append(
                f"  {sym}: {pos['qty']:.4f} sh @ avg ${pos['avg_price']:.2f} "
                f"(now ${pos['market_price']:.2f})"
            )
    else:
        lines.append("Positions: none — flat and patient.")
    decisions = db.recent_decisions(5)
    if decisions:
        lines.append("Recent decisions:")
        for d in decisions:
            lines.append(f"  {d['ts'][:16]} {d['symbol']} {d['action']} @ {d['price']}")
    else:
        lines.append("No decisions logged yet — no cross has fired since I booted.")
    return "\n".join(lines)


def _backtest_text(config, db, bridge, symbol: str) -> str:
    bars = market_data.get_bars(symbol, 180, config)
    result = run_backtest(symbol, bars, build_strategy(config))
    try:
        db.log_backtest(result, params=f"fast={config.sma_fast},slow={config.sma_slow}")
    except Exception as exc:  # noqa: BLE001
        log.warning("log_backtest failed: %s", exc)
    summary = result.summary()
    # History event — transport-independent, no-op when bridge disabled.
    try:
        if bridge is not None:
            bridge.log_event("backtest", f"Backtest {symbol}", summary)
    except Exception as exc:  # noqa: BLE001 - fail-silent
        log.warning("Bot HQ backtest event failed: %s", exc)
    verdict = (
        "That strategy earned its keep on this window."
        if result.total_return_pct > 0
        else "Rough window — the strategy gave money back here. "
        "That's data, not a verdict on the system."
    )
    lines = [
        f"Ran my SMA{config.sma_fast}/SMA{config.sma_slow} crossover over "
        f"180 days of {symbol}. {verdict}",
        summary,
    ]
    for t in result.trades:
        lines.append(
            f"  {t.entry_date} buy @ {t.entry_price:.2f} -> "
            f"{t.exit_date} sell @ {t.exit_price:.2f}  PnL ${t.pnl:+,.2f}"
        )
    lines.append("Same code that runs my paper book — no cherry-picking.")
    return "\n".join(lines)


def _analyze_text(config, symbol: str, db=None, paper=None) -> str:
    bars = market_data.get_bars(symbol, 180, config)
    a = analyze(symbol, bars, config)
    if a.bars_count == 0:
        return (
            f"I pulled {symbol} and got nothing back — no bars from any "
            "source. Can't read a chart that isn't there."
        )
    fast, slow = config.sma_fast, config.sma_slow
    lines = [
        f"📊 {a.symbol} — my read:",
        verdict_block(a, fast, slow),
        commentary_line(a, fast, slow),
    ]
    moves = []
    if a.change_5d_pct is not None:
        moves.append(f"5 sessions: {a.change_5d_pct:+.2f}%")
    if a.change_20d_pct is not None:
        moves.append(f"~month: {a.change_20d_pct:+.2f}%")
    if moves:
        lines.append("Momentum: " + " | ".join(moves) + ".")
    if a.last_signal_action:
        lines.append(
            f"Last time I actually moved: {a.last_signal_action} on "
            f"{a.last_signal_date} ({a.last_signal_reason})."
        )
    lines.append(
        "That's the strategy talking, not a tip — paper desk, research only."
    )
    # Moon-style agent panel — advisory LLM lenses over the same numbers.
    if getattr(config, "llm_enabled", False):
        panel_text = _panel_text(config, a, bars, db, paper)
        if panel_text:
            lines.append("")
            lines.append(panel_text)
        else:
            lines.append(
                "(Agent panel offline right now — the model server didn't "
                "answer. My deterministic read above stands.)"
            )
    return "\n".join(lines)


def _panel_text(config, a, bars, db, paper) -> str:
    """Run the LLM panel for one symbol; '' when no lens answered."""
    try:
        from .panel import build_context_pack, render_panel, run_panel, strategy_vote

        backtest_summary = ""
        try:
            result = run_backtest(a.symbol, bars, build_strategy(config))
            backtest_summary = result.summary()
        except Exception:  # noqa: BLE001
            backtest_summary = ""
        account_line = ""
        if paper is not None:
            try:
                acct = paper.get_account(last_prices={a.symbol: a.close})
                pos = acct.positions.get(a.symbol)
                account_line = (
                    f"cash ${acct.cash:,.2f}, equity ${acct.equity:,.2f}, "
                    + (
                        f"holding {pos['qty']:.4f} sh of {a.symbol} @ avg "
                        f"${pos['avg_price']:.2f}"
                        if pos
                        else f"no {a.symbol} position"
                    )
                )
            except Exception:  # noqa: BLE001
                account_line = ""
        recent = ", ".join(f"{b.close:,.2f}" for b in bars[-10:])
        pack = build_context_pack(
            a,
            fast=config.sma_fast,
            slow=config.sma_slow,
            backtest_summary=backtest_summary,
            account_line=account_line,
            recent_closes=recent,
        )
        votes = run_panel(config, a, pack)
        if not votes:
            return ""
        return render_panel(votes, strategy_vote(a), config.llm_model)
    except Exception as exc:  # noqa: BLE001 - panel must never break a read
        log.warning("panel failed: %s", exc)
        return ""


def _briefing_text(config, db, paper) -> str:
    mode = "paper-api" if getattr(paper, "live_calls", False) else "mock"
    analyses = []
    for symbol in db.list_watchlist():
        try:
            bars = market_data.get_bars(symbol, 180, config)
            analyses.append(analyze(symbol, bars, config))
        except Exception as exc:  # noqa: BLE001
            log.warning("briefing analyze failed for %s: %s", symbol, exc)
    headline, body = briefing(analyses, mode, config.sma_fast, config.sma_slow)
    return f"{headline}\n{body}"


def _why_text(config, db, symbol: str) -> str:
    """'Why are you holding / did you buy X?' — show the work."""
    symbols = [symbol] if symbol else db.list_watchlist()
    if not symbols:
        return "Nothing on the watchlist to explain — the desk is empty."
    parts = []
    for sym in symbols:
        a = analyze(sym, market_data.get_bars(sym, 180, config), config)
        if a.action in ("buy", "sell"):
            parts.append(
                f"{sym}: that wasn't a hold — my rule fired {a.action} "
                f"({a.reason})."
            )
        elif a.has_lines:
            parts.append(
                f"{sym}: holding because no fresh cross fired. "
                + trend_sentence(a, config.sma_fast, config.sma_slow)
                + " A hold from me isn't 'I like it' — it's 'my one rule "
                "hasn't triggered', and I don't improvise."
            )
        else:
            parts.append(
                f"{sym}: holding because I don't even have both moving "
                "averages yet — never trade on a half-drawn chart."
            )
    return "\n".join(parts)


def handle_chat_text(config, db, paper, bridge, text) -> str:
    """Route one chat message to a truthful, conversational answer.

    Slash commands stay exact; everything else goes through plain-English
    intent matching. Read-only/reporting only — chat can never place,
    queue or approve an order.
    """
    raw = (text or "").strip()
    if not raw:
        return GREETING
    parts = raw.split()
    cmd = parts[0].lower()
    watchlist = []
    try:
        watchlist = db.list_watchlist()
    except Exception:  # noqa: BLE001
        watchlist = []

    # ---- exact slash commands -------------------------------------------
    if cmd == "/watchlist":
        return _watchlist_text(db)
    if cmd == "/paper_status" or cmd == "/status":
        return _paper_status_text(config, db, paper)
    if cmd == "/analyze":
        if len(parts) < 2:
            return "Usage: /analyze SYMBOL — or just ask \"how's AAPL looking?\""
        return _analyze_text(config, parts[1].upper(), db, paper)
    if cmd == "/briefing":
        return _briefing_text(config, db, paper)
    if cmd == "/panel":
        from .panelbook import panel_status_text

        return panel_status_text(config, db)
    if cmd == "/backtest":
        if len(parts) < 2:
            return "Usage: /backtest SYMBOL"
        return _backtest_text(config, db, bridge, parts[1].upper())
    if cmd == "/help" or cmd == "/commands":
        return HELP_TEXT
    if cmd == "/start":
        return GREETING

    # ---- plain English ----------------------------------------------------
    low = raw.lower()
    symbol = _find_symbol(raw, watchlist)

    if any(w in low for w in ("backtest", "back test", "back-test")):
        if symbol:
            return _backtest_text(config, db, bridge, symbol)
        return "Backtest which one? Give me a symbol — e.g. \"backtest SPY\"."
    if low.startswith(("who are you", "your name", "who is this")) or "who are you" in low:
        return (
            f"I'm {BOT_NAME} — Glenride's trading desk bot, named after the "
            "GT on my handle. One strategy, paper money, zero ego: I run an "
            "SMA crossover on the watchlist, narrate every decision, and "
            "never touch a real order. Moon Dev energy, compliance-department "
            "risk profile."
        )
    if any(p in low for p in ("how are you", "how's it going", "hows it going", "you good", "status")):
        return _paper_status_text(config, db, paper)
    if any(p in low for p in ("briefing", "brief me", "roundup", "market update", "what's happening", "whats happening", "what happened")):
        return _briefing_text(config, db, paper)
    if "why" in low and any(
        w in low for w in ("hold", "holding", "buy", "bought", "sell", "sold", "trade", "trading", "nothing", "sitting")
    ):
        return _why_text(config, db, symbol)
    if any(p in low for p in ("strategy", "how do you trade", "how do you decide", "your system", "your rules", "rbi")):
        return STRATEGY_TEXT
    if any(p in low for p in ("watchlist", "watching", "watch list", "on your desk", "covering")):
        return _watchlist_text(db)
    if any(p in low for p in ("account", "portfolio", "positions", "balance", "equity", "cash", "pnl", "p&l", "profit")):
        return _paper_status_text(config, db, paper)
    if symbol and any(
        p in low
        for p in (
            "look", "chart", "analyze", "analyse", "think", "opinion",
            "read", "about", "doing", "price", "?", "should",
        )
    ):
        return _analyze_text(config, symbol, db, paper)
    if symbol and len(parts) <= 3:
        return _analyze_text(config, symbol, db, paper)
    # Panel intent sits AFTER the symbol reads on purpose: "full read on
    # AAPL with the panel" is a chart read (which includes the panel),
    # while "show me your panel scoreboard" has no symbol and lands here.
    if any(p in low for p in ("panel", "graduation", "graduate", "real money", "real funds", "scoreboard", "your own book", "panel book")):
        from .panelbook import panel_status_text

        return panel_status_text(config, db)
    first_word = re.split(r"[^a-z']+", low, maxsplit=1)[0]
    if first_word in ("hello", "hey", "hi", "yo", "morning", "gm", "sup") or low.startswith(
        ("what's up", "whats up", "good morning", "good afternoon")
    ):
        return GREETING
    if "thank" in low:
        return (
            "Anytime. That's the job — watch the lines, show the work, "
            "no FOMO. Ping me when you want the next read. 📈"
        )
    if "help" in low or "command" in low or "can you do" in low:
        return HELP_TEXT

    # ---- honest fallback ----------------------------------------------------
    return (
        "I want to make sure I answer the right thing — I'm a one-strategy "
        "desk bot, so my range is: chart reads (\"how's MSFT looking?\"), "
        "explaining myself (\"why are you holding?\"), the paper account "
        "(\"status\"), the watchlist, backtests (\"backtest AAPL\"), or a "
        "full /briefing. Try me one of those ways."
    )


def process_inbox(config, db, paper, bridge) -> int:
    """Fetch dashboard inbox, handle each message, reply via bridge.

    Returns the number of messages handled. No-op (0) when the bridge
    is disabled. Fail-silent per message: one bad message does not
    block the rest.
    """
    if bridge is None:
        return 0
    try:
        if not bridge.enabled:
            return 0
    except Exception:
        return 0
    try:
        messages = bridge.fetch_inbox()
    except Exception as exc:  # noqa: BLE001 - fail-silent
        log.warning("Bot HQ fetch_inbox failed: %s", exc)
        return 0
    handled = 0
    for msg in messages or []:
        try:
            body = msg.get("body", "") if isinstance(msg, dict) else str(msg)
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            answer = handle_chat_text(config, db, paper, bridge, body)
            bridge.reply(answer, in_reply_to=msg_id)
            handled += 1
        except Exception as exc:  # noqa: BLE001 - fail-silent per message
            log.warning("Bot HQ chat handling failed: %s", exc)
    return handled
