"""Configuration loading + the PAPER-ONLY safety guard.

Secrets come from ``.env`` (BOT_TOKEN, ALPACA_API_KEY, ...). Nothing secret
is ever hardcoded here. The Alpaca base URL is hard-locked to the PAPER
host: configuring any other host is a fatal startup error, by design.
There is intentionally NO setting that enables live trading.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger(__name__)

from .bridge import Bridge, normalize_transport  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")

# --- PAPER-ONLY constants ---------------------------------------------------
PAPER_HOST = "paper-api.alpaca.markets"
PAPER_BASE_URL = f"https://{PAPER_HOST}"
LIVE_HOST = "api.alpaca.markets"  # named only so we can refuse it loudly


def assert_paper_url(url: str | None) -> str:
    """Return the normalized paper base URL, or raise RuntimeError.

    Any URL whose host is not exactly ``paper-api.alpaca.markets`` is
    refused — including the live Alpaca host, http:// variants, and
    look-alike hosts.
    """
    candidate = (url or PAPER_BASE_URL).strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or parsed.hostname != PAPER_HOST:
        raise RuntimeError(
            f"REFUSING to start: Alpaca base URL {candidate!r} is not the "
            f"PAPER endpoint ({PAPER_BASE_URL}). This bot is PAPER ONLY — "
            "there is no live-trading mode and there never will be."
        )
    return candidate


def assert_paper_key(api_key: str | None) -> None:
    """Refuse Alpaca LIVE keys. Paper keys start with 'PK', live with 'AK'."""
    if api_key and api_key.strip().upper().startswith("AK"):
        raise RuntimeError(
            "REFUSING to start: ALPACA_API_KEY looks like a LIVE Alpaca key "
            "(live keys start with 'AK'; paper keys start with 'PK'). This "
            "bot is PAPER ONLY. Generate paper keys from the paper-trading "
            "dashboard at app.alpaca.markets (paper mode)."
        )


def _load_env_file(path: str = ENV_PATH) -> None:
    """Load .env into os.environ (python-dotenv if present, else a tiny
    built-in parser so the core never hard-depends on it)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
        return
    except ImportError:
        pass
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("Invalid integer for %s; using %d", name, default)
        return default


@dataclass
class AppConfig:
    bot_token: str = ""
    admin_user_id: int = 0
    admin_chat_id: int = 0  # alerts target; defaults to admin_user_id

    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = PAPER_BASE_URL
    # Real paper-API calls happen ONLY when keys exist AND this is "true".
    alpaca_paper_enabled: bool = False

    alphavantage_api_key: str = ""  # optional second data source

    data_mode: str = "auto"  # auto | fixture
    watchlist_seed: list[str] = field(default_factory=lambda: ["AAPL", "MSFT", "SPY"])
    poll_minutes: int = 60
    timezone: str = "America/New_York"

    sma_fast: int = 10
    sma_slow: int = 30
    trade_notional_usd: float = 1000.0

    db_path: str = os.path.join(BASE_DIR, "data", "trading.db")
    cache_dir: str = os.path.join(BASE_DIR, "data", "cache")
    mock_state_path: str = os.path.join(BASE_DIR, "data", "mock_account.json")

    transport: str = "telegram"
    bot_hq_url: str = ""
    bot_hq_key: str = ""

    # Moon-style LLM panel (advisory only — can never trigger an order).
    # Default OFF; the iMac deployment enables it against the free local
    # models on the Mac Studio. See bot/llm.py.
    llm_enabled: bool = False
    llm_base_url: str = "https://mac-studio.tail22a20c.ts.net/v1"
    llm_model: str = "qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"
    llm_api_key: str = ""
    llm_timeout_s: int = 120

    # Panel paper trading (bot/panelbook.py) — D'Vaughn's 2026-10-22
    # directive. The panel trades its OWN virtual book on consensus;
    # the strategy book is untouched. Real-money execution still does
    # not exist in this codebase — graduation is a REQUEST the human
    # approves in Bot HQ, never a switch the bot flips.
    panel_trading: bool = False
    panel_min_confidence: int = 70

    def make_bridge(self) -> Bridge:
        return Bridge(
            url=self.bot_hq_url,
            key=self.bot_hq_key,
            transport=self.transport,
            capabilities=["chat"],
        )

    @property
    def paper_live_calls(self) -> bool:
        """True only when real Alpaca PAPER calls are allowed."""
        return bool(
            self.alpaca_paper_enabled
            and self.alpaca_api_key
            and self.alpaca_api_secret
        )


def load_config() -> AppConfig:
    _load_env_file()

    # The guard runs on EVERY startup, before anything else can happen.
    base_url = assert_paper_url(os.environ.get("ALPACA_BASE_URL", PAPER_BASE_URL))
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    assert_paper_key(api_key)

    admin_user_id = _env_int("ADMIN_USER_ID", 0)
    admin_chat_id = _env_int("ADMIN_CHAT_ID", 0) or admin_user_id

    seed_raw = os.environ.get("WATCHLIST", "AAPL,MSFT,SPY")
    seed = [s.strip().upper() for s in seed_raw.split(",") if s.strip()]

    cfg = AppConfig(
        bot_token=os.environ.get("BOT_TOKEN", "").strip(),
        admin_user_id=admin_user_id,
        admin_chat_id=admin_chat_id,
        alpaca_api_key=api_key,
        alpaca_api_secret=os.environ.get("ALPACA_API_SECRET", "").strip(),
        alpaca_base_url=base_url,
        alpaca_paper_enabled=os.environ.get(
            "ALPACA_PAPER_ENABLED", "false"
        ).strip().lower()
        == "true",
        alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY", "").strip(),
        data_mode=os.environ.get("DATA_MODE", "auto").strip().lower(),
        watchlist_seed=seed,
        poll_minutes=_env_int("POLL_MINUTES", 60),
        timezone=os.environ.get("TIMEZONE", "America/New_York"),
        sma_fast=_env_int("SMA_FAST", 10),
        sma_slow=_env_int("SMA_SLOW", 30),
        db_path=os.environ.get(
            "DB_PATH", os.path.join(BASE_DIR, "data", "trading.db")
        ),
        transport=normalize_transport(os.environ.get("TRANSPORT", "telegram")),
        bot_hq_url=os.environ.get("BOT_HQ_URL", "").strip(),
        bot_hq_key=os.environ.get("BOT_HQ_KEY", "").strip(),
        llm_enabled=os.environ.get("LLM_ENABLED", "false").strip().lower()
        == "true",
        llm_base_url=os.environ.get(
            "LLM_BASE_URL", "https://mac-studio.tail22a20c.ts.net/v1"
        ).strip(),
        llm_model=os.environ.get(
            "LLM_MODEL", "qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"
        ).strip(),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_timeout_s=_env_int("LLM_TIMEOUT_S", 120),
        panel_trading=os.environ.get("PANEL_TRADING", "false").strip().lower()
        == "true",
        panel_min_confidence=_env_int("PANEL_MIN_CONFIDENCE", 70),
    )
    log.info(
        "Config loaded: mode=%s data=%s paper_calls=%s watchlist_seed=%s",
        "PAPER",
        cfg.data_mode,
        cfg.paper_live_calls,
        cfg.watchlist_seed,
    )
    return cfg
