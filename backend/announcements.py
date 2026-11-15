"""NSE announcement normalisation and persistence.

Ingestion only. The read-only feed query lives in today.py. Freshness rule is
the same strict gate as price ingestion: only a verified live fetch is stored.
"""
from __future__ import annotations

import hashlib
import time as _time
from datetime import datetime, timedelta, timezone

from database import DB_PATH, initialize, session
from ingest import freshness_gate
from materiality import classify

IST = timezone(timedelta(hours=5, minutes=30))
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                 "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y", "%Y-%m-%d")
# NSE field names; the first non-empty string wins. Subject/date names mirror sources/nse.py.
_SUBJECT = ("desc", "subject")
_DESCRIPTION = ("attchmntText", "details", "body")
_DATE = ("an_dt", "sort_date", "exchdisstime")
_LINK = ("attchmntFile", "attachment")
_ID = ("seq_id",)


def _text(record, keys):
    for k in keys:
        v = record.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _parse_time(value):
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=IST).isoformat(timespec="seconds")
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=IST)).isoformat(timespec="seconds")


def normalize_record(symbol: str, record):
    """-> (row_dict, None) or (None, reason). Never raises on malformed input."""
    if not isinstance(record, dict):
        return None, "not_an_object"
    rec_symbol = record.get("symbol")
    if isinstance(rec_symbol, str) and rec_symbol.strip().upper() != symbol:
        return None, "symbol_mismatch"
    subject = _text(record, _SUBJECT)
    if subject is None:
        return None, "missing_subject"
    raw_time = _text(record, _DATE)
    announced_at = _parse_time(raw_time) if raw_time else None
    if announced_at is None:
        return None, "missing_or_unparseable_date"
    link = _text(record, _LINK)
    if link is not None and not link.lower().startswith(("http://", "https://")):
        link = None                                   # missing attachment is allowed; junk is dropped
    description = _text(record, _DESCRIPTION)
    seq = _text(record, _ID) or (str(record[_ID[0]]) if isinstance(record.get(_ID[0]), int)
                                 and not isinstance(record.get(_ID[0]), bool) else None)
    if seq:
        source_id = f"seq:{seq}"
    else:                                             # deterministic fallback key
        digest = hashlib.sha256("|".join(
            [symbol, announced_at, subject, link or "", description or ""]).encode("utf-8"))
        source_id = f"sha256:{digest.hexdigest()[:32]}"
    row = {"symbol": symbol, "source": "nse", "source_id": source_id,
           "announced_at": announced_at, "subject": subject, "description": description,
           "attachment_url": link}
    row.update(classify(subject, description))
    return row, None


def ingest_symbol_announcements(symbol, result, *, db_path=DB_PATH, now=None) -> dict:
    symbol = symbol.strip().upper()
    out = {"symbol": symbol, "inserted": 0, "duplicates": 0, "malformed": 0,
           "malformed_reasons": {}, "by_materiality": {}}
    rejection = freshness_gate(result)
    if rejection is not None:                          # nothing written on any rejection
        return {**out, "status": rejection,
                "error": result.get("error") if isinstance(result, dict) else None}
    records = result.get("records")
    if not isinstance(records, list):
        return {**out, "status": "failed", "error": "records missing"}
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    rows = []
    for rec in records:
        row, reason = normalize_record(symbol, rec)
        if row is None:
            out["malformed"] += 1
            out["malformed_reasons"][reason] = out["malformed_reasons"].get(reason, 0) + 1
        else:
            rows.append(row)
    with session(db_path) as conn:                     # one transaction per symbol
        for row in rows:
            cur = conn.execute(
                """INSERT INTO announcements
                   (symbol, source, source_id, announced_at, subject, description,
                    attachment_url, category, materiality, matched_rule, ingested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol, source, source_id) DO NOTHING""",
                (row["symbol"], row["source"], row["source_id"], row["announced_at"],
                 row["subject"], row["description"], row["attachment_url"], row["category"],
                 row["materiality"], row["matched_rule"], stamp))
            if cur.rowcount == 1:
                out["inserted"] += 1
                out["by_materiality"][row["materiality"]] = \
                    out["by_materiality"].get(row["materiality"], 0) + 1
            else:
                out["duplicates"] += 1
    return {**out, "status": "ok"}


def ingest_announcements(symbols, fetcher, *, db_path=DB_PATH, now=None,
                         pause=0.0, sleep=_time.sleep) -> list[dict]:
    """fetcher(symbol) returns the sources.nse_records.fetch_records contract.
    One symbol failing never stops or corrupts another."""
    symbols = [s.strip().upper() for s in symbols]
    initialize(db_path, symbols)                       # also performs the additive v3->v4 migration
    results = []
    for i, sym in enumerate(symbols):
        if i and pause:
            sleep(pause)
        try:
            results.append(ingest_symbol_announcements(sym, fetcher(sym), db_path=db_path, now=now))
        except Exception as exc:
            results.append({"symbol": sym, "status": "error", "error": repr(exc), "inserted": 0})
    return results
