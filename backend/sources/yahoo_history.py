"""Live daily history for alert ingestion.

Reuses the existing Milestone 1 pieces instead of duplicating them:
  - sources.yahoo._download_history  (the one yfinance call, auto_adjust=False)
  - sources.cache.save               (same cache file and envelope fetch() reads)

Rows come ONLY from the live download made inside this call. There is no cache
read and no stale fallback here; a failed download returns ok=False with no rows.
sources/yahoo.py is not modified, so fetch() and the Milestone 1 probes are unchanged.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from sources import cache
from sources import yahoo as _yahoo
from sources.net import note_live_attempt

PRICE_FIELD = "Adj Close"          # the only field that ever qualifies for price alerts


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _failure(error: str) -> dict:
    return {"ok": False, "stale": False, "from_cache": False, "rows": [],
            "price_field": None, "fetched_at": None, "error": error}


def _default_downloader(yahoo_symbol: str):
    return _yahoo._download_history(yahoo_symbol)       # -> (rows, meta)


def _default_cache_writer(symbol: str, payload: dict, latency_ms: float) -> None:
    cache.save("yahoo", symbol, payload, latency_ms, _yahoo.BROWSER_CACHE_NAME)


def fetch_history(symbol: str, *, downloader=None, cache_writer=None, clock=None) -> dict:
    """Contract: ok, stale, from_cache, rows, price_field, fetched_at, error.

    ok=True implies: rows came from the download made in this call, stale=False,
    from_cache=False, fetched_at is the clock reading taken when the download returned,
    and the payload was saved through cache.save. Each row is
    {"date", "adj_close", "close", "volume"}. adj_close is populated only when the real
    "Adj Close" column exists (never from "Close"); price_field is "Adj Close" only when
    at least one finite adj_close exists, otherwise None.
    """
    symbol = symbol.strip().upper()
    downloader = downloader or _default_downloader
    cache_writer = cache_writer or _default_cache_writer
    clock = clock or (lambda: datetime.now(timezone.utc))

    yahoo_symbol = f"{symbol}.NS"
    note_live_attempt(f"yahoo:{yahoo_symbol}")
    started = time.perf_counter()
    try:
        raw_rows, meta = downloader(yahoo_symbol)
    except Exception as exc:
        return _failure(f"download failed: {exc!r}")
    latency_ms = (time.perf_counter() - started) * 1000
    fetched_at = clock().isoformat()

    if not raw_rows or not isinstance(meta, dict):
        return _failure("download returned no rows")
    columns = meta.get("columns") or []
    if "Volume" not in columns:
        return _failure("download has no Volume column")
    has_adj = PRICE_FIELD in columns          # NOT meta["adjusted_price_field"]: that may be raw Close

    rows = []
    for r in raw_rows:
        rows.append({
            "date": r.get("date"),
            "adj_close": _finite(r.get("adj_close")) if has_adj else None,
            "close": _finite(r.get("close")),      # display only; never used for alerts
            "volume": _finite(r.get("volume")),
        })
    adjusted_ok = has_adj and any(r["adj_close"] is not None for r in rows)

    try:
        cache_writer(symbol, {"meta": meta, "rows": raw_rows}, latency_ms)
    except Exception as exc:
        return _failure(f"cache write failed: {exc!r}")

    return {"ok": True, "stale": False, "from_cache": False, "rows": rows,
            "price_field": PRICE_FIELD if adjusted_ok else None,
            "fetched_at": fetched_at, "error": None}
