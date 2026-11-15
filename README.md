\# FINN — Financial Investment Agent



FINN is a prototype financial research and monitoring agent for Indian equities, built for the ENAM AI Engineer Build Challenge.



\## Current Implementation



\### Portfolio Monitoring

\- Tracks 20 configured NSE-listed companies.

\- Retrieves daily historical price and volume data through Yahoo Finance.

\- Stores historical market data in a local SQLite database.

\- Evaluates completed trading sessions for price and unusual-volume alerts.

\- Persists alerts for later retrieval.



\### Alert Reliability

\- Uses adjusted closing prices when the actual adjusted-price field is available.

\- Compares volume against the preceding 20 valid sessions.

\- Tracks price and volume checks independently.

\- Deduplicates alerts by symbol, session date and alert type.

\- Avoids generating historical alert floods during initial seeding.

\- Isolates individual symbol failures.



\### Data Sources

\- Yahoo Finance: daily market data.

\- NSE: corporate announcement discovery.

\- Screener.in: annual report and concall link discovery.

\- Local JSON cache with cache-only and forced-failure modes.



\## Portfolio



RELIANCE, TCS, HDFCBANK, ICICIBANK, INFY, ITC, HINDUNILVR, BHARTIARTL, LT, SUNPHARMA, MARUTI, BAJFINANCE, ASIANPAINT, TATASTEEL, NTPC, DIXON, PERSISTENT, POLYCAB, PIDILITIND and APLAPOLLO.



\## Technology



\- Python

\- SQLite

\- httpx

\- yfinance

\- BeautifulSoup



\## Setup



From the repository root:



```powershell

py -3 -m venv .venv

.\\.venv\\Scripts\\python.exe -m pip install -r backend\\requirements.txt

```



\## Run



Open PowerShell in the `backend` directory.



Run ingestion:



```powershell

\& ..\\.venv\\Scripts\\python.exe run\_ingest.py

```



Inspect the database:



```powershell

\& ..\\.venv\\Scripts\\python.exe run\_ingest.py --inspect

```



Run tests:



```powershell

\& ..\\.venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v

```



\## Source Verification



Run from the `backend` directory:



```powershell

\& ..\\.venv\\Scripts\\python.exe -m probes.probe\_sources live

\& ..\\.venv\\Scripts\\python.exe -m probes.probe\_sources cache-only

\& ..\\.venv\\Scripts\\python.exe -m probes.probe\_sources force-fail

```



\## Local Verification Completed



\- 50 automated unit tests passed.

\- Initial ingestion succeeded for all 20 portfolio symbols.

\- Historical sessions were seeded into SQLite.

\- Database schema version 3 was verified.

\- Cache-only probes completed with zero network attempts and no cache rewrites.

\- Forced-failure probes verified stale-cache handling and safe failure for uncached sources.



These are local development results, not production deployment claims.



\## Milestone 3: Announcements Ingestion (offline-tested; live run pending)

- `sources/nse_records.py`: fresh raw NSE records, reusing `nse._download` and the existing cache payload shape.
- `announcements.py`: normalisation, deterministic dedupe key (`seq_id`, else SHA-256 fallback), SQLite persistence.
- `materiality.py`: transparent keyword rules (high/medium/low + category + matched rule). This is keyword matching, not an LLM and not a full materiality judgement.
- `today.get_recent_announcements()`: read-only feed query.
- Schema v4 adds an `announcements` table; a clean v3 database migrates additively, anything else is refused untouched.
- Run: `python run_ingest.py --announcements` (live, from `backend`).
- Only verified live fetches are stored; cached or stale data is never ingested.

Not yet implemented: PDF download, results extraction, management biographies, Gemini, pull-mode research, frontend.

\## Current Limitations



This repository currently contains the backend prototype. A completed mobile frontend and deployed application are not included.



Remaining work includes:

- Improved announcement materiality classification and PDF extraction (live NSE ingestion is verified; see Milestone 3).

\- Financial results extraction (Revenue, EBITDA, PBT and PAT).

\- PDF downloading and text extraction.

\- Gemini Flash integration and citation verification.

\- On-demand company research.

\- A complete mobile-first interface.



The project prioritises reliable data ingestion and alert generation before adding AI-generated analysis.



\## Data and Privacy



Local databases, downloaded cache data, virtual environments and environment files are excluded from Git. API keys and credentials must never be committed.

