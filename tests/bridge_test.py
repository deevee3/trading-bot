"""Offline bridge test for the Trading Bot package. Standard library only —
localhost stub server only, no external network, no real orders (mock /
fixture mode throughout, same as the smoke test).

Run:  python3 tests/bridge_test.py     (from the package root)
Exit code 0 = all checks PASS.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

FAILURES: list[str] = []
RECORDS: list[dict] = []
RECORDS_LOCK = threading.Lock()


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def _record(method, path, key, body):
    with RECORDS_LOCK:
        RECORDS.append(
            {"method": method, "path": path, "key": key, "body": body}
        )


def _find(method, path_prefix):
    with RECORDS_LOCK:
        return [
            r
            for r in RECORDS
            if r["method"] == method and r["path"].startswith(path_prefix)
        ]


def _clear():
    with RECORDS_LOCK:
        RECORDS.clear()


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence
        pass

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = None
        _record(method, self.path, self.headers.get("X-Bot-Key"), body)

        # Route (strip query string for matching)
        route = self.path.split("?")[0]
        if method == "POST" and route == "/api/heartbeat":
            payload = {}
        elif method == "POST" and route == "/api/events":
            payload = {}
        elif method == "GET" and route == "/api/messages/inbox":
            payload = [{"id": 7, "body": "/watchlist", "created_at": "2026-01-01T00:00:00Z"}]
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif method == "POST" and route == "/api/messages":
            payload = {}
        elif method == "POST" and route == "/api/approvals":
            payload = {"id": 1}
        elif method == "GET" and route == "/api/approvals/1":
            payload = {"status": "approved"}
        else:
            payload = {}

        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    os.environ["DATA_MODE"] = "fixture"

    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        from bot.bridge import Bridge

        bridge = Bridge(
            url=base_url,
            key="test-key-123",
            transport="both",
            capabilities=["chat"],
        )
        check("bridge enabled when url+key set", bridge.enabled is True)
        check("bridge use_web for both", bridge.use_web is True)
        check("bridge use_telegram for both", bridge.use_telegram is True)

        # ------------------------------------------------ heartbeat
        _clear()
        ok = bridge.heartbeat("online", "cycle: test")
        check("heartbeat returns True", ok is True)
        recs = _find("POST", "/api/heartbeat")
        check("heartbeat posted once", len(recs) == 1, f"{len(recs)} calls")
        if recs:
            b = recs[0]["body"] or {}
            check("heartbeat X-Bot-Key header", recs[0]["key"] == "test-key-123")
            check(
                "heartbeat shape bot_id/name",
                b.get("bot_id") == "trading-bot" and b.get("name") == "Trading Bot",
                str(b),
            )
            check(
                "heartbeat shape status/detail",
                b.get("status") == "online" and b.get("detail") == "cycle: test",
            )
            check(
                "heartbeat capabilities [chat]",
                b.get("capabilities") == ["chat"],
                str(b.get("capabilities")),
            )

        # ------------------------------------------------ log_event
        _clear()
        ok = bridge.log_event("cycle", "Evaluation cycle ran", "AAPL: hold")
        check("log_event returns True", ok is True)
        recs = _find("POST", "/api/events")
        check("log_event posted to /api/events", len(recs) == 1)
        if recs:
            b = recs[0]["body"] or {}
            check(
                "log_event shape",
                b.get("bot_id") == "trading-bot"
                and b.get("kind") == "cycle"
                and b.get("title") == "Evaluation cycle ran"
                and b.get("detail") == "AAPL: hold",
                str(b),
            )

        # ------------------------------------------------ notify -> alert event
        _clear()
        ok = bridge.notify("Trading alert", "PAPER BUY AAPL")
        check("notify returns True", ok is True)
        recs = _find("POST", "/api/events")
        check("notify posts to /api/events", len(recs) == 1)
        if recs:
            b = recs[0]["body"] or {}
            check(
                "notify posts kind=alert",
                b.get("kind") == "alert"
                and b.get("title") == "Trading alert"
                and b.get("detail") == "PAPER BUY AAPL",
                str(b),
            )

        # ------------------------------------------------ fetch_inbox
        _clear()
        inbox = bridge.fetch_inbox()
        check("fetch_inbox returns list", isinstance(inbox, list))
        check("fetch_inbox parses one message", len(inbox) == 1, str(inbox))
        if inbox:
            check(
                "fetch_inbox parsing id/body/created_at",
                inbox[0].get("id") == 7
                and inbox[0].get("body") == "/watchlist"
                and inbox[0].get("created_at") == "2026-01-01T00:00:00Z",
                str(inbox[0]),
            )
        recs = _find("GET", "/api/messages/inbox")
        check("fetch_inbox hit inbox endpoint", len(recs) == 1)

        # ------------------------------------------------ reply
        _clear()
        ok = bridge.reply("Watchlist: AAPL", in_reply_to=7)
        check("reply returns True", ok is True)
        recs = _find("POST", "/api/messages")
        check("reply posted to /api/messages", len(recs) == 1)
        if recs:
            b = recs[0]["body"] or {}
            check(
                "reply shape with in_reply_to",
                b.get("bot_id") == "trading-bot"
                and b.get("body") == "Watchlist: AAPL"
                and b.get("in_reply_to") == 7,
                str(b),
            )
            check("reply X-Bot-Key header", recs[0]["key"] == "test-key-123")

        # ------------------------------------------------ approvals stub (unused by this bot, harmless)
        _clear()
        approval_id = bridge.request_approval("test", "t", "b")
        check("request_approval returns id 1", approval_id == 1, str(approval_id))
        decision = bridge.get_decision(1)
        check(
            "get_decision parses approved",
            decision is not None and decision.get("status") == "approved",
            str(decision),
        )

        # ------------------------------------------------ disabled bridge = zero calls
        _clear()
        disabled = Bridge(url="", key="", transport="both", capabilities=["chat"])
        check("disabled bridge not enabled", disabled.enabled is False)
        check("disabled use_web False", disabled.use_web is False)
        check("disabled heartbeat False", disabled.heartbeat("online", "x") is False)
        check("disabled log_event False", disabled.log_event("k", "t", "d") is False)
        check("disabled notify False", disabled.notify("t", "d") is False)
        check("disabled fetch_inbox []", disabled.fetch_inbox() == [])
        check("disabled reply False", disabled.reply("hi") is False)
        check("disabled request_approval None", disabled.request_approval("k", "t", "b") is None)
        check("disabled get_decision None", disabled.get_decision(1) is None)
        with RECORDS_LOCK:
            n = len(RECORDS)
        check("disabled bridge made zero calls", n == 0, f"{n} calls")

        # ------------------------------------------------ closed port fails silent
        closed = _free_closed_port()
        dead = Bridge(
            url=f"http://127.0.0.1:{closed}",
            key="test-key-123",
            transport="both",
            timeout=1.0,
            capabilities=["chat"],
        )
        try:
            hb = dead.heartbeat("online", "x")
            ev = dead.log_event("k", "t", "d")
            nt = dead.notify("t", "d")
            inbox2 = dead.fetch_inbox()
            rp = dead.reply("hi")
            ap = dead.request_approval("k", "t", "b")
            check("closed-port heartbeat fails silent (False)", hb is False)
            check("closed-port log_event fails silent (False)", ev is False)
            check("closed-port notify fails silent (False)", nt is False)
            check("closed-port fetch_inbox fails silent ([])", inbox2 == [])
            check("closed-port reply fails silent (False)", rp is False)
            check("closed-port request_approval fails silent (None)", ap is None)
        except Exception as exc:
            check("closed-port fails silent (no exception)", False, str(exc))

        # ------------------------------------------------ handle_chat_text
        from bot.chat import handle_chat_text
        from bot.config import AppConfig
        from bot.database import Database
        from bot.paper import PaperClient

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                data_mode="fixture",
                db_path=os.path.join(tmp, "test.db"),
                cache_dir=os.path.join(tmp, "cache"),
                mock_state_path=os.path.join(tmp, "mock.json"),
                bot_hq_url=base_url,
                bot_hq_key="test-key-123",
                transport="both",
            )
            # make_bridge wiring from config
            mb = cfg.make_bridge()
            check("config.make_bridge enabled", mb.enabled is True)
            check(
                "config.make_bridge capabilities [chat]",
                mb.capabilities == ["chat"],
                str(mb.capabilities),
            )
            check("config.make_bridge transport both", mb.transport == "both")

            db = Database(os.path.join(tmp, "test.db"))
            db.seed_watchlist(["FIXTURE", "AAPL"])
            db.log_decision("FIXTURE", "buy", 100.0, "smoke", "mock")
            paper = PaperClient(mock_state_path=os.path.join(tmp, "mock.json"))
            check("chat paper client is mock", not paper.live_calls)

            answer_wl = handle_chat_text(cfg, db, paper, disabled, "/watchlist")
            check(
                "handle_chat_text /watchlist answers",
                "FIXTURE" in answer_wl and "AAPL" in answer_wl,
                answer_wl.replace("\n", " | ")[:120],
            )
            check(
                "handle_chat_text /watchlist includes last action",
                "buy" in answer_wl,
                answer_wl.replace("\n", " | ")[:120],
            )

            answer_help = handle_chat_text(cfg, db, paper, disabled, "/help")
            check(
                "handle_chat_text /help lists commands",
                "/watchlist" in answer_help
                and "/paper_status" in answer_help
                and "/backtest" in answer_help
                and "/help" in answer_help,
                answer_help.replace("\n", " | ")[:160],
            )
            check(
                "handle_chat_text /help is PAPER ONLY",
                "PAPER ONLY" in answer_help,
            )
            # Must not claim any live-trading ability: no affirmative live claim.
            low = answer_help.lower()
            check(
                "handle_chat_text /help does not claim live-trading ability",
                "paper only" in low
                and "can never place a real order" in low
                and "live trading enabled" not in low
                and "place live" not in low
                and "real-money trading" not in low,
                answer_help.replace("\n", " | ")[:160],
            )

            answer_other = handle_chat_text(cfg, db, paper, disabled, "hello there")
            check(
                "handle_chat_text unknown text returns help",
                answer_other == answer_help,
            )

            answer_ps = handle_chat_text(cfg, db, paper, disabled, "/paper_status")
            check(
                "handle_chat_text /paper_status reads paper account",
                "PAPER account" in answer_ps and "Cash" in answer_ps,
                answer_ps.replace("\n", " | ")[:160],
            )

            answer_bt = handle_chat_text(cfg, db, paper, disabled, "/backtest FIXTURE")
            check(
                "handle_chat_text /backtest runs backtest",
                "FIXTURE" in answer_bt and "return" in answer_bt,
                answer_bt.replace("\n", " | ")[:160],
            )

    finally:
        server.shutdown()
        server.server_close()

    print()
    if FAILURES:
        print(f"BRIDGE TEST: FAIL ({len(FAILURES)} failing: {', '.join(FAILURES)})")
        return 1
    print("BRIDGE TEST: PASS (all checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
