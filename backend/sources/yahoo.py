"""Yahoo Finance daily bars via yfinance.

Only `fetch_live` imports yfinance or touches the network. Cache modes rebuild
the probe facts from the stored table.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime
from statistics import median

from sources import cache
from sources.net import note_live_attempt

HISTORY_PERIOD = "6mo"
HISTORY_INTERVAL = "1d"
BROWSER_CACHE_NAME = "response"


def fetch(symbol: str, mode: str) -> dict:
    cached = cache.load("yahoo", symbol, BROWSER_CACHE_NAME)
    if mode == "cache_only":
        if cached is None:
            return _miss("cache-only mode and no cached payload; no network request was made")
        return _from_envelope(symbol, cached, from_cache=True, stale=False)
    if mode == "force_fail":
        if cached is None:
            return _miss("forced failure and no cached payload; fallback did not succeed")
        return _from_envelope(symbol, cached, from_cache=True, stale=True)
    if mode != "live":
        raise ValueError(mode)
    return _fetch_live(symbol)


def _fetch_live(symbol: str) -> dict:
    yahoo_symbol = f"{symbol}.NS"
    note_live_attempt(f"yahoo:{yahoo_symbol}")
    started = time.perf_counter()
    try:
        table, meta = _download_history(yahoo_symbol)
    except Exception as exc:  # library and network failures stay uncached
        latency_ms = (time.perf_counter() - started) * 1000
        return _error(str(exc), latency_ms)
    latency_ms = (time.perf_counter() - started) * 1000
    payload = {"meta": meta, "rows": table}
    cache.save("yahoo", symbol, payload, latency_ms, BROWSER_CACHE_NAME)
    envelope = cache.load("yahoo", symbol, BROWSER_CACHE_NAME)
    return _from_envelope(symbol, envelope, from_cache=False, stale=False)


def _download_history(yahoo_symbol: str) -> tuple[list[dict], dict]:
    import logging

    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("peewee").setLevel(logging.CRITICAL)
    ticker = yf.Ticker(yahoo_symbol)
    frame = ticker.history(
        period=HISTORY_PERIOD,
        interval=HISTORY_INTERVAL,
        auto_adjust=False,
        actions=False,
    )
    columns_index = frame.columns
    if getattr(columns_index, "nlevels", 1) > 1:
        frame = frame.copy()
        frame.columns = columns_index.get_level_values(0)
    columns = [str(column) for column in frame.columns]
    adjusted_field = _adjusted_field(columns)
    rows: list[dict] = []
    for index, row in frame.iterrows():
        item = {"date": _date_text(index)}
        for column in columns:
            item[_json_key(column)] = _number(row[column])
        rows.append(item)
    meta = {
        "yahoo_symbol": yahoo_symbol,
        "library": "yfinance",
        "library_version": getattr(yf, "__version__", None),
        "period": HISTORY_PERIOD,
        "interval": HISTORY_INTERVAL,
        "auto_adjust": False,
        "columns": columns,
        "adjusted_price_field": adjusted_field,
    }
    return rows, meta


def _from_envelope(symbol: str, envelope: dict, *, from_cache: bool, stale: bool) -> dict:
    payload = envelope["payload"]
    meta = payload["meta"]
    rows = payload["rows"]
    field = meta.get("adjusted_price_field")
    key = _json_key(field) if field else None
    prices = [_number(row.get(key)) for row in rows] if key else []
    volumes = [_number(row.get("volume")) for row in rows]
    dates = [row.get("date") for row in rows if row.get("date")]
    previous = prices[-2] if len(prices) >= 2 else None
    last = prices[-1] if prices else None
    volume_tail = volumes[-20:] if len(volumes) >= 20 else []
    volume_ready = len(volume_tail) == 20 and all(value is not None for value in volume_tail)
    gaps = _day_gaps(dates)
    typical_gap = median(gaps) if gaps else None
    daily = bool(rows) and typical_gap is not None and typical_gap <= 5
    return {
        "source": "yahoo",
        "symbol": symbol,
        "ok": True,
        "stale": stale,
        "from_cache": from_cache,
        "fetched_at": envelope["saved_at"],
        "served_at": cache.utc_now(),
        "request_latency_ms": envelope["request_latency_ms"],
        "error": None,
        "yahoo_symbol": meta.get("yahoo_symbol"),
        "library_version": meta.get("library_version"),
        "auto_adjust": meta.get("auto_adjust"),
        "columns": meta.get("columns"),
        "adjusted_price_field": field,
        "daily_history_available": daily,
        "sessions": len(rows),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "median_calendar_gap_days": typical_gap,
        "previous_close_available": previous is not None and last is not None,
        "previous_close": previous,
        "last_close": last,
        "volume_avg_20_available": volume_ready,
        "volume_avg_20": round(sum(volume_tail) / 20, 2) if volume_ready else None,
    }


def _adjusted_field(columns: list[str]) -> str | None:
    for name in ("Adj Close", "AdjClose", "adjclose"):
        if name in columns:
            return name
    if "Close" in columns:
        return "Close"
    return None


def _json_key(column: str) -> str:
    return column.strip().lower().replace(" ", "_")


def _number(value):
    if value is None or value == "":
        return None
    if hasattr(value, "item"):
        value = value.item()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _date_text(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _day_gaps(dates: list[str]) -> list[int]:
    parsed: list[date] = []
    for text in dates:
        try:
            parsed.append(date.fromisoformat(text))
        except ValueError:
            continue
    return [(b - a).days for a, b in zip(parsed, parsed[1:]) if (b - a).days > 0]


def _miss(message: str) -> dict:
    return {
        "source": "yahoo",
        "ok": False,
        "stale": False,
        "from_cache": False,
        "fetched_at": None,
        "served_at": cache.utc_now(),
        "request_latency_ms": None,
        "error": message,
        "daily_history_available": False,
        "previous_close_available": False,
        "volume_avg_20_available": False,
        "adjusted_price_field": None,
    }


def _error(message: str, latency_ms: float) -> dict:
    body = _miss(message)
    body["request_latency_ms"] = round(latency_ms, 1)
    return body
