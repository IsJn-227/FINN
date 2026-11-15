"""NSE corporate announcements.

The public filings page reads JSON from /api/corporate-announcements. A symbol
query is limited to the last 180 days so the probe does not download a
company's entire filing history.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

import httpx

from sources import cache
from sources.net import note_live_attempt

API_URL = "https://www.nseindia.com/api/corporate-announcements"
HOME_URL = "https://www.nseindia.com/"
WINDOW_DAYS = 180
TITLE_FIELDS = ("desc", "subject")
DATE_FIELDS = ("an_dt", "sort_date", "exchdisstime")
LINK_FIELDS = ("attchmntFile", "attachment")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}


def fetch(symbol: str, mode: str) -> dict:
    cached = cache.load("nse", symbol)
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
    end = date.today()
    start = end - timedelta(days=WINDOW_DAYS)
    params = {
        "index": "equities",
        "symbol": symbol,
        "from_date": start.strftime("%d-%m-%Y"),
        "to_date": end.strftime("%d-%m-%Y"),
    }
    started = time.perf_counter()
    try:
        status, body, content_type, warmup = _download(symbol, params)
    except Exception as exc:
        return _error(str(exc), (time.perf_counter() - started) * 1000, params)
    latency_ms = (time.perf_counter() - started) * 1000
    if status != 200:
        return _error(
            f"NSE returned HTTP {status}",
            latency_ms,
            params,
            http_status=status,
            content_type=content_type,
        )
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return _error(
            "NSE response was not JSON",
            latency_ms,
            params,
            http_status=status,
            content_type=content_type,
        )
    if not isinstance(parsed, list):
        return _error(
            f"NSE JSON was a {type(parsed).__name__}, not a list",
            latency_ms,
            params,
            http_status=status,
            content_type=content_type,
        )
    payload = {
        "request": params,
        "http_status": status,
        "content_type": content_type,
        "cookie_warmup_used": warmup,
        "records": parsed,
    }
    cache.save("nse", symbol, payload, latency_ms)
    envelope = cache.load("nse", symbol)
    return _from_envelope(symbol, envelope, from_cache=False, stale=False)


def _download(symbol: str, params: dict) -> tuple[int, str, str, bool]:
    timeout = httpx.Timeout(45.0)
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        note_live_attempt(f"nse:{symbol}")
        response = client.get(API_URL, params=params)
        warmup = False
        if not _json_body(response):
            note_live_attempt(f"nse:{symbol}:home")
            client.get(HOME_URL, headers={**HEADERS, "Accept": "text/html"})
            note_live_attempt(f"nse:{symbol}:retry")
            response = client.get(API_URL, params=params)
            warmup = True
        return response.status_code, response.text, response.headers.get("content-type", ""), warmup


def _json_body(response: httpx.Response) -> bool:
    if response.status_code != 200:
        return False
    if "json" in response.headers.get("content-type", "").lower():
        return True
    text = response.text.lstrip()
    return text.startswith("[") or text.startswith("{")


def _from_envelope(symbol: str, envelope: dict, *, from_cache: bool, stale: bool) -> dict:
    payload = envelope["payload"]
    records = payload["records"]
    titled = dated = linked = complete = 0
    title_field = date_field = link_field = None
    sample = None
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        title_key, title = _pick(record, TITLE_FIELDS)
        date_key, when = _pick(record, DATE_FIELDS)
        link_key, link = _pick(record, LINK_FIELDS)
        if link and not link.startswith("http"):
            link_key, link = None, None
        if title:
            titled += 1
            title_field = title_field or title_key
        if when:
            dated += 1
            date_field = date_field or date_key
        if link:
            linked += 1
            link_field = link_field or link_key
        if title and when and link:
            complete += 1
            if sample is None:
                sample = {
                    "position": index,
                    "title": title,
                    "date": when,
                    "document_link": link,
                }
    return {
        "source": "nse",
        "symbol": symbol,
        "ok": True,
        "stale": stale,
        "from_cache": from_cache,
        "fetched_at": envelope["saved_at"],
        "served_at": cache.utc_now(),
        "request_latency_ms": envelope["request_latency_ms"],
        "error": None,
        "window": payload["request"],
        "http_status": payload["http_status"],
        "content_type": payload["content_type"],
        "cookie_warmup_used": payload["cookie_warmup_used"],
        "announcements_returned": len(records) > 0,
        "count": len(records),
        "records_with_title": titled,
        "records_with_date": dated,
        "records_with_document_link": linked,
        "records_with_title_date_and_link": complete,
        "title_field": title_field,
        "date_field": date_field,
        "document_field": link_field,
        "sample": sample,
    }


def _pick(record: dict, keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return None, None


def _miss(message: str) -> dict:
    return {
        "source": "nse",
        "ok": False,
        "stale": False,
        "from_cache": False,
        "fetched_at": None,
        "served_at": cache.utc_now(),
        "request_latency_ms": None,
        "error": message,
        "announcements_returned": False,
        "count": 0,
        "records_with_title": 0,
        "records_with_date": 0,
        "records_with_document_link": 0,
    }


def _error(message: str, latency_ms: float, params: dict, http_status: int | None = None, content_type: str | None = None) -> dict:
    body = _miss(message)
    body["request_latency_ms"] = round(latency_ms, 1)
    body["window"] = params
    body["http_status"] = http_status
    body["content_type"] = content_type
    return body
