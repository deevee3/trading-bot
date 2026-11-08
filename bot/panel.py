"""The Moon-Dev-style agent panel for GT.

Moon Dev's design (see deevee3/glenridemoon-agents): specialized agents
— Chuck the chart analyst, a risk agent, a strategy agent — each read
the same market context through an LLM and vote BUY / SELL / NOTHING in
a strict 3-line format; a swarm mode takes consensus. This module is
that design, wired to GT:

  * The CONTEXT PACK is built from GT's real numbers only (bot/analyze.py
    output, the strategy's own signal, a fresh backtest of the exact
    production strategy, and the paper account state) — the models get
    facts, never vibes, and are told not to invent any.
  * Three lenses (chart / risk / strategy) run as three LLM calls with
    Moon's own verdict format, on the free local models (bot/llm.py).
  * The deterministic strategy itself casts the fourth vote, with its
    rule-based confluence confidence.
  * Consensus = majority; ties break to NOTHING (Moon: risk management
    first). The panel is ADVISORY — it explains and votes in chat. Paper
    orders remain gated by the backtested strategy code alone, so the
    RBI parity (what backtests is what trades) is never broken by a
    model's mood.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import llm
from .analyze import Analysis

log = logging.getLogger(__name__)

FORMAT_RULES = """You must respond in exactly 3 lines:
Line 1: Only write BUY, SELL, or NOTHING
Line 2: One short reason why, grounded only in the numbers given
Line 3: Only write "Confidence: X%" where X is 0-100
Never invent prices, news, or data that is not in the context."""

LENSES: dict[str, str] = {
    "chart": (
        "You are Chuck, the chart-analysis agent on a trading desk "
        "(Moon Dev design). You read price vs its moving averages, trend "
        "and momentum confluence. " + FORMAT_RULES
    ),
    "risk": (
        "You are the risk agent on a trading desk (Moon Dev design). "
        "Risk management outranks returns: you vote NOTHING unless the "
        "setup is clean, the trend is unambiguous, and the backtest "
        "drawdown is acceptable. " + FORMAT_RULES
    ),
    "strategy": (
        "You are the strategy agent on a trading desk (Moon Dev design). "
        "You judge only whether the rules of the SMA-crossover system "
        "were followed and what the system itself is saying — discipline "
        "over opinion. " + FORMAT_RULES
    ),
}


@dataclass
class Vote:
    agent: str
    word: str  # BUY | SELL | NOTHING
    reason: str = ""
    confidence: int | None = None
    source: str = "llm"  # llm | strategy


def build_context_pack(
    a: Analysis,
    *,
    fast: int,
    slow: int,
    backtest_summary: str = "",
    account_line: str = "",
    recent_closes: str = "",
) -> str:
    """Everything an agent needs, and nothing it doesn't."""
    lines = [
        f"Symbol: {a.symbol} (daily bars, latest {a.last_date})",
        f"Close: ${a.close:,.2f}"
        + (
            f" ({a.day_change_pct:+.2f}% on the day)"
            if a.day_change_pct is not None
            else ""
        ),
        f"SMA{fast}: "
        + (f"${a.sma_fast:,.2f}" if a.sma_fast is not None else "n/a")
        + f" | SMA{slow}: "
        + (f"${a.sma_slow:,.2f}" if a.sma_slow is not None else "n/a")
        + (
            f" | spread {a.spread_pct:+.2f}% | trend: {a.trend}"
            if a.spread_pct is not None
            else ""
        ),
        "Momentum: "
        + (
            f"5 sessions {a.change_5d_pct:+.2f}%, ~month {a.change_20d_pct:+.2f}%"
            if a.change_5d_pct is not None and a.change_20d_pct is not None
            else "n/a"
        ),
        f"Production strategy (SMA{fast}/SMA{slow} crossover) signal today: "
        f"{a.action.upper()}"
        + (f" — {a.reason}" if a.reason else " (no fresh crossover)"),
        "Last non-hold strategy signal: "
        + (
            f"{a.last_signal_action} on {a.last_signal_date} ({a.last_signal_reason})"
            if a.last_signal_action
            else "none in the window"
        ),
    ]
    if backtest_summary:
        lines.append(f"Backtest of this exact strategy (180d): {backtest_summary}")
    if account_line:
        lines.append(f"Paper account: {account_line}")
    if recent_closes:
        lines.append(f"Recent closes: {recent_closes}")
    if a.stale_days > 10:
        lines.append(
            f"WARNING: latest bar is {a.stale_days} days old (offline/mock feed)."
        )
    lines.append(
        "This is a PAPER account. The strategy's signal is the only thing "
        "that can trigger a paper order; your vote is advisory analysis."
    )
    return "\n".join(lines)


