import hashlib
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

from database import (initialize, inspect_db, read_session, session, set_thresholds)
from ingest import IST, freshness_gate, ingest_history, ingest_portfolio
from sources.yahoo_history import fetch_history
from today import get_today_alerts

FRESH = {"ok": True, "stale": False, "from_cache": False}


def make_rows(n, start=date(2026, 9, 1), price=100.0, volume=1000.0):
    rows, d = [], start
    while len(rows) < n:
        if d.weekday() < 5:
            rows.append({"date": d.isoformat(), "adj_close": price, "volume": volume})
        d += timedelta(days=1)
    return rows


def after_close(d):
    return datetime.combine(d, time(19, 0), tzinfo=IST)


def intraday(d):
    return datetime.combine(d, time(11, 0), tzinfo=IST)


def next_weekday(d):
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.sqlite3"
        initialize(self.db, symbols=["TEST"])
        self.rows = make_rows(25)
        self.rows[-1].update(adj_close=106.0, volume=2500.0)   # +6%, 2.5x
        self.last = date.fromisoformat(self.rows[-1]["date"])

    def tearDown(self):
        self.tmp.cleanup()    # fails on Windows if a connection leaked

    def ingest(self, rows=None, now=None, status=FRESH, field="Adj Close", symbol="TEST", db=None):
        rows = self.rows if rows is None else rows
        latest = date.fromisoformat(max(r["date"] for r in rows)[:10])
        return ingest_history(symbol, rows, price_field=field, source_status=status,
                              now=now or after_close(latest), fetched_at="2026-10-05T14:00:00+00:00",
                              db_path=db or self.db)

    def count(self, table, db=None):
        with session(db or self.db) as c:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def kinds(self):
        with session(self.db) as c:
            return [r[0] for r in c.execute("SELECT kind FROM alerts ORDER BY kind")]

    def evaluated_dates(self):
        with session(self.db) as c:
            return {r[0] for r in c.execute("SELECT session_date FROM evaluated_sessions")}


