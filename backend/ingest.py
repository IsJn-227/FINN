"""Freshness gate, IST cutoff, seeding and per-symbol ingestion."""
from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta, timezone

from alerts import (MAX_SESSION_GAP_DAYS, PRICE_FIELD, UNAVAILABLE, evaluate_session,
                    normalize_date, save_bars)
from database import DB_PATH, initialize, session

IST = timezone(timedelta(hours=5, minutes=30))   # India has no DST
SESSION_FINAL_TIME = time(18, 0)                 # NSE closes 15:30; slack for Yahoo to finalise


def last_complete_session_cutoff(now=None) -> str:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IST)
    day = local.date() if local.time() >= SESSION_FINAL_TIME else local.date() - timedelta(days=1)
    return day.isoformat()


def freshness_gate(source_status) -> str | None:
    """None only for a verifiably fresh live fetch. Identity checks: missing/None/0/1 never pass."""
    if not isinstance(source_status, dict):
        return "failed"
    if source_status.get("ok") is not True:
        return "failed"
    if source_status.get("stale") is not False:
        return "stale_or_unverified"
    if source_status.get("from_cache") is not False:
        return "cached_or_unverified"
    return None


def ingest_history(symbol, rows, *, price_field, source_status, now=None,
                   fetched_at=None, db_path=DB_PATH) -> dict:
    symbol = symbol.strip().upper()
    out = {"symbol": symbol, "alerts_created": [], "sessions_seeded": 0}

    rejection = freshness_gate(source_status)
    if rejection is not None:                      # nothing is written on any rejection
        return {**out, "status": rejection, "error": source_status.get("error")
                if isinstance(source_status, dict) else None}

    price_basis = PRICE_FIELD if price_field == PRICE_FIELD else UNAVAILABLE
    out["price_alerts_enabled"] = price_basis == PRICE_FIELD

    cutoff = last_complete_session_cutoff(now)
    by_date = {}
    for r in rows:
        d = normalize_date(r.get("date"))
        if d is not None and d <= cutoff:          # drops partial/future bars; last duplicate wins
            by_date[d] = {"date": d, "adj_close": r.get("adj_close"), "volume": r.get("volume")}
    out["cutoff"] = cutoff
    if not by_date:
        return {**out, "status": "no_complete_sessions"}

    dates = sorted(by_date)
    latest = dates[-1]
    out["latest_session"] = latest
    if (date.fromisoformat(cutoff) - date.fromisoformat(latest)).days > MAX_SESSION_GAP_DAYS:
        return {**out, "status": "data_lag"}

    with session(db_path) as conn:                 # one transaction per symbol
        save_bars(conn, symbol, [by_date[d] for d in dates], price_basis, fetched_at)
        seeded = 0
        for d in dates[:-1]:                       # history: seed silently, never alert
            if evaluate_session(conn, symbol, d, allow_alerts=False)["status"] == "evaluated":
                seeded += 1
        target = evaluate_session(conn, symbol, latest, allow_alerts=True,
                                  source_fetched_at=fetched_at)

    return {**out, "status": "ok", "sessions_seeded": seeded, "target": target,
            "alerts_created": target["created"]}


def ingest_portfolio(symbols, fetcher, *, now=None, db_path=DB_PATH,
                     pause=0.0, sleep=_time.sleep) -> list[dict]:
    """Ingest each symbol independently. `fetcher(symbol)` must return the
    fetch_history contract. One symbol failing never stops or corrupts another."""
    symbols = [s.strip().upper() for s in symbols]
    initialize(db_path, symbols)
    results = []
    for i, sym in enumerate(symbols):
        if i and pause:
            sleep(pause)
        try:
            res = fetcher(sym)
            results.append(ingest_history(
                sym, res.get("rows") or [], price_field=res.get("price_field"),
                source_status=res, now=now, fetched_at=res.get("fetched_at"),
                db_path=db_path))
        except Exception as exc:
            results.append({"symbol": sym, "status": "error", "error": repr(exc),
                            "alerts_created": []})
    return results
