import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from announcements import ingest_announcements, ingest_symbol_announcements, normalize_record
from database import SCHEMA_VERSION, initialize, inspect_db, session
from materiality import classify
from sources.nse_records import fetch_records
from today import get_recent_announcements, get_today_alerts

FRESH = {"ok": True, "stale": False, "from_cache": False}


def rec(symbol="TCS", desc="Financial Results", when="2026-11-13 18:45:20", seq="100",
        link="https://nsearchives.nseindia.com/a.pdf", text=None):
    r = {"symbol": symbol, "desc": desc, "an_dt": when, "attchmntText": text}
    if seq is not None:
        r["seq_id"] = seq
    if link is not None:
        r["attchmntFile"] = link
    return r


def fresh(records):
    return {**FRESH, "records": records, "fetched_at": "2026-11-15T10:00:00+00:00", "error": None}


class Materiality(unittest.TestCase):
    def check(self, subject, rule, tier, text=None):
        got = classify(subject, text)
        self.assertEqual((got["matched_rule"], got["materiality"]), (rule, tier), subject)

    def test_tiers(self):
        self.check("Financial Results for Q2 FY27", "financial_results", "high")
        self.check("Resignation of Chief Financial Officer", "senior_management_change", "high")
        self.check("Appointment of CFO and Company Secretary", "senior_management_change", "high")
        self.check("Resignation of Statutory Auditors", "auditor_resignation", "high")
        self.check("Acquisition of stake in XYZ Ltd", "merger_acquisition", "high")
        self.check("Initiation of CIRP against subsidiary", "insolvency_default_fraud", "high")
        self.check("Credit Rating reaffirmed by CRISIL", "credit_rating", "medium")
        self.check("Declaration of dividend", "shareholder_returns", "medium")
        self.check("Outcome of Board Meeting", "board_meeting_outcome", "medium")
        self.check("Analysts/Institutional Investor Meet/Con. Call Updates", "investor_call", "medium")

    def test_routine_and_unknown_are_low(self):
        self.check("Resignation of Company Secretary", "routine_secretarial_role", "low")
        self.check("Closure of Trading Window", "routine_admin", "low")
        self.check("Allotment of ESOP shares", "routine_admin", "low")
        self.check("Updates", "default_unmatched", "low")

    def test_board_meeting_intimation_is_not_results(self):
        self.check("Board Meeting Intimation", "board_meeting_intimation", "medium",
                   "to consider financial results")

    def test_odd_input_never_raises(self):
        for s, d in ((None, None), ("", ""), (123, ["x"]), ("   ", None)):
            self.assertEqual(classify(s, d)["matched_rule"], "default_unmatched")

    def test_description_is_used(self):
        self.assertEqual(classify("Announcement under Regulation 30", "Acquisition of 51% stake")
                         ["materiality"], "high")


