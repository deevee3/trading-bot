"""Per-symbol analytics + analyst commentary.

Moon-Dev-style bots don't dump raw signal rows — a named agent reads the
chart and talks: what the trend is, where price sits against its lines,
what the last signal was, and what would change its mind. This module is
the pure-math half of that voice: given bars, it computes everything the
conversation layer (bot/chat.py) and the cycle briefing (bot/runner.py)
need, with zero network and zero side effects, so it's fully testable
offline on fixture data.

Nothing here places or recommends real trades. The strategy is the same
SMA-crossover the backtester and runner use; commentary only explains
what that strategy already decided.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .market_data import Bar
from .strategies import _sma, build_strategy


@dataclass
class Analysis:
    symbol: str
    bars_count: int
    last_date: str = ""
    close: float = 0.0
    day_change_pct: float | None = None
    change_5d_pct: float | None = None
    change_20d_pct: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None
    spread_pct: float | None = None  # (fast - slow) / slow * 100
    trend: str = "warming up"  # uptrend | downtrend | warming up
    action: str = "hold"  # latest strategy signal: buy | sell | hold
    reason: str = ""
    last_signal_action: str = ""  # most recent non-hold signal, "" if none
    last_signal_date: str = ""
    last_signal_reason: str = ""
    stale_days: int = 0  # age of the latest bar vs today
    confidence: int | None = None  # rule-based confluence score, 30-95

    @property
    def verdict_word(self) -> str:
        """Moon Dev vocabulary: BUY, SELL, or NOTHING (his agents' hold)."""
        return {"buy": "BUY", "sell": "SELL"}.get(self.action, "NOTHING")

    @property
    def has_lines(self) -> bool:
        return self.sma_fast is not None and self.sma_slow is not None


def _pct(now: float, before: float) -> float | None:
    if not before:
        return None
    return (now / before - 1.0) * 100.0


def analyze(symbol: str, bars: list[Bar], config) -> Analysis:
    """Full read on one symbol from its bars and the configured strategy."""
    symbol = symbol.upper()
    out = Analysis(symbol=symbol, bars_count=len(bars))
    if not bars:
        return out
    strategy = build_strategy(config)
    signals = strategy.evaluate(bars)
    closes = [b.close for b in bars]
    last = bars[-1]
    out.last_date = last.date
    out.close = last.close
    if len(bars) >= 2:
        out.day_change_pct = _pct(last.close, bars[-2].close)
    if len(bars) >= 6:
        out.change_5d_pct = _pct(last.close, bars[-6].close)
    if len(bars) >= 21:
        out.change_20d_pct = _pct(last.close, bars[-21].close)
    idx = len(bars) - 1
    out.sma_fast = _sma(closes, config.sma_fast, idx)
    out.sma_slow = _sma(closes, config.sma_slow, idx)
    if out.has_lines:
        out.spread_pct = _pct(out.sma_fast, out.sma_slow)
        out.trend = "uptrend" if out.sma_fast >= out.sma_slow else "downtrend"
    latest = signals[-1]
    out.action = latest.action
    out.reason = latest.reason
    for sig in reversed(signals):
        if sig.action in ("buy", "sell"):
            out.last_signal_action = sig.action
            out.last_signal_date = sig.date
            out.last_signal_reason = sig.reason
            break
    try:
        y, m, d = (int(p) for p in last.date.split("-"))
        out.stale_days = max(0, (date.today() - date(y, m, d)).days)
    except Exception:  # noqa: BLE001 - unparseable date is not fatal
        out.stale_days = 0
    out.confidence = _confluence_confidence(out)
    return out


def _confluence_confidence(a: "Analysis") -> int | None:
    """Deterministic stand-in for the LLM 'Confidence: X%' line Moon
    Dev's agents end every read with. No model, no vibes: a published
    rule over the same numbers the read itself quotes.

      base 50
      + up to 20 for line separation (|spread %| — a wide gap between
        the SMAs is a clearer trend than lines hugging each other)
      + 10 when 5-session and ~month momentum agree with the trend
      + 10 when today's move agrees with the trend
      + 5 when the last real signal fired on the trend's side
    Clamped 30-95; None until both lines exist.
    """
    if not a.has_lines or a.spread_pct is None:
        return None
    up = a.trend == "uptrend"
    score = 50.0
    score += min(20.0, abs(a.spread_pct) * 4.0)

    def _agrees(pct: float | None) -> bool:
        return pct is not None and ((pct >= 0) == up)

    if _agrees(a.change_5d_pct) and _agrees(a.change_20d_pct):
        score += 10
    if _agrees(a.day_change_pct):
        score += 10
    if a.last_signal_action and ((a.last_signal_action == "buy") == up):
        score += 5
    return int(max(30, min(95, round(score))))


def verdict_block(a: Analysis, fast: int, slow: int) -> str:
    """The Moon Dev 3-line verdict that opens every chart read:
    line 1 the call in caps, line 2 one short reason, line 3 confidence."""
    if not a.has_lines:
        reason = "Not enough bars to draw both lines yet — no read is an honest read."
        conf = "Confidence: — (warming up)"
    elif a.action in ("buy", "sell"):
        reason = a.reason or f"Fresh {a.action} cross on the SMA{fast}/SMA{slow}."
        conf = f"Confidence: {a.confidence}%"
    elif a.trend == "uptrend":
        reason = (
            f"Uptrend intact (SMA{fast} above SMA{slow}) but no fresh cross — "
            "Moon rule: no cross, no chase."
        )
        conf = f"Confidence: {a.confidence}%"
    else:
        reason = (
            f"Downtrend (SMA{fast} under SMA{slow}) and no cross back up — "
            "hands off."
        )
        conf = f"Confidence: {a.confidence}%"
    if a.has_lines:
        conf += " (confluence score — how clear the trend is, not a win rate)"
    return f"{a.verdict_word}\n{reason}\n{conf}"


def trend_sentence(a: Analysis, fast: int, slow: int) -> str:
    """One plain-English sentence on where price sits vs its lines."""
    if not a.has_lines:
        return (
            f"I only have {a.bars_count} bars for {a.symbol} — not enough "
            f"for the SMA{slow} line yet, so no trend read. Warming up."
        )
    if a.trend == "uptrend":
        return (
            f"Trend read: up. The fast line (SMA{fast} ${a.sma_fast:,.2f}) is "
            f"riding {abs(a.spread_pct):.2f}% above the slow line "
            f"(SMA{slow} ${a.sma_slow:,.2f})."
        )
    return (
        f"Trend read: down. The fast line (SMA{fast} ${a.sma_fast:,.2f}) is "
        f"{abs(a.spread_pct):.2f}% under the slow line "
        f"(SMA{slow} ${a.sma_slow:,.2f})."
    )


def flip_condition(a: Analysis, fast: int, slow: int) -> str:
    """What would change the bot's mind — the Moon-Dev 'what I'm watching'."""
    if not a.has_lines:
        return "What flips me: enough bars to draw both lines, then a cross."
    if a.action in ("buy", "sell"):
        return (
            f"That signal just fired — what flips me now is the opposite "
            f"cross: SMA{fast} closing {'below' if a.action == 'buy' else 'above'} "
            f"SMA{slow}."
        )
    if a.trend == "uptrend":
        return (
            f"What flips me: I only act on a fresh cross. We're already "
            f"trending up, so no new buy fires until SMA{fast} dips below "
            f"SMA{slow} and crosses back over — patience, no chasing."
        )
    return (
        f"What flips me: SMA{fast} crossing back above SMA{slow} — that's "
        f"my buy trigger. Until then I'm hands-off."
    )


def commentary_line(a: Analysis, fast: int, slow: int) -> str:
    """One analyst paragraph on a symbol — used in cycle briefings."""
    if not a.has_lines and a.bars_count == 0:
        return f"{a.symbol}: no data came back at all — skipping it this round."
    day = (
        f"{a.day_change_pct:+.2f}% on the day" if a.day_change_pct is not None
        else "no prior bar to compare"
    )
    head = f"{a.symbol} ${a.close:,.2f} (bar {a.last_date}, {day}) — {a.action.upper()}."
    bits = [head, trend_sentence(a, fast, slow)]
    if a.last_signal_action:
        bits.append(
            f"Last real signal: {a.last_signal_action} on {a.last_signal_date}."
        )
    if a.action == "hold":
        bits.append(flip_condition(a, fast, slow))
    elif a.reason:
        bits.append(f"Why: {a.reason}.")
    if a.stale_days > 10:
        bits.append(
            f"⚠️ Heads up: my newest bar is {a.stale_days} days old — this "
            "is the offline/mock feed talking, not the live tape."
        )
    return " ".join(bits)


def briefing(
    analyses: list[Analysis],
    mode: str,
    fast: int,
    slow: int,
    acted: list[str] | None = None,
) -> tuple[str, str]:
    """An hourly desk briefing: (headline, body) in the analyst voice.

    ``acted`` = symbols an order was actually placed for this cycle (the
    runner passes it). Without it, fresh signals are reported as signals,
    never as trades.
    """
    orders = [a for a in analyses if a.action in ("buy", "sell")]
    if acted:
        headline = f"Desk briefing — paper orders placed: {', '.join(acted)}"
    elif orders:
        acts = ", ".join(f"{a.action.upper()} {a.symbol}" for a in orders)
        headline = f"Desk briefing — fresh signal on the board: {acts} (paper)"
    elif analyses and all(a.trend == "uptrend" for a in analyses if a.has_lines) and any(
        a.has_lines for a in analyses
    ):
        headline = "Desk briefing — all quiet: trends up, no fresh crosses, no trades"
    else:
        headline = "Desk briefing — all quiet: no fresh signals, hands off"
    lines = [commentary_line(a, fast, slow) for a in analyses]
    footer = (
        f"Mode: {mode}. Paper only — nothing real was traded. "
        "Ask me about any symbol and I'll walk the chart."
    )
    body = "\n".join(lines + [footer]) if lines else "No symbols on the watchlist."
    return headline, body
