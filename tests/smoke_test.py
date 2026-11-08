"""Offline smoke test for the Trading Bot package. Standard library only —
no network, no Telegram, no yfinance, and NO order ever leaves this
machine (the paper client runs in mock mode throughout).

Run:  python3 tests/smoke_test.py     (from the package root)
Exit code 0 = all checks PASS.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def expect_raises(name: str, fn) -> None:
    try:
        fn()
    except RuntimeError as exc:
        check(name, True, str(exc)[:90])
        return
    check(name, False, "no RuntimeError raised")


def main() -> int:
    os.environ["DATA_MODE"] = "fixture"

    from bot import config as cfg_mod
    from bot.backtest import run_backtest
    from bot.database import Database
    from bot.market_data import fixture_bars
    from bot.paper import PaperClient
    from bot.strategies import SmaCrossover

    # ------------------------------------------------ 1. fixture data loads
    bars = fixture_bars("FIXTURE")
    check("fixture bars load", len(bars) >= 200, f"{len(bars)} bars")
    check(
        "fixture bars sane",
        all(b.low <= b.close <= b.high for b in bars[:50]),
        "low <= close <= high on first 50",
    )

    # ------------------------------------------------ 2. backtest on fixture
    strategy = SmaCrossover(fast=10, slow=30)
    result = run_backtest("FIXTURE", bars, strategy, start_cash=10_000.0)
    print("       " + result.summary())
    check("backtest produces trades", len(result.trades) >= 1, f"{len(result.trades)} trades")
    check(
        "backtest equity curve complete",
        len(result.equity_curve) == len(bars),
        f"{len(result.equity_curve)} points",
    )
    check("backtest final equity positive", result.final_equity > 0, f"${result.final_equity:,.2f}")
    check(
        "backtest metrics in range",
        0.0 <= result.win_rate_pct <= 100.0 and result.max_drawdown_pct >= 0.0,
    )

    # ------------------------------------------------ 3. PAPER-ONLY guard
    expect_raises(
        "guard rejects live Alpaca URL",
        lambda: cfg_mod.assert_paper_url("https://api.alpaca.markets"),
    )
    expect_raises(
        "guard rejects http paper URL",
        lambda: cfg_mod.assert_paper_url("http://paper-api.alpaca.markets"),
    )
    expect_raises(
        "guard rejects look-alike host",
        lambda: cfg_mod.assert_paper_url("https://paper-api.alpaca.markets.evil.com"),
    )
    expect_raises(
        "PaperClient refuses live URL at construction",
        lambda: PaperClient(base_url="https://api.alpaca.markets"),
    )
    expect_raises(
        "guard rejects live (AK) API key",
        lambda: cfg_mod.assert_paper_key("AKAAAAAAAAAAAAAAAAAAAA"),
    )
    accepted = cfg_mod.assert_paper_url("https://paper-api.alpaca.markets/")
    check("guard accepts the paper URL", accepted == cfg_mod.PAPER_BASE_URL, accepted)
    cfg_mod.assert_paper_key("PKAAAAAAAAAAAAAAAAAAAA")  # must NOT raise

    # ------------------------------------------------ 4. mock paper round-trip
    with tempfile.TemporaryDirectory() as tmp:
        client = PaperClient(mock_state_path=os.path.join(tmp, "mock.json"))
        check("client defaults to mock mode", not client.live_calls)
        acct0 = client.get_account()
        check("mock account starts at $100k", acct0.cash == 100_000.0)

        order = client.submit_order("FIXTURE", "buy", 10.0, bars[-1].close)
        check("mock buy fills", order["status"] == "filled", order["status"])
        acct1 = client.get_account(last_prices={"FIXTURE": bars[-1].close})
        check(
            "mock position appears",
            "FIXTURE" in acct1.positions and acct1.positions["FIXTURE"]["qty"] == 10.0,
        )
        order2 = client.submit_order("FIXTURE", "sell", 10.0, bars[-1].close)
        acct2 = client.get_account()
        check(
            "mock sell closes position, cash restored",
            order2["status"] == "filled" and "FIXTURE" not in acct2.positions
            and abs(acct2.cash - 100_000.0) < 0.01,
            f"cash ${acct2.cash:,.2f}",
        )
        rejected = client.submit_order("FIXTURE", "sell", 5.0, bars[-1].close)
        check("mock oversell rejected", rejected["status"] == "rejected_no_position")

    # ------------------------------------------------ 5. SQLite round-trip
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        db.seed_watchlist(["FIXTURE", "AAPL"])
        check("watchlist seeds", db.list_watchlist() == ["AAPL", "FIXTURE"])
        db.add_to_watchlist("MSFT")
        db.remove_from_watchlist("AAPL")
        check("watchlist add/remove", db.list_watchlist() == ["FIXTURE", "MSFT"])
        db.log_decision("FIXTURE", "buy", bars[-1].close, "smoke", "mock")
        check("last_action reads back", db.last_action("FIXTURE") == "buy")
        db.log_order(order)
        check("order logged", db.order_count() == 1)
        db.log_backtest(result, params="fast=10,slow=30")
        check("backtest logged + decisions readable", len(db.recent_decisions(5)) == 1)

    print()
    if FAILURES:
        print(f"SMOKE TEST: FAIL ({len(FAILURES)} failing: {', '.join(FAILURES)})")
        return 1
    print("SMOKE TEST: PASS (all checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
