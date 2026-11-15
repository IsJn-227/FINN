"""Read-only Today feed over persisted alerts. No schema creation, no evaluation, no writes."""
import json

from database import DB_PATH, read_session


def get_today_alerts(sessions: int = 1, *, db_path=DB_PATH) -> list[dict]:
    """Alerts for the latest `sessions` trading sessions that have stored bars.

    Sessions come from daily_bars (so pending sessions are included), never from
    calendar arithmetic or the wall clock.
    """
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 1:
        raise ValueError("sessions must be an integer >= 1")
    with read_session(db_path) as conn:
        if conn is None:
            return []
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"alerts", "daily_bars"} <= have:
            return []
        dates = conn.execute(
            "SELECT DISTINCT session_date FROM daily_bars ORDER BY session_date DESC LIMIT ?",
            (sessions,)).fetchall()
        if not dates:
            return []
        rows = conn.execute(
            """SELECT id, symbol, session_date, kind, observed, threshold, evidence, created_at
               FROM alerts WHERE session_date >= ?
               ORDER BY session_date DESC, symbol, kind""",
            (dates[-1][0],)).fetchall()
    return [{**dict(r), "evidence": json.loads(r["evidence"])} for r in rows]


_RANK = {"high": 3, "medium": 2, "low": 1}


def get_recent_announcements(limit: int = 50, *, min_materiality: str = "low", symbol=None,
                             db_path=DB_PATH) -> list[dict]:
    """Newest stored announcements first. Read-only: never fetches, classifies or writes."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be an integer >= 1")
    if min_materiality not in _RANK:
        raise ValueError("min_materiality must be high, medium or low")
    wanted = [m for m, r in _RANK.items() if r >= _RANK[min_materiality]]
    with read_session(db_path) as conn:
        if conn is None:
            return []
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "announcements" not in have:
            return []
        sql = ("SELECT id, symbol, announced_at, subject, description, attachment_url, category, "
               "materiality, matched_rule, ingested_at FROM announcements WHERE materiality IN (%s)"
               % ",".join("?" * len(wanted)))
        args = list(wanted)
        if symbol is not None:
            sql += " AND symbol = ?"
            args.append(symbol.strip().upper())
        sql += " ORDER BY announced_at DESC, id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
