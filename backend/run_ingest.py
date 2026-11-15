"""FINN ingestion entry point. Run from the backend directory.

  python run_ingest.py --inspect          # read-only: describe the database, write nothing
  python run_ingest.py                    # ingest PORTFOLIO live
  python run_ingest.py --include-extra    # also the research symbols
"""
import argparse
import json

from database import DB_PATH, inspect_db
from ingest import ingest_portfolio
from portfolio import EXTRA_RESEARCH, PORTFOLIO
from today import get_today_alerts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--include-extra", action="store_true")
    parser.add_argument("--announcements", action="store_true",
                        help="ingest NSE corporate announcements live instead of prices")
    args = parser.parse_args()

    print("DB:", DB_PATH)
    state = inspect_db(DB_PATH)
    print(json.dumps(state, indent=2))
    if args.inspect:
        return
    if state.get("exists") and not state.get("compatible") and state.get("user_version") != 3:
        raise SystemExit("Existing database has an incompatible schema (a clean v3 DB is migrated automatically). Not touching it. "
                         "Rename it yourself (Move-Item) and re-run.")

    if args.announcements:
        from announcements import ingest_announcements
        from sources.nse_records import fetch_records
        from today import get_recent_announcements
        syms = list(PORTFOLIO) + (list(EXTRA_RESEARCH) if args.include_extra else [])
        results = ingest_announcements(syms, fetch_records, pause=0.5)
        print("========== ANNOUNCEMENT INGESTION ==========")
        print(json.dumps(results, indent=2, default=str))
        print("\n========== LATEST HIGH/MEDIUM ANNOUNCEMENTS ==========")
        for a in get_recent_announcements(15, min_materiality="medium"):
            print(a["announced_at"], a["symbol"], a["materiality"], a["category"], "|", a["subject"])
        return

    from sources.yahoo_history import fetch_history      # imported late: network code
    symbols = list(PORTFOLIO) + (list(EXTRA_RESEARCH) if args.include_extra else [])
    results = ingest_portfolio(symbols, fetch_history, pause=0.5)
    print("========== INGESTION RESULTS ==========")
    print(json.dumps(results, indent=2, default=str))
    print("\n========== SAVED ALERTS (latest 5 sessions) ==========")
    print(json.dumps(get_today_alerts(5), indent=2, default=str))


if __name__ == "__main__":
    main()
