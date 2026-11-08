"""Alpaca PAPER-trading client. PAPER ONLY — structurally, not by policy.

Safety is enforced in code, in three layers:
1. The constructor runs ``assert_paper_url`` — any base URL whose host is
   not exactly ``paper-api.alpaca.markets`` raises and the bot will not
   start. There is no code path in this package that can reach the live
   Alpaca host; the live hostname appears only in the refusal message.
2. ``assert_paper_key`` refuses Alpaca live keys (they start with 'AK').
3. Default mode is MOCK: orders are simulated against a local JSON account
   and NO network call is made. Real paper-API calls happen only when keys
   are present AND ``ALPACA_PAPER_ENABLED=true``.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field

from .config import PAPER_BASE_URL, assert_paper_key, assert_paper_url

log = logging.getLogger(__name__)

MOCK_START_CASH = 100_000.0


@dataclass
class PaperAccount:
    cash: float
    equity: float
    positions: dict[str, dict] = field(default_factory=dict)  # sym -> {qty, avg_price, market_price}
    mode: str = "mock"  # "mock" | "paper-api"


class PaperClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = PAPER_BASE_URL,
        enabled: bool = False,
        mock_state_path: str = "",
    ) -> None:
        # Guard FIRST — a bad URL or a live key stops construction cold.
        self.base_url = assert_paper_url(base_url)
        assert_paper_key(api_key)
        self.api_key = api_key
        self.api_secret = api_secret
        self.mock_state_path = mock_state_path
        self.live_calls = bool(enabled and api_key and api_secret)
        log.info(
            "PaperClient ready: base=%s mode=%s",
            self.base_url,
            "paper-api" if self.live_calls else "mock",
        )

    # ------------------------------------------------------------ mock state
    def _mock_load(self) -> dict:
        if self.mock_state_path and os.path.exists(self.mock_state_path):
            with open(self.mock_state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return {"cash": MOCK_START_CASH, "positions": {}}

    def _mock_save(self, state: dict) -> None:
        if not self.mock_state_path:
            return
        os.makedirs(os.path.dirname(self.mock_state_path), exist_ok=True)
        with open(self.mock_state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    # ------------------------------------------------------------ HTTP (paper only)
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"  # base_url is guard-verified paper host
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("APCA-API-KEY-ID", self.api_key)
        req.add_header("APCA-API-SECRET-KEY", self.api_secret)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}

    # ------------------------------------------------------------ public API
    def get_account(self, last_prices: dict[str, float] | None = None) -> PaperAccount:
        if self.live_calls:
            acct = self._request("GET", "/v2/account")
            positions = {}
            for pos in self._request("GET", "/v2/positions") or []:
                positions[pos["symbol"]] = {
                    "qty": float(pos["qty"]),
                    "avg_price": float(pos["avg_entry_price"]),
                    "market_price": float(pos["current_price"]),
                }
            return PaperAccount(
                cash=float(acct["cash"]),
                equity=float(acct["equity"]),
                positions=positions,
                mode="paper-api",
            )
        state = self._mock_load()
        prices = last_prices or {}
        positions = {}
        equity = state["cash"]
        for sym, pos in state["positions"].items():
            market = prices.get(sym, pos["avg_price"])
            positions[sym] = {
                "qty": pos["qty"],
                "avg_price": pos["avg_price"],
                "market_price": market,
            }
            equity += pos["qty"] * market
        return PaperAccount(
            cash=state["cash"], equity=equity, positions=positions, mode="mock"
        )

    def submit_order(
        self, symbol: str, side: str, qty: float, latest_price: float
    ) -> dict:
        """Place a PAPER (or mock) market order. Returns a status dict."""
        symbol = symbol.upper()
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be buy|sell, got {side!r}")
        if qty <= 0:
            raise ValueError("qty must be positive")

        if self.live_calls:
            order = self._request(
                "POST",
                "/v2/orders",
                {
                    "symbol": symbol,
                    "qty": qty,
                    "side": side,
                    "type": "market",
                    "time_in_force": "day",
                },
            )
            log.info("PAPER order submitted: %s %s %s -> %s", side, qty, symbol, order.get("id"))
            return {
                "status": order.get("status", "submitted"),
                "broker_id": order.get("id", ""),
                "mode": "paper-api",
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": latest_price,
            }

        # ---- mock execution against the local simulated account
        state = self._mock_load()
        positions = state["positions"]
        if side == "buy":
            cost = qty * latest_price
            if cost > state["cash"]:
                return {
                    "status": "rejected_insufficient_cash",
                    "broker_id": "",
                    "mode": "mock",
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": latest_price,
                }
            state["cash"] -= cost
            pos = positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
            total_qty = pos["qty"] + qty
            pos["avg_price"] = (
                pos["avg_price"] * pos["qty"] + cost
            ) / total_qty
            pos["qty"] = total_qty
            positions[symbol] = pos
        else:  # sell
            pos = positions.get(symbol)
            if not pos or pos["qty"] < qty:
                return {
                    "status": "rejected_no_position",
                    "broker_id": "",
                    "mode": "mock",
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": latest_price,
                }
            state["cash"] += qty * latest_price
            pos["qty"] -= qty
            if pos["qty"] <= 1e-9:
                positions.pop(symbol, None)
            else:
                positions[symbol] = pos
        self._mock_save(state)
        log.info("MOCK paper order filled: %s %s %s @ %.2f", side, qty, symbol, latest_price)
        return {
            "status": "filled",
            "broker_id": "mock",
            "mode": "mock",
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": latest_price,
        }
