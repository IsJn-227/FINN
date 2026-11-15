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
