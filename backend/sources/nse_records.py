"""Fresh raw NSE announcement records for ingestion.

Reuses sources.nse._download (headers, cookie warm-up, network guard) and
sources.cache.save with the SAME payload shape nse._fetch_live writes, so
nse.fetch() cache-only / force-fail modes keep working. sources/nse.py is not
modified. Records come only from the download made in this call: no cache read,
no stale fallback. If the cache write fails the result is NOT fresh.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone

from sources import cache
from sources import nse as _nse


def _failure(error: str) -> dict:
    return {"ok": False, "stale": False, "from_cache": False, "records": [],
            "fetched_at": None, "error": error}


def _default_cache_writer(symbol: str, payload: dict, latency_ms: float) -> None:
    cache.save("nse", symbol, payload, latency_ms)


def fetch_records(symbol: str, *, downloader=None, cache_writer=None, clock=None,
                  today=None) -> dict:
    symbol = symbol.strip().upper()
    downloader = downloader or _nse._download
    cache_writer = cache_writer or _default_cache_writer
    clock = clock or (lambda: datetime.now(timezone.utc))
    end = today or date.today()
    start = end - timedelta(days=_nse.WINDOW_DAYS)
    params = {"index": "equities", "symbol": symbol,
              "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")}
    started = time.perf_counter()
    try:
        status, body, content_type, warmup = downloader(symbol, params)
    except Exception as exc:
        return _failure(f"download failed: {exc!r}")
    latency_ms = (time.perf_counter() - started) * 1000
    fetched_at = clock().isoformat()
    if status != 200:
        return _failure(f"NSE returned HTTP {status}")
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return _failure("NSE response was not JSON")
    if not isinstance(parsed, list):
        return _failure(f"NSE JSON was a {type(parsed).__name__}, not a list")
    payload = {"request": params, "http_status": status, "content_type": content_type,
               "cookie_warmup_used": warmup, "records": parsed}
    try:
        cache_writer(symbol, payload, latency_ms)
    except Exception as exc:
        return _failure(f"cache write failed: {exc!r}")
    return {"ok": True, "stale": False, "from_cache": False, "records": parsed,
            "fetched_at": fetched_at, "error": None}
