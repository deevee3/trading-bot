"""Market data: yfinance (primary, free), Alpha Vantage (optional),
bundled CSV fixtures (offline). Results are cached as CSV under
``data/cache/`` so repeat runs and backtests don't hammer the network.

yfinance is imported lazily — the fixture path and the whole offline test
suite run on the standard library alone.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(BASE_DIR, "fixtures")
CACHE_MAX_AGE_SECONDS = 6 * 3600  # intraday runs reuse the same daily bars


@dataclass
class Bar:
    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int


def _bars_from_csv(path: str) -> list[Bar]:
    bars: list[Bar] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append(
                Bar(
                    date=row["date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["volume"])),
                )
            )
    bars.sort(key=lambda b: b.date)
    return bars


def _bars_to_csv(bars: list[Bar], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.date, b.open, b.high, b.low, b.close, b.volume])


def fixture_bars(symbol: str) -> list[Bar]:
    """Bundled offline data. Any symbol falls back to the FIXTURE series so
    backtests and smoke tests always work with zero network."""
    path = os.path.join(FIXTURES_DIR, f"{symbol.upper()}_daily.csv")
    if not os.path.exists(path):
        path = os.path.join(FIXTURES_DIR, "FIXTURE_daily.csv")
    log.info("Loading fixture bars from %s", path)
    return _bars_from_csv(path)


def _yfinance_bars(symbol: str, days: int) -> list[Bar]:
    import yfinance as yf  # lazy: only needed for live data

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=int(days * 1.6) + 10)  # cover weekends
    hist = yf.Ticker(symbol).history(start=start.isoformat(), end=end.isoformat())
    bars: list[Bar] = []
    for ts, row in hist.iterrows():
        bars.append(
            Bar(
                date=ts.strftime("%Y-%m-%d"),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=int(row["Volume"]),
            )
        )
    return bars[-days:]


def _alphavantage_bars(symbol: str, api_key: str, days: int) -> list[Bar]:
    query = urllib.parse.urlencode(
        {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol,
            "outputsize": "full",
            "apikey": api_key,
        }
    )
    url = f"https://www.alphavantage.co/query?{query}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    series = payload.get("Time Series (Daily)", {})
    bars = [
        Bar(
            date=d,
            open=float(v["1. open"]),
            high=float(v["2. high"]),
            low=float(v["3. low"]),
            close=float(v["4. close"]),
            volume=int(v["5. volume"]),
        )
        for d, v in series.items()
    ]
    bars.sort(key=lambda b: b.date)
    return bars[-days:]


def get_bars(symbol: str, days: int, config) -> list[Bar]:
    """Dispatch by DATA_MODE: 'fixture' is fully offline; 'auto' tries the
    disk cache, then yfinance, then Alpha Vantage, then fixtures."""
    symbol = symbol.upper()
    if config.data_mode == "fixture":
        return fixture_bars(symbol)[-days:]

    cache_path = os.path.join(config.cache_dir, f"{symbol}_daily.csv")
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < CACHE_MAX_AGE_SECONDS:
            cached = _bars_from_csv(cache_path)
            if len(cached) >= min(days, 30):
                return cached[-days:]

    bars: list[Bar] = []
    try:
        bars = _yfinance_bars(symbol, days)
    except Exception as exc:  # network down, yfinance missing, bad symbol
        log.warning("yfinance failed for %s: %s", symbol, exc)
    if not bars and config.alphavantage_api_key:
        try:
            bars = _alphavantage_bars(symbol, config.alphavantage_api_key, days)
        except Exception as exc:
            log.warning("Alpha Vantage failed for %s: %s", symbol, exc)
    if bars:
        _bars_to_csv(bars, cache_path)
        return bars
    if os.path.exists(cache_path):
        log.warning("All live sources failed for %s; using stale cache", symbol)
        return _bars_from_csv(cache_path)[-days:]
    log.warning("All sources failed for %s; falling back to fixture data", symbol)
    return fixture_bars(symbol)[-days:]
