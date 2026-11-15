"""Per-session alert evaluation. SQLite only: no network, no clock."""
from __future__ import annotations

import json
import math
import numbers
from datetime import date

PRICE_FIELD = "Adj Close"
UNAVAILABLE = "unavailable"
VOLUME_WINDOW = 20
MAX_SESSION_GAP_DAYS = 6        # weekend + holidays; heuristic, no NSE calendar
MAX_VOLUME_WINDOW_DAYS = 50     # 20 sessions normally span ~28 calendar days


def num(value):
    """Finite float or None. Rejects bool, strings, NaN, inf. Accepts numpy scalars."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def normalize_date(value):
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _days(later, earlier):
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def save_bars(conn, symbol, rows, price_basis, fetched_at=None):
    """Upsert bars. Adjusted close is stored only when the basis is verified Adj Close.

    If the basis is unavailable, only volume is refreshed: previously stored verified
    adjusted prices are never overwritten with NULL, and raw close is never stored.
    """
    n = 0
    for r in rows:
        d = normalize_date(r.get("date"))
        if d is None:
            continue
        vol = num(r.get("volume"))
        if price_basis == PRICE_FIELD:
            conn.execute(
                """INSERT INTO daily_bars(symbol, session_date, adjusted_close, volume, price_field, fetched_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(symbol, session_date) DO UPDATE SET
                     adjusted_close=excluded.adjusted_close, volume=excluded.volume,
                     price_field=excluded.price_field, fetched_at=excluded.fetched_at""",
                (symbol, d, num(r.get("adj_close")), vol, PRICE_FIELD, fetched_at))
        else:
            conn.execute(
                """INSERT INTO daily_bars(symbol, session_date, adjusted_close, volume, price_field, fetched_at)
                   VALUES (?,?,NULL,?,?,?)
                   ON CONFLICT(symbol, session_date) DO UPDATE SET
                     volume=excluded.volume, fetched_at=excluded.fetched_at""",
                (symbol, d, vol, UNAVAILABLE, fetched_at))
        n += 1
    return n


def _mark(conn, symbol, session_date, mode):
    conn.execute(
        "INSERT OR IGNORE INTO evaluated_sessions(symbol, session_date, mode) VALUES (?,?,?)",
        (symbol, session_date, mode))


def _insert_alert(conn, symbol, session_date, kind, observed, threshold, evidence):
    cur = conn.execute(
        """INSERT OR IGNORE INTO alerts(symbol, session_date, kind, observed, threshold, evidence)
           VALUES (?,?,?,?,?,?)""",
        (symbol, session_date, kind, observed, threshold, json.dumps(evidence, sort_keys=True)))
    return cur.rowcount == 1


def _checks_done(conn, symbol, session_date):
    return {r[0] for r in conn.execute(
        "SELECT check_name FROM session_checks WHERE symbol=? AND session_date=?",
        (symbol, session_date))}


def _mark_check(conn, symbol, session_date, name):
    conn.execute(
        "INSERT OR IGNORE INTO session_checks(symbol, session_date, check_name) VALUES (?,?,?)",
        (symbol, session_date, name))


def _bar_complete(row):
    price, vol = num(row["adjusted_close"]), num(row["volume"])
    return (row["price_field"] == PRICE_FIELD and price is not None and price > 0
            and vol is not None and vol > 0)


def _price_check(conn, symbol, rows, idx, limit, fetched_at):
    """Returns (state, created_kind). 'done' only if a valid comparison was performed."""
    cur = rows[idx]
    if cur["price_field"] != PRICE_FIELD:
        return "blocked:basis_unavailable", None
    cur_price = num(cur["adjusted_close"])
    if cur_price is None or cur_price <= 0:
        return "blocked:invalid_current_bar", None
    if idx == 0:
        return "blocked:no_previous_session", None
    prev = rows[idx - 1]
    prev_price = num(prev["adjusted_close"])
    if prev["price_field"] != PRICE_FIELD or prev_price is None or prev_price <= 0:
        return "blocked:invalid_previous_bar", None
    if _days(cur["session_date"], prev["session_date"]) > MAX_SESSION_GAP_DAYS:
        return "blocked:session_gap", None
    change = round((cur_price / prev_price - 1) * 100, 6)
    created = None
    if abs(change) >= limit:
        kind = "price_up" if change > 0 else "price_down"
        if _insert_alert(conn, symbol, cur["session_date"], kind, change, limit, {
            "price_field": PRICE_FIELD,
            "previous_session": prev["session_date"],
            "current_session": cur["session_date"],
            "previous_adjusted_close": prev_price,
            "current_adjusted_close": cur_price,
            "change_pct": change,
            "threshold_pct": limit,
            "source_fetched_at": fetched_at,
        }):
            created = kind
    return "done", created


def _volume_check(conn, symbol, rows, idx, limit, fetched_at):
    cur = rows[idx]
    cur_vol = num(cur["volume"])
    if cur_vol is None or cur_vol <= 0:
        return "blocked:invalid_current_bar", None
    window = rows[max(0, idx - VOLUME_WINDOW):idx]
    if len(window) < VOLUME_WINDOW:
        return "blocked:insufficient_history", None
    vols = [num(r["volume"]) for r in window]
    if any(v is None or v <= 0 for v in vols):
        return "blocked:invalid_window_volume", None
    if _days(cur["session_date"], window[0]["session_date"]) > MAX_VOLUME_WINDOW_DAYS:
        return "blocked:window_span", None
    avg = sum(vols) / VOLUME_WINDOW
    multiple = round(cur_vol / avg, 6)
    created = None
    if multiple >= limit:
        if _insert_alert(conn, symbol, cur["session_date"], "volume_spike", multiple, limit, {
            "current_session": cur["session_date"],
            "current_volume": cur_vol,
            "preceding_20_session_average": round(avg, 2),
            "window_start": window[0]["session_date"],
            "window_end": window[-1]["session_date"],
            "multiple": multiple,
            "threshold_multiple": limit,
            "source_fetched_at": fetched_at,
        }):
            created = "volume_spike"
    return "done", created


def evaluate_session(conn, symbol, session_date, *, allow_alerts, source_fetched_at=None):
    """Evaluate one session. Caller owns the transaction.

    status: 'evaluated'  - both checks complete (or seeded); never retried
            'pending'    - a check is blocked; retried next run. Completed checks are
                           recorded in session_checks and never re-run, so a threshold
                           change cannot apply to a check that already ran.
            'missing_bar', 'already_evaluated'
    """
    if conn.execute(
        "SELECT 1 FROM evaluated_sessions WHERE symbol=? AND session_date=?",
        (symbol, session_date)).fetchone():
        return {"status": "already_evaluated", "created": []}

    rows = conn.execute(
        """SELECT session_date, adjusted_close, volume, price_field
           FROM daily_bars WHERE symbol=? AND session_date<=? ORDER BY session_date""",
        (symbol, session_date)).fetchall()
    if not rows or rows[-1]["session_date"] != session_date:
        return {"status": "missing_bar", "created": []}
    idx = len(rows) - 1

    if not allow_alerts:
        if _bar_complete(rows[idx]):
            _mark(conn, symbol, session_date, "seeded")
            return {"status": "evaluated", "created": []}
        return {"status": "pending", "created": [], "states": {"seed": "blocked:incomplete_bar"}}

    limits = conn.execute(
        "SELECT price_pct, volume_multiple FROM thresholds WHERE symbol=?", (symbol,)).fetchone()
    if limits is None:
        raise ValueError(f"No thresholds configured for {symbol}")

    done = _checks_done(conn, symbol, session_date)
    states, created = {}, []
    for name, fn, limit in (("price", _price_check, limits["price_pct"]),
                            ("volume", _volume_check, limits["volume_multiple"])):
        if name in done:
            states[name] = "done"
            continue
        states[name], kind = fn(conn, symbol, rows, idx, limit, source_fetched_at)
        if kind:
            created.append(kind)
        if states[name] == "done":
            _mark_check(conn, symbol, session_date, name)

    if all(s == "done" for s in states.values()):
        _mark(conn, symbol, session_date, "alerted")
        return {"status": "evaluated", "created": created, "states": states}
    return {"status": "pending", "created": created, "states": states}
