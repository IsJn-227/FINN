# FINN — Build Log

## Milestone 1: Source Probe

Status: Tests passed

### Objective
Verify Yahoo Finance, NSE announcements, and Screener sources for RELIANCE, DIXON, and PERSISTENT.

### Implementation
- Source clients created for Yahoo Finance, NSE, and Screener.
- Local response caching implemented.
- Cached fallback behavior implemented.
- Probe supports live, cache-only, and force-fail modes.
- PDF document response validation included.

### Tests performed

#### 1. Live probe
Command:
`C:\Users\isjn9\FINN\.venv\Scripts\python.exe -m probes.probe_sources live`

Results:
- Tested 3 companies across 3 sources.
- All 9 source checks returned successfully.
- Yahoo returned 127 daily sessions per company.
- NSE announcements were retrieved.
- Screener annual report and concall links were discovered.
- Sample annual report PDFs returned HTTP 200 and valid PDF signatures.

#### 2. Cache-only probe
Command:
`C:\Users\isjn9\FINN\.venv\Scripts\python.exe -m probes.probe_sources cache-only`

Results:
- All 9 source checks passed.
- All responses came from local cache.
- Network attempts: 0.
- Problems: 0.
- Cache files rewritten: none.

#### 3. Forced-failure probe
Command:
`C:\Users\isjn9\FINN\.venv\Scripts\python.exe -m probes.probe_sources force-fail`

Results:
- All 9 cached source fallbacks returned successfully.
- All cached fallbacks were marked stale=true.
- Uncached NOCACHE requests correctly returned errors.
- Network attempts: 0.
- Cache files rewritten: none.
- Problems: 0.

### Known limitations
- RELIANCE: 96 of 97 NSE announcements had document links.
- DIXON: 83 of 84 NSE announcements had document links.
- PERSISTENT: 150 of 150 NSE announcements had document links.
- Yahoo adjusted-close data was available for all three companies.
- RELIANCE Yahoo request showed relatively high latency during the live probe.
- Source coverage and response quality must be monitored as the pipeline expands.

### Architecture decisions
- Cached responses are returned when live requests fail.
- Cached fallback responses are explicitly marked stale.
- Missing cache data produces an error rather than fabricated fresh data.
- Local cache files are not rewritten during cache-only or forced-failure tests.

### Milestone 1 conclusion
Live, cache-only, and forced-failure probes completed.
Milestone 1 source verification passed for the three test companies.

## Milestone 2: Portfolio Monitoring and Alerts

Status: Locally verified

### Implementation
- Added a configurable 20-company portfolio.
- Added SQLite database schema version 3.
- Implemented daily market-data ingestion.
- Added persistent price and volume alert evaluation.
- Added independent session checks and alert deduplication.
- Added a read-only Today data interface.

### Verification
- Python compilation completed successfully.
- All 50 unittest tests passed.
- Initial live ingestion succeeded for all 20 portfolio symbols.
- 126 historical sessions were seeded per symbol.
- Price and volume checks completed for the latest eligible session.
- Cache-only and forced-failure source probes passed with zero network attempts.
- No cache files were rewritten during blocked-network tests.

### Current limitations
- Corporate announcement ingestion and materiality classification are not yet implemented.
- Financial results extraction, PDF text extraction, and Gemini integration remain future work.
- Mobile frontend and deployment remain future work.

### Milestone 2 conclusion
The portfolio monitoring and alert backend has been implemented and locally verified.

## Milestone 3: Corporate Announcements Ingestion

Status: Implemented, unit-tested on Windows, and verified against live NSE data.
### Implementation
- Added `sources/nse_records.py` (raw fresh NSE records; `sources/nse.py` unchanged).
- Added `announcements.py`, `materiality.py`; added `get_recent_announcements()` to `today.py`.
- Database schema v4: new `announcements` table, unique on (symbol, source, source_id). Additive v3 to v4 migration, refused if v3 is not exactly intact.
- `run_ingest.py --announcements` runs live announcement ingestion.
- Uses `ON CONFLICT DO NOTHING` rather than `INSERT OR IGNORE`, because OR IGNORE would also silently swallow CHECK/NOT NULL violations (found by a test).

### Verification
- 31 new announcement tests; full Windows regression suite: 81/81 passing.
- Live NSE announcement ingestion completed for all 20 portfolio symbols.
- SQLite contains 1,665 announcements: 161 high, 529 medium, 975 low.
- Repeat ingestion correctly identified existing records as duplicates.
- Database schema v4 is compatible; announcement feed is read-only.
### Known limitations
- Materiality is keyword rules only and will misclassify some announcements.
- Announcement times are assumed IST.
- No PDF download, results extraction, Gemini or pull-mode yet.