def strategy_vote(a: Analysis) -> Vote:
    return Vote(
        agent="strategy-code",
        word=a.verdict_word,
        reason=a.reason or "The backtested rule's own signal — no cross, no action.",
        confidence=a.confidence,
        source="strategy",
    )


def consensus(votes: list[Vote]) -> Vote | None:
    """Majority wins; a tie breaks to NOTHING. Returns a synthetic
    consensus Vote (agent='consensus') or None with no votes."""
    if not votes:
        return None
    counts: dict[str, int] = {}
    for v in votes:
        counts[v.word] = counts.get(v.word, 0) + 1
    top = max(counts.values())
    winners = [w for w, c in counts.items() if c == top]
    word = winners[0] if len(winners) == 1 else "NOTHING"
    confs = [v.confidence for v in votes if v.word == word and v.confidence]
    conf = int(sum(confs) / len(confs)) if confs else None
    return Vote(agent="consensus", word=word, confidence=conf, source="panel")


def run_panel(config, a: Analysis, context_pack: str) -> list[Vote]:
    """Run the three LLM lenses + the strategy vote.

    SEQUENTIAL on purpose: LM Studio serves one generation lane, so
    Moon-style concurrency just queues three reasoning calls behind
    each other until timeouts (observed live 2026-10-22). One at a time,
    with a server preflight so a sleeping Mac Studio costs one 8-second
    check instead of three dead calls. Returns [] for the LLM votes
    when the LLM is disabled/unreachable — the caller still gets the
    strategy vote from strategy_vote().
    """
    if not getattr(config, "llm_enabled", False):
        return []
    if not llm.server_up(config.llm_base_url):
        log.warning("panel skipped: model server not answering")
        return []

    votes: list[Vote] = []
    theme_ctx = ""
    try:
        with open("/app/fleet-wiki/gt-theme-small-traders-week.md", encoding="utf-8") as _tf:
            theme_ctx = _tf.read().strip()
    except OSError:
        theme_ctx = ""
    if theme_ctx:
        log.info("panel theme context loaded (%d chars)", len(theme_ctx))
    weekly_ctx = ""
    try:
        with open("/app/fleet-wiki/gt-weekly-prevention-over-cure.md", encoding="utf-8") as _wf:
            weekly_ctx = _wf.read().strip()
    except OSError:
        weekly_ctx = ""
    if weekly_ctx:
        log.info("panel weekly theme context loaded (%d chars)", len(weekly_ctx))
    for name, system in LENSES.items():
        text = llm.chat_completion(
            config.llm_base_url,
            config.llm_model,
            [
                {"role": "system", "content": system + ("\n\nWeekly theme background (calendar context only; "
                 "never a trade signal, never a price target):\n" + theme_ctx[:1500] if theme_ctx else "")
                 + ("\n\nWeek 1 weekly theme — Prevention over cure (calendar context only; "
                 "never a trade signal, never a price target):\n" + weekly_ctx[:1200] if weekly_ctx else "")},
                {"role": "user", "content": context_pack},
            ],
            temperature=0.3,
            # Reasoning models spend most of the budget thinking; 1200
            # tokens returned EMPTY content on the real context pack
            # (observed live 2026-10-22). 3000 leaves room to answer.
            max_tokens=3000,
            timeout_s=config.llm_timeout_s,
            api_key=config.llm_api_key,
        )
        parsed = llm.parse_verdict(text)
        if parsed is None:
            log.warning("panel lens %s returned no parseable verdict", name)
            continue
        word, reason, conf = parsed
        votes.append(Vote(agent=name, word=word, reason=reason, confidence=conf))
    return votes


def render_panel(llm_votes: list[Vote], strat: Vote, model: str) -> str:
    """The desk-panel section of a chat read."""
    all_votes = llm_votes + [strat]
    cons = consensus(all_votes)
    lines = [
        f"🧠 Agent panel (Moon-style swarm, local model: {model}):",
    ]
    icon = {"chart": "📊", "risk": "🛡️", "strategy": "🎯", "strategy-code": "⚙️"}
    for v in all_votes:
        conf = f" — Confidence: {v.confidence}%" if v.confidence is not None else ""
        who = "strategy code (the backtested rule itself)" if v.source == "strategy" else f"{v.agent} agent"
        lines.append(f"{icon.get(v.agent, '🤖')} {who}: {v.word}{conf}")
        if v.reason:
            lines.append(f"   {v.reason}")
    if cons is not None:
        conf = f" at {cons.confidence}% average confidence" if cons.confidence else ""
        lines.append(f"🗳️ Consensus: {cons.word}{conf}.")
    lines.append(
        "How this is used: the panel paper-trades its OWN book on "
        "consensus (see /panel); the strategy book stays purely the "
        "backtested rule's. Two track records, compared honestly."
    )
    return "\n".join(lines)