class AlertLogic(Base):
    def test_first_run_seeds_history_and_alerts_only_latest(self):
        self.rows[5]["adj_close"] = 130.0     # historic 30% spike must NOT alert
        self.rows[2]["volume"] = 50000.0   # outside the latest 20-session window
        self.assertEqual(self.ingest()["status"], "ok")
        self.assertEqual(self.kinds(), ["price_up", "volume_spike"])
        with session(self.db) as c:
            dates = {r[0] for r in c.execute("SELECT session_date FROM alerts")}
            modes = [r[0] for r in c.execute("SELECT mode FROM evaluated_sessions")]
        self.assertEqual(dates, {self.last.isoformat()})
        self.assertEqual(modes.count("seeded"), 24)
        self.assertEqual(modes.count("alerted"), 1)

    def test_evidence_snapshot(self):
        self.ingest()
        feed = {a["kind"]: a["evidence"] for a in get_today_alerts(1, db_path=self.db)}
        p, v = feed["price_up"], feed["volume_spike"]
        self.assertEqual(p["price_field"], "Adj Close")
        self.assertEqual(p["previous_session"], self.rows[-2]["date"])
        self.assertEqual((p["previous_adjusted_close"], p["current_adjusted_close"]), (100.0, 106.0))
        self.assertEqual(p["threshold_pct"], 5.0)
        self.assertEqual(p["source_fetched_at"], "2026-10-05T14:00:00+00:00")
        self.assertEqual(v["window_start"], self.rows[-21]["date"])
        self.assertEqual(v["window_end"], self.rows[-2]["date"])
        self.assertEqual(v["threshold_multiple"], 2.0)

    def test_exact_thresholds_fire_down(self):
        rows = make_rows(25)
        rows[-1].update(adj_close=95.0, volume=2000.0)
        self.ingest(rows=rows)
        self.assertEqual(self.kinds(), ["price_down", "volume_spike"])

    def test_just_below_thresholds_do_not_fire(self):
        rows = make_rows(25)
        rows[-1].update(adj_close=95.01, volume=1999.0)
        result = self.ingest(rows=rows)
        self.assertEqual(self.kinds(), [])
        self.assertEqual(result["target"]["status"], "evaluated")

    def test_volume_uses_preceding_20_excluding_current(self):
        rows = make_rows(25)
        for i, r in enumerate(rows):
            r["volume"] = 1000.0 + 10 * i                 # window idx 4..23 -> avg 1135
        rows[-1]["volume"] = 2270.0                      # exactly 2.0x of 1135
        self.ingest(rows=rows)
        self.assertEqual(self.kinds(), ["volume_spike"])
        ev = get_today_alerts(1, db_path=self.db)[0]["evidence"]
        self.assertEqual(ev["preceding_20_session_average"], 1135.0)
        self.assertEqual(ev["multiple"], 2.0)

    def test_volume_just_below_multiple(self):
        rows = make_rows(25)
        for i, r in enumerate(rows):
            r["volume"] = 1000.0 + 10 * i
        rows[-1]["volume"] = 2269.0
        self.ingest(rows=rows)
        self.assertEqual(self.kinds(), [])

    def test_fewer_than_20_prior_sessions(self):
        rows = make_rows(10)
        rows[-1].update(adj_close=110.0, volume=9000.0)
        result = self.ingest(rows=rows)
        self.assertEqual(self.kinds(), ["price_up"])
        self.assertEqual(result["target"]["states"]["volume"], "blocked:insufficient_history")
        self.assertEqual(result["target"]["status"], "pending")
        self.assertEqual(len(self.evaluated_dates()), 9)

    def test_zero_volume_in_window_blocks_volume_alert(self):
        self.rows[10]["volume"] = 0
        result = self.ingest()
        self.assertEqual(self.kinds(), ["price_up"])
        self.assertEqual(result["target"]["states"]["volume"], "blocked:invalid_window_volume")

    def test_invalid_latest_bar_stays_pending_then_recovers(self):
        bad = [dict(r) for r in self.rows]
        bad[-1]["adj_close"] = float("nan")
        bad[-1]["volume"] = None
        first = self.ingest(rows=bad)
        self.assertEqual(first["target"]["status"], "pending")
        self.assertEqual(self.count("session_checks"), 0)
        self.assertNotIn(self.last.isoformat(), self.evaluated_dates())
        self.ingest()
        self.assertEqual(self.kinds(), ["price_up", "volume_spike"])
        self.assertIn(self.last.isoformat(), self.evaluated_dates())

    def test_missing_previous_adjusted_close_keeps_session_pending(self):
        rows = [dict(r) for r in self.rows]
        rows[-2]["adj_close"] = None
        result = self.ingest(rows=rows)
        self.assertEqual(result["target"]["status"], "pending")
        self.assertEqual(result["target"]["states"]["price"], "blocked:invalid_previous_bar")
        self.assertEqual(result["target"]["states"]["volume"], "done")
        self.assertEqual(self.kinds(), ["volume_spike"])
        self.assertNotIn(self.last.isoformat(), self.evaluated_dates())
        self.ingest()                                            # complete data returns
        self.assertEqual(self.kinds(), ["price_up", "volume_spike"])
        self.assertEqual(self.count("alerts"), 2)                # volume not doubled

    def test_single_session_stays_pending(self):
        result = self.ingest(rows=make_rows(1))
        self.assertEqual(result["target"]["states"]["price"], "blocked:no_previous_session")
        self.assertEqual(self.count("evaluated_sessions"), 0)

    def test_incomplete_historical_bar_is_not_marked_seeded(self):
        rows = [dict(r) for r in self.rows]
        rows[2]["volume"] = None                    # outside the latest 20-session window
        result = self.ingest(rows=rows)
        self.assertEqual(result["target"]["status"], "evaluated")
        self.assertNotIn(rows[2]["date"], self.evaluated_dates())
        self.assertEqual(len(self.evaluated_dates()), 24)

    def test_raw_close_never_creates_price_alert_but_volume_works(self):
        result = self.ingest(field="Close")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["price_alerts_enabled"])
        self.assertEqual(self.kinds(), ["volume_spike"])
        self.assertEqual(result["target"]["states"]["price"], "blocked:basis_unavailable")
        with session(self.db) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM daily_bars WHERE adjusted_close IS NOT NULL").fetchone()[0], 0)
        self.assertNotIn(self.last.isoformat(), self.evaluated_dates())

    def test_price_alert_recovers_when_adjusted_basis_returns(self):
        self.ingest(field=None)
        self.ingest(field="Adj Close")
        self.assertEqual(self.kinds(), ["price_up", "volume_spike"])
        self.assertEqual(self.count("alerts"), 2)
        self.assertIn(self.last.isoformat(), self.evaluated_dates())

    def test_unavailable_basis_run_does_not_erase_stored_adjusted_prices(self):
        self.ingest()
        self.ingest(field=None)
        with session(self.db) as c:
            n = c.execute("SELECT COUNT(*) FROM daily_bars WHERE adjusted_close IS NOT NULL "
                          "AND price_field='Adj Close'").fetchone()[0]
        self.assertEqual(n, 25)

    def test_rerun_creates_no_duplicates(self):
        self.ingest()
        again = self.ingest()
        self.assertEqual(again["alerts_created"], [])
        self.assertEqual(again["target"]["status"], "already_evaluated")
        self.assertEqual(self.count("alerts"), 2)

    def test_unsorted_and_duplicate_dates(self):
        rows = list(reversed(self.rows)) + [dict(self.rows[-1])]
        self.ingest(rows=rows)
        self.assertEqual(self.kinds(), ["price_up", "volume_spike"])
        self.assertEqual(self.count("daily_bars"), 25)

    def test_timestamp_dates_are_normalised(self):
        rows = [dict(r, date=r["date"] + " 00:00:00+05:30") for r in self.rows]
        self.ingest(rows=rows)
        self.assertEqual(self.kinds(), ["price_up", "volume_spike"])

    def test_partial_session_excluded_until_after_cutoff(self):
        self.ingest(now=intraday(self.last))
        self.assertEqual(self.count("alerts"), 0)
        self.assertNotIn(self.last.isoformat(), self.evaluated_dates())
        with session(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM daily_bars WHERE session_date=?",
                                       (self.last.isoformat(),)).fetchone()[0], 0)
        self.ingest(now=after_close(self.last))
        self.assertEqual(self.kinds(), ["price_up", "volume_spike"])

    def test_future_bars_excluded(self):
        future = self.last + timedelta(days=3)
        rows = self.rows + [{"date": future.isoformat(), "adj_close": 1.0, "volume": 1.0}]
        self.ingest(rows=rows, now=after_close(self.last))
        self.assertEqual(self.count("daily_bars"), 25)

    def test_implausibly_old_latest_data_rejected(self):
        result = self.ingest(now=after_close(self.last + timedelta(days=30)))
        self.assertEqual(result["status"], "data_lag")
        self.assertEqual(self.count("daily_bars"), 0)

    def test_threshold_change_applies_only_to_new_sessions(self):
        self.ingest()
        set_thresholds("TEST", price_pct=1.0, volume_multiple=1.1, db_path=self.db)
        self.assertEqual(self.ingest()["alerts_created"], [])
        self.assertEqual(self.count("alerts"), 2)
        new = next_weekday(self.last)
        rows = self.rows + [{"date": new.isoformat(), "adj_close": 108.1, "volume": 1500.0}]
        self.ingest(rows=rows)
        with session(self.db) as c:
            got = dict(c.execute("SELECT kind, threshold FROM alerts WHERE session_date=?",
                                 (new.isoformat(),)).fetchall())
        self.assertEqual(got, {"price_up": 1.0, "volume_spike": 1.1})
        self.assertEqual(self.count("alerts"), 4)

    def test_completed_check_is_not_rerun_after_threshold_change(self):
        rows = [dict(r) for r in self.rows]
        rows[-1].update(adj_close=100.0, volume=1500.0)     # 1.5x volume, 0% price
        rows[-2]["adj_close"] = None                        # price blocked, volume completes
        self.ingest(rows=rows)
        self.assertEqual(self.count("alerts"), 0)
        set_thresholds("TEST", volume_multiple=1.1, db_path=self.db)
        rows[-2]["adj_close"] = 100.0
        result = self.ingest(rows=rows)
        self.assertEqual(result["target"]["status"], "evaluated")
        self.assertEqual(self.count("alerts"), 0)           # volume check not re-run at 1.1x

    def test_threshold_validation(self):
        for bad in (float("nan"), float("inf"), 0, -1, True, "5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                set_thresholds("TEST", price_pct=bad, db_path=self.db)
        with self.assertRaises(ValueError):
            set_thresholds("NOPE", price_pct=1, db_path=self.db)


class FreshnessGate(Base):
    CASES = {
        "stale": {"ok": True, "stale": True, "from_cache": False},
        "force_fail_shape": {"ok": True, "stale": True, "from_cache": True},
        "cache_only_shape": {"ok": True, "stale": False, "from_cache": True},
        "from_cache_missing": {"ok": True, "stale": False},
        "stale_missing": {"ok": True, "from_cache": False},
        "from_cache_none": {"ok": True, "stale": False, "from_cache": None},
        "from_cache_zero": {"ok": True, "stale": False, "from_cache": 0},
        "stale_zero": {"ok": True, "stale": 0, "from_cache": False},
        "not_ok": {"ok": False, "stale": False, "from_cache": False},
        "ok_truthy": {"ok": 1, "stale": False, "from_cache": False},
        "empty": {},
        "not_a_dict": None,
    }

    def test_non_fresh_status_never_writes(self):
        for name, status in self.CASES.items():
            with self.subTest(name):
                result = self.ingest(status=status)
                self.assertNotEqual(result["status"], "ok")
                for table in ("alerts", "daily_bars", "evaluated_sessions", "session_checks"):
                    self.assertEqual(self.count(table), 0)

    def test_gate_statuses(self):
        self.assertEqual(freshness_gate(FRESH), None)
        self.assertEqual(freshness_gate({"ok": True, "stale": False, "from_cache": True}),
                         "cached_or_unverified")
        self.assertEqual(freshness_gate({"ok": True, "stale": True, "from_cache": True}),
                         "stale_or_unverified")
        self.assertEqual(freshness_gate({"ok": False}), "failed")


class PortfolioRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "p.sqlite3"
        rows = make_rows(25)
        rows[-1].update(adj_close=106.0, volume=2500.0)
        self.rows = rows
        self.now = after_close(date.fromisoformat(rows[-1]["date"]))

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, bad=()):
        def f(sym):
            if sym in bad:
                raise RuntimeError("boom " + sym)
            return {**FRESH, "rows": self.rows, "price_field": "Adj Close",
                    "fetched_at": "2026-10-05T14:00:00+00:00", "error": None}
        return f

    def count(self, sql, *args):
        with session(self.db) as c:
            return c.execute(sql, args).fetchone()[0]

    def test_fetcher_exception_does_not_stop_run(self):
        results = ingest_portfolio(["A", "B", "C"], self.fetcher(bad={"B"}), now=self.now, db_path=self.db)
        self.assertEqual([r["status"] for r in results], ["ok", "error", "ok"])
        self.assertIn("boom B", results[1]["error"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_bars WHERE symbol='B'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM alerts"), 4)

    def test_database_failure_rolls_back_only_that_symbol(self):
        import ingest as ingest_module
        real = ingest_module.evaluate_session

        def flaky(conn, symbol, session_date, **kw):
            if symbol == "B" and kw.get("allow_alerts"):
                raise RuntimeError("db exploded after bars were written")
            return real(conn, symbol, session_date, **kw)

        with mock.patch.object(ingest_module, "evaluate_session", flaky):
            results = ingest_portfolio(["A", "B", "C"], self.fetcher(), now=self.now, db_path=self.db)
        self.assertEqual([r["status"] for r in results], ["ok", "error", "ok"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_bars WHERE symbol='B'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM evaluated_sessions WHERE symbol='B'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_bars WHERE symbol='A'"), 25)
        self.assertEqual(self.count("SELECT COUNT(*) FROM daily_bars WHERE symbol='C'"), 25)

    def test_non_dict_fetcher_result_is_contained(self):
        results = ingest_portfolio(["A"], lambda s: None, now=self.now, db_path=self.db)
        self.assertEqual(results[0]["status"], "error")

    def test_pause_between_symbols_only(self):
        sleeps = []
        ingest_portfolio(["A", "B", "C"], self.fetcher(), now=self.now, db_path=self.db,
                         pause=0.5, sleep=sleeps.append)
        self.assertEqual(sleeps, [0.5, 0.5])


class DatabaseSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "d.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_rolls_back_and_closes_on_error(self):
        initialize(self.db, symbols=["TEST"])
        with self.assertRaises(RuntimeError):
            with session(self.db) as c:
                c.execute("UPDATE thresholds SET price_pct=9 WHERE symbol='TEST'")
                raise RuntimeError
        with session(self.db) as c:
            self.assertEqual(c.execute("SELECT price_pct FROM thresholds").fetchone()[0], 5.0)

    def test_old_schema_is_refused_and_left_untouched(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("CREATE TABLE alerts (id INTEGER PRIMARY KEY, symbol TEXT);"
                           "INSERT INTO alerts VALUES (1, 'KEEPME');")
        conn.commit()
        conn.close()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaises(RuntimeError):
            initialize(self.db, symbols=["TEST"])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        info = inspect_db(self.db)
        self.assertEqual((info["exists"], info["compatible"]), (True, False))

    def test_inspect_missing_db_creates_nothing(self):
        self.assertEqual(inspect_db(self.db), {"exists": False})
        self.assertFalse(self.db.exists())

    def test_initialize_is_idempotent_and_validated(self):
        initialize(self.db, symbols=["TEST"])
        initialize(self.db, symbols=["TEST", "OTHER"])
        self.assertTrue(inspect_db(self.db)["compatible"])


class Feed(Base):
    def test_feed_reads_saved_alerts_by_session(self):
        self.ingest()
        feed = get_today_alerts(1, db_path=self.db)
        self.assertEqual(len(feed), 2)
        self.assertIsInstance(feed[0]["evidence"], dict)

    def test_feed_is_read_only(self):
        self.ingest()
        before = (self.count("alerts"), self.count("daily_bars"))
        get_today_alerts(5, db_path=self.db)
        self.assertEqual((self.count("alerts"), self.count("daily_bars")), before)
        with read_session(self.db) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM alerts")

    def test_feed_missing_or_empty_db_creates_nothing(self):
        missing = Path(self.tmp.name) / "none.sqlite3"
        self.assertEqual(get_today_alerts(1, db_path=missing), [])
        self.assertFalse(missing.exists())
        empty = Path(self.tmp.name) / "empty.sqlite3"
        sqlite3.connect(empty).close()
        self.assertEqual(get_today_alerts(1, db_path=empty), [])
        conn = sqlite3.connect(empty)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)
        conn.close()

    def test_feed_includes_pending_session(self):
        rows = [dict(r) for r in self.rows]
        rows[-2]["adj_close"] = None
        self.ingest(rows=rows)                       # latest pending, volume alert saved
        self.assertNotIn(self.last.isoformat(), self.evaluated_dates())
        feed = get_today_alerts(1, db_path=self.db)
        self.assertEqual([a["kind"] for a in feed], ["volume_spike"])

    def test_feed_validates_sessions(self):
        for bad in (0, -1, True, "1", 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                get_today_alerts(bad, db_path=self.db)


def raw_rows(with_adj=True, n=3):
    """Rows exactly as sources.yahoo._download_history produces them (lowercase keys)."""
    rows = []
    for i in range(n):
        row = {"date": f"2026-10-{1 + i:02d}", "open": 1.0, "close": 100.0 + i, "volume": 1000.0 + i}
        if with_adj:
            row["adj_close"] = 90.0 + i
        rows.append(row)
    return rows


def raw_meta(with_adj=True):
    cols = ["Open", "Close", "Volume"] + (["Adj Close"] if with_adj else [])
    # mirrors yahoo._adjusted_field: falls back to raw Close when Adj Close is absent
    return {"yahoo_symbol": "X.NS", "columns": cols,
            "adjusted_price_field": "Adj Close" if with_adj else "Close"}


def dl(with_adj=True, n=3):
    return lambda ticker: (raw_rows(with_adj, n), raw_meta(with_adj))


def no_cache(symbol, payload, latency_ms):
    pass


class YahooHistory(unittest.TestCase):
    CLOCK = staticmethod(lambda: datetime(2026, 10, 5, 14, 0))

    def run_fetch(self, sym="RELIANCE", **kw):
        kw.setdefault("cache_writer", no_cache)
        kw.setdefault("clock", self.CLOCK)
        return fetch_history(sym, **kw)

    def test_live_success_returns_rows_from_this_download_only(self):
        calls = []

        def downloader(ticker):
            calls.append(ticker)
            return raw_rows(), raw_meta()

        res = self.run_fetch(downloader=downloader)
        self.assertEqual(calls, ["RELIANCE.NS"])
        self.assertTrue(res["ok"] is True and res["stale"] is False and res["from_cache"] is False)
        self.assertEqual(res["price_field"], "Adj Close")
        self.assertEqual(res["fetched_at"], "2026-10-05T14:00:00")
        self.assertEqual([r["adj_close"] for r in res["rows"]], [90.0, 91.0, 92.0])
        self.assertEqual(res["rows"][0]["date"], "2026-10-01")
        self.assertIsNone(res["error"])

    def test_download_failure_is_not_fresh_and_has_no_rows(self):
        def downloader(ticker):
            raise ConnectionError("net down")

        res = self.run_fetch(downloader=downloader)
        self.assertFalse(res["ok"])
        self.assertEqual(res["rows"], [])
        self.assertIn("net down", res["error"])
        self.assertIsNone(res["fetched_at"])
        self.assertEqual(freshness_gate(res), "failed")

    def test_failed_download_never_reads_cache_or_writes_db(self):
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "x.sqlite3"
        initialize(db, symbols=["TEST"])

        def downloader(t):
            raise TimeoutError("slow")

        with mock.patch("sources.cache.load", side_effect=AssertionError("cache read")):
            res = self.run_fetch("TEST", downloader=downloader)
        out = ingest_history("TEST", res["rows"], price_field=res["price_field"],
                             source_status=res, db_path=db)
        self.assertEqual(out["status"], "failed")
        with session(db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0], 0)
        tmp.cleanup()

    def test_cache_write_failure_is_not_a_fresh_success(self):
        def writer(sym, payload, latency):
            raise OSError("disk full")

        res = self.run_fetch("TCS", downloader=dl(), cache_writer=writer)
        self.assertFalse(res["ok"])
        self.assertEqual(res["rows"], [])
        self.assertIn("disk full", res["error"])
        self.assertIn("cache write failed", res["error"])

    def test_cache_writer_gets_the_existing_payload_format(self):
        seen = {}
        rows_in = raw_rows()
        res = self.run_fetch("TCS", downloader=lambda t: (rows_in, raw_meta()),
                             cache_writer=lambda s, p, l: seen.update(sym=s, payload=p, latency=l))
        self.assertTrue(res["ok"])
        self.assertEqual(seen["sym"], "TCS")
        self.assertEqual(set(seen["payload"]), {"meta", "rows"})
        self.assertIs(seen["payload"]["rows"], rows_in)
        self.assertGreaterEqual(seen["latency"], 0)

    def test_default_cache_writer_uses_cache_save_with_yahoo_response_name(self):
        with mock.patch("sources.cache.save") as save:
            res = fetch_history("TCS", downloader=dl(), clock=self.CLOCK)
        self.assertTrue(res["ok"])
        args = save.call_args[0]
        self.assertEqual(args[0], "yahoo")
        self.assertEqual(args[1], "TCS")
        self.assertEqual(set(args[2]), {"meta", "rows"})
        self.assertEqual(args[4], "response")

    def test_missing_adj_close_fails_closed_even_though_meta_falls_back_to_close(self):
        res = self.run_fetch("ITC", downloader=dl(with_adj=False))
        self.assertTrue(res["ok"])
        self.assertIsNone(res["price_field"])
        self.assertTrue(all(r["adj_close"] is None for r in res["rows"]))
        self.assertEqual([r["close"] for r in res["rows"]], [100.0, 101.0, 102.0])   # display only

    def test_adj_close_column_present_but_all_nan_is_unavailable(self):
        rows = raw_rows()
        for r in rows:
            r["adj_close"] = None
        res = self.run_fetch("ITC", downloader=lambda t: (rows, raw_meta()))
        self.assertIsNone(res["price_field"])

    def test_empty_and_missing_volume_fail(self):
        self.assertFalse(self.run_fetch(downloader=lambda t: ([], raw_meta()))["ok"])
        self.assertFalse(self.run_fetch(downloader=lambda t: (None, None))["ok"])
        meta = raw_meta()
        meta["columns"] = ["Close", "Adj Close"]
        self.assertFalse(self.run_fetch(downloader=lambda t: (raw_rows(), meta))["ok"])

    def test_default_downloader_is_the_existing_yahoo_download_history(self):
        import pandas as pd
        seen = {}

        class Ticker:
            def __init__(self, t):
                seen["ticker"] = t

            def history(self, **kw):
                seen["kw"] = kw
                idx = pd.to_datetime(["2026-10-01", "2026-10-02"])
                return pd.DataFrame({"Open": [1.0, 2.0], "Close": [100.0, 101.0],
                                     "Adj Close": [90.0, 91.0], "Volume": [10, 20]}, index=idx)

        fake = types.SimpleNamespace(Ticker=Ticker, __version__="test")
        with mock.patch.dict(sys.modules, {"yfinance": fake}):
            res = fetch_history("dixon ", cache_writer=no_cache, clock=self.CLOCK)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(seen["ticker"], "DIXON.NS")
        self.assertIs(seen["kw"]["auto_adjust"], False)
        self.assertEqual(res["price_field"], "Adj Close")
        self.assertEqual([r["adj_close"] for r in res["rows"]], [90.0, 91.0])
        self.assertEqual(res["rows"][0]["date"], "2026-10-01")

    def test_end_to_end_with_mocked_download_and_a_failing_symbol(self):
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "e.sqlite3"
        rows = make_rows(25)
        rows[-1].update(adj_close=106.0, volume=2500.0)
        raw = [{"date": r["date"], "close": r["adj_close"], "adj_close": r["adj_close"],
                "volume": r["volume"]} for r in rows]
        meta = {"columns": ["Close", "Adj Close", "Volume"], "adjusted_price_field": "Adj Close"}

        def downloader(ticker):
            if ticker.startswith("BAD"):
                raise RuntimeError("yahoo exploded")
            return raw, meta

        def fetcher(sym):
            return fetch_history(sym, downloader=downloader, cache_writer=no_cache, clock=self.CLOCK)

        now = after_close(date.fromisoformat(rows[-1]["date"]))
        results = ingest_portfolio(["AAA", "BAD", "CCC"], fetcher, now=now, db_path=db)
        self.assertEqual([r["status"] for r in results], ["ok", "failed", "ok"])
        self.assertEqual(sorted(results[0]["alerts_created"]), ["price_up", "volume_spike"])
        self.assertIn("yahoo exploded", results[1]["error"])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