class Normalize(unittest.TestCase):
    def test_complete_record(self):
        row, reason = normalize_record("TCS", rec())
        self.assertIsNone(reason)
        self.assertEqual(row["source_id"], "seq:100")
        self.assertEqual(row["announced_at"], "2026-11-13T18:45:20+05:30")
        self.assertEqual(row["attachment_url"], "https://nsearchives.nseindia.com/a.pdf")
        self.assertEqual(row["materiality"], "high")

    def test_missing_attachment_is_allowed(self):
        row, reason = normalize_record("TCS", rec(link=None))
        self.assertIsNone(reason)
        self.assertIsNone(row["attachment_url"])

    def test_non_http_attachment_is_dropped_not_stored(self):
        row, _ = normalize_record("TCS", rec(link="javascript:alert(1)"))
        self.assertIsNone(row["attachment_url"])

    def test_alternate_date_format(self):
        row, _ = normalize_record("TCS", rec(when="13-Nov-2026 18:45:20"))
        self.assertEqual(row["announced_at"], "2026-11-13T18:45:20+05:30")

    def test_malformed_records_rejected_with_reason(self):
        cases = {"not_an_object": "junk", "missing_subject": {"symbol": "TCS", "an_dt": "2026-11-13 10:00:00"},
                 "missing_or_unparseable_date": rec(when="yesterday"),
                 "symbol_mismatch": rec(symbol="INFY")}
        for reason, record in cases.items():
            row, got = normalize_record("TCS", record)
            self.assertIsNone(row)
            self.assertEqual(got, reason)
        self.assertEqual(normalize_record("TCS", None)[1], "not_an_object")

    def test_fallback_key_is_deterministic_and_distinct(self):
        a, _ = normalize_record("TCS", rec(seq=None))
        b, _ = normalize_record("TCS", rec(seq=None))
        c, _ = normalize_record("TCS", rec(seq=None, desc="Different"))
        self.assertTrue(a["source_id"].startswith("sha256:"))
        self.assertEqual(a["source_id"], b["source_id"])
        self.assertNotEqual(a["source_id"], c["source_id"])

    def test_integer_seq_id(self):
        r = rec(seq=None)
        r["seq_id"] = 77
        self.assertEqual(normalize_record("TCS", r)[0]["source_id"], "seq:77")


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "a.sqlite3"
        initialize(self.db, symbols=["TCS", "INFY"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_then_repeat_creates_no_duplicates(self):
        records = [rec(seq="1"), rec(seq="2", desc="Dividend"), rec(seq="3", link=None, desc="Updates")]
        first = ingest_symbol_announcements("TCS", fresh(records), db_path=self.db)
        self.assertEqual((first["status"], first["inserted"], first["duplicates"]), ("ok", 3, 0))
        second = ingest_symbol_announcements("TCS", fresh(records), db_path=self.db)
        self.assertEqual((second["inserted"], second["duplicates"]), (0, 3))
        with session(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 3)

    def test_same_source_id_for_different_symbols_is_allowed(self):
        ingest_symbol_announcements("TCS", fresh([rec(seq="1")]), db_path=self.db)
        ingest_symbol_announcements("INFY", fresh([rec(symbol="INFY", seq="1")]), db_path=self.db)
        with session(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 2)

    def test_malformed_mixed_with_good_records(self):
        res = ingest_symbol_announcements("TCS", fresh([rec(seq="1"), "junk", rec(when="bad", seq="2")]),
                                          db_path=self.db)
        self.assertEqual((res["inserted"], res["malformed"]), (1, 2))
        self.assertEqual(res["malformed_reasons"], {"not_an_object": 1, "missing_or_unparseable_date": 1})

    def test_stale_cached_failed_and_unverified_write_nothing(self):
        bad = [{**fresh([rec()]), "stale": True}, {**fresh([rec()]), "from_cache": True},
               {**fresh([rec()]), "ok": False}, {"records": [rec()]}, None,
               {**fresh([rec()]), "from_cache": 0}, {**fresh([rec()]), "stale": None}]
        for result in bad:
            res = ingest_symbol_announcements("TCS", result, db_path=self.db)
            self.assertNotEqual(res["status"], "ok", result)
        with session(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 0)

    def test_records_not_a_list_fails(self):
        res = ingest_symbol_announcements("TCS", {**FRESH, "records": None}, db_path=self.db)
        self.assertEqual(res["status"], "failed")

    def test_one_failing_symbol_does_not_stop_others(self):
        def fetcher(sym):
            if sym == "BAD":
                raise RuntimeError("boom")
            return fresh([rec(symbol=sym, seq=f"{sym}1")])
        db = Path(self.tmp.name) / "p.sqlite3"
        res = ingest_announcements(["TCS", "BAD", "INFY"], fetcher, db_path=db)
        self.assertEqual([r["status"] for r in res], ["ok", "error", "ok"])
        self.assertIn("boom", res[1]["error"])

    def test_transaction_rolls_back_on_db_error(self):
        # a row violating NOT NULL mid-batch must not leave partial inserts for the symbol
        good = rec(seq="1")
        with mock.patch("announcements.classify", side_effect=[
                {"category": "x", "materiality": "low", "matched_rule": "r"},
                {"category": "x", "materiality": "INVALID", "matched_rule": "r"}]):
            with self.assertRaises(sqlite3.IntegrityError):
                ingest_symbol_announcements("TCS", fresh([good, rec(seq="2")]), db_path=self.db)
        with session(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 0)


class Feed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "f.sqlite3"
        initialize(self.db, symbols=["TCS", "INFY"])
        ingest_symbol_announcements("TCS", fresh([
            rec(seq="1", when="2026-11-10 10:00:00", desc="Financial Results"),
            rec(seq="2", when="2026-11-12 10:00:00", desc="Dividend"),
            rec(seq="3", when="2026-11-13 10:00:00", desc="Updates")]), db_path=self.db)
        ingest_symbol_announcements("INFY", fresh([rec(symbol="INFY", seq="9", when="2026-11-11 10:00:00",
                                                       desc="Closure of Trading Window")]), db_path=self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_newest_first_and_limit(self):
        rows = get_recent_announcements(2, db_path=self.db)
        self.assertEqual([r["subject"] for r in rows], ["Updates", "Dividend"])

    def test_min_materiality_and_symbol_filters(self):
        high = get_recent_announcements(10, min_materiality="high", db_path=self.db)
        self.assertEqual([r["subject"] for r in high], ["Financial Results"])
        med = get_recent_announcements(10, min_materiality="medium", db_path=self.db)
        self.assertEqual({r["materiality"] for r in med}, {"high", "medium"})
        self.assertEqual({r["symbol"] for r in get_recent_announcements(10, symbol=" infy ", db_path=self.db)},
                         {"INFY"})

    def test_feed_is_read_only(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        get_recent_announcements(10, db_path=self.db)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_missing_db_and_bad_arguments(self):
        missing = Path(self.tmp.name) / "none.sqlite3"
        self.assertEqual(get_recent_announcements(5, db_path=missing), [])
        self.assertFalse(missing.exists())
        for bad in (0, -1, True, "5"):
            with self.assertRaises(ValueError):
                get_recent_announcements(bad, db_path=self.db)
        with self.assertRaises(ValueError):
            get_recent_announcements(5, min_materiality="urgent", db_path=self.db)


class Migration(unittest.TestCase):
    V3 = """
    CREATE TABLE portfolio (symbol TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE thresholds (symbol TEXT PRIMARY KEY REFERENCES portfolio(symbol),
        price_pct REAL NOT NULL DEFAULT 5.0, volume_multiple REAL NOT NULL DEFAULT 2.0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE daily_bars (symbol TEXT NOT NULL, session_date TEXT NOT NULL, adjusted_close REAL,
        volume REAL, price_field TEXT NOT NULL, fetched_at TEXT, PRIMARY KEY (symbol, session_date));
    CREATE TABLE evaluated_sessions (symbol TEXT NOT NULL, session_date TEXT NOT NULL, mode TEXT NOT NULL,
        evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (symbol, session_date));
    CREATE TABLE session_checks (symbol TEXT NOT NULL, session_date TEXT NOT NULL, check_name TEXT NOT NULL,
        completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (symbol, session_date, check_name));
    CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, session_date TEXT NOT NULL,
        kind TEXT NOT NULL, observed REAL NOT NULL, threshold REAL NOT NULL, evidence TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (symbol, session_date, kind));
    INSERT INTO portfolio(symbol) VALUES ('TCS');
    INSERT INTO daily_bars VALUES ('TCS','2026-11-13',100.0,5.0,'Adj Close','t');
    INSERT INTO alerts(symbol,session_date,kind,observed,threshold,evidence)
        VALUES ('TCS','2026-11-13','price_up',6.0,5.0,'{}');
    PRAGMA user_version = 3;
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "m.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_v3_migrates_additively_and_keeps_data(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(self.V3)
        conn.commit()
        conn.close()
        initialize(self.db, symbols=["TCS"])
        info = inspect_db(self.db)
        self.assertEqual((info["user_version"], info["compatible"]), (SCHEMA_VERSION, True))
        with session(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0], 1)
        self.assertEqual(len(get_today_alerts(1, db_path=self.db)), 1)

    def test_modified_v3_is_refused_and_untouched(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(self.V3)
        conn.execute("ALTER TABLE daily_bars DROP COLUMN price_field")
        conn.commit()
        conn.close()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaises(RuntimeError):
            initialize(self.db, symbols=["TCS"])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_feed_on_unmigrated_v3_returns_empty_without_writing(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(self.V3)
        conn.commit()
        conn.close()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(get_recent_announcements(5, db_path=self.db), [])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)


class FetchRecords(unittest.TestCase):
    CLOCK = staticmethod(lambda: datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc))

    def run_fetch(self, downloader, writer=None, sym="tcs "):
        return fetch_records(sym, downloader=downloader, cache_writer=writer or (lambda *a: None),
                             clock=self.CLOCK, today=date(2026, 11, 15))

    def test_success_is_fresh_and_cache_payload_matches_nse_shape(self):
        seen = {}
        calls = []

        def dl(symbol, params):
            calls.append((symbol, params))
            return 200, json.dumps([rec()]), "application/json", False

        res = self.run_fetch(dl, lambda s, p, l: seen.update(sym=s, payload=p))
        self.assertTrue(res["ok"] is True and res["stale"] is False and res["from_cache"] is False)
        self.assertEqual(res["fetched_at"], "2026-11-15T10:00:00+00:00")
        self.assertEqual(calls[0][0], "TCS")
        self.assertEqual(calls[0][1]["to_date"], "15-11-2026")
        self.assertEqual(set(seen["payload"]), {"request", "http_status", "content_type",
                                                "cookie_warmup_used", "records"})
        self.assertEqual(seen["sym"], "TCS")

    def test_failures_are_not_fresh(self):
        cases = [lambda s, p: (500, "", "", False), lambda s, p: (200, "<html>", "text/html", False),
                 lambda s, p: (200, '{"a":1}', "application/json", False)]
        for dl in cases:
            res = self.run_fetch(dl)
            self.assertFalse(res["ok"])
            self.assertEqual(res["records"], [])

        def boom(s, p):
            raise ConnectionError("net down")
        res = self.run_fetch(boom)
        self.assertFalse(res["ok"])
        self.assertIn("net down", res["error"])

    def test_cache_write_failure_is_not_fresh(self):
        def writer(*a):
            raise OSError("disk full")
        res = self.run_fetch(lambda s, p: (200, "[]", "application/json", False), writer)
        self.assertFalse(res["ok"])
        self.assertIn("cache write failed", res["error"])

    def test_network_block_guard_is_respected(self):
        with mock.patch.dict("os.environ", {"FINN_BLOCK_NETWORK": "1"}):
            res = fetch_records("TCS", cache_writer=lambda *a: None, clock=self.CLOCK)
        self.assertFalse(res["ok"])
        self.assertIn("network blocked", res["error"])

    def test_end_to_end_with_mocked_download(self):
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "e.sqlite3"
        payloads = {"TCS": [rec(seq="1"), rec(seq="2", desc="Dividend")],
                    "INFY": [rec(symbol="INFY", seq="1", desc="Resignation of Chief Financial Officer")]}

        def dl(symbol, params):
            if symbol == "BAD":
                raise TimeoutError("slow")
            return 200, json.dumps(payloads[symbol]), "application/json", False

        results = ingest_announcements(
            ["TCS", "BAD", "INFY"],
            lambda s: fetch_records(s, downloader=dl, cache_writer=lambda *a: None, clock=self.CLOCK),
            db_path=db)
        self.assertEqual([r["status"] for r in results], ["ok", "failed", "ok"])
        feed = get_recent_announcements(10, min_materiality="high", db_path=db)
        self.assertEqual({r["symbol"] for r in feed}, {"TCS", "INFY"})
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
