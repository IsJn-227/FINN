"""screener.in document-link discovery.

The company page is only the index. This client records annual-report and
concall links from section#documents, then checks whether one of those links
returns a PDF. The PDF cache stores status, headers, and the first bytes,
which is the evidence the probe reports. Full reports are not downloaded.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from sources import cache
from sources.net import note_live_attempt

PAGE_CANDIDATES = (
    "https://www.screener.in/company/{symbol}/consolidated/",
    "https://www.screener.in/company/{symbol}/",
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
PDF_ATTEMPT_LIMIT = 3
PDF_PREFIX_BYTES = 1024


def fetch(symbol: str, mode: str) -> dict:
    page = cache.load("screener", symbol, "page")
    document = cache.load("screener", symbol, "document")
    if mode == "cache_only":
        return _from_cache(symbol, page, document, stale=False, missing="cache-only mode and no cached payload; no network request was made")
    if mode == "force_fail":
        return _from_cache(
            symbol,
            page,
            document,
            stale=True,
            missing="forced failure and no cached payload; fallback did not succeed",
        )
    if mode != "live":
        raise ValueError(mode)
    return _fetch_live(symbol)


def _fetch_live(symbol: str) -> dict:
    started = time.perf_counter()
    try:
        page_url, status, html, content_type = _download_page(symbol)
    except Exception as exc:
        return _miss(str(exc), latency_ms=(time.perf_counter() - started) * 1000)
    page_latency = (time.perf_counter() - started) * 1000
    if status != 200 or not html:
        return _miss(
            f"screener.in returned HTTP {status}",
            latency_ms=page_latency,
            http_status=status,
            content_type=content_type,
        )
    extracted = _extract_links(page_url, html)
    page_payload = {
        "page_url": page_url,
        "http_status": status,
        "content_type": content_type,
        "html": html,
        "document_headings": extracted["headings"],
        "annual_reports": extracted["annual_report"],
        "concalls": extracted["concall"],
    }
    cache.save("screener", symbol, page_payload, page_latency, "page")
    attempts = _check_documents(symbol, extracted["annual_report"] + extracted["concall"])
    document_latency = round(sum(item.get("request_latency_ms") or 0 for item in attempts), 1)
    if any(item.get("http_status") for item in attempts):
        cache.save("screener", symbol, {"attempts": attempts}, document_latency, "document")
    page_envelope = cache.load("screener", symbol, "page")
    document_envelope = cache.load("screener", symbol, "document")
    result = _present(symbol, page_envelope, document_envelope, from_cache=False, stale=False)
    if attempts and document_envelope is None:
        result["pdf_check"] = attempts[-1]
        result["pdf_attempts"] = len(attempts)
        result["pdf_returned"] = any(item.get("is_pdf") for item in attempts)
        result["document_cache_present"] = False
        result["document_error"] = "document request did not complete; response was not cached"
    return result


def _download_page(symbol: str) -> tuple[str, int, str, str]:
    timeout = httpx.Timeout(45.0)
    last_status = 0
    last_body = ""
    last_type = ""
    last_url = PAGE_CANDIDATES[0].format(symbol=symbol)
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        for template in PAGE_CANDIDATES:
            note_live_attempt(f"screener:{symbol}")
            url = template.format(symbol=symbol)
            response = client.get(url)
            last_status = response.status_code
            last_body = response.text
            last_type = response.headers.get("content-type", "")
            last_url = str(response.url)
            if response.status_code == 200 and response.text:
                return last_url, last_status, last_body, last_type
    return last_url, last_status, last_body, last_type


def _check_documents(symbol: str, links: list[dict]) -> list[dict]:
    attempts = []
    for link in links[:PDF_ATTEMPT_LIMIT]:
        note_live_attempt(f"screener-document:{symbol}")
        started = time.perf_counter()
        try:
            attempt = _read_prefix(link)
        except Exception as exc:
            attempt = {
                "source_title": link.get("title"),
                "requested_url": link.get("url"),
                "error": str(exc),
                "is_pdf": False,
                "http_status": None,
            }
        attempt["request_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        attempts.append(attempt)
        if attempt.get("is_pdf"):
            break
    return attempts


def _read_prefix(link: dict) -> dict:
    timeout = httpx.Timeout(45.0)
    headers = {**HEADERS, "Accept": "application/pdf,application/octet-stream,*/*;q=0.8"}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", link["url"]) as response:
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) >= PDF_PREFIX_BYTES:
                    break
            prefix = bytes(chunks[:PDF_PREFIX_BYTES])
            content_type = response.headers.get("content-type", "")
            magic = prefix.lstrip()[:5]
            return {
                "source_title": link.get("title"),
                "requested_url": link["url"],
                "final_url": str(response.url),
                "http_status": response.status_code,
                "content_type": content_type,
                "magic_ascii": magic.decode("ascii", errors="replace"),
                "bytes_read": len(prefix),
                "is_pdf": response.status_code == 200 and magic.startswith(b"%PDF"),
                "error": None,
            }


def _extract_links(page_url: str, html: str) -> dict[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one("section#documents") or soup.select_one("#documents")
    found = {"annual_report": [], "concall": [], "headings": []}
    if section is None:
        return found
    found["headings"] = [node.get_text(" ", strip=True) for node in section.find_all(["h2", "h3", "h4"])]
    bucket = None
    for node in section.find_all(["h2", "h3", "h4", "a"]):
        if node.name in {"h2", "h3", "h4"}:
            bucket = _bucket(node.get_text(" ", strip=True))
            continue
        if bucket not in found:
            continue
        href = node.get("href")
        if not href:
            continue
        title = node.get_text(" ", strip=True) or node.get("title") or ""
        found[bucket].append({"title": title, "url": urljoin(page_url, href)})
    return found


def _bucket(heading: str) -> str | None:
    text = heading.lower()
    if "annual" in text:
        return "annual_report"
    if "concall" in text or "conference call" in text or "earnings call" in text:
        return "concall"
    return None


def _from_cache(symbol: str, page: dict | None, document: dict | None, *, stale: bool, missing: str) -> dict:
    if page is None:
        return _miss(missing)
    if stale and document is None:
        result = _present(symbol, page, None, from_cache=True, stale=True)
        result["document_error"] = missing
        return result
    return _present(symbol, page, document, from_cache=True, stale=stale)


def _present(symbol: str, page: dict, document: dict | None, *, from_cache: bool, stale: bool) -> dict:
    payload = page["payload"]
    annual = payload.get("annual_reports") or []
    concalls = payload.get("concalls") or []
    attempts = []
    if document is not None:
        attempts = document["payload"].get("attempts") or []
    pdf_hit = next((item for item in attempts if item.get("is_pdf")), None)
    checked = pdf_hit or (attempts[-1] if attempts else None)
    return {
        "source": "screener",
        "symbol": symbol,
        "ok": True,
        "stale": stale,
        "from_cache": from_cache,
        "fetched_at": page["saved_at"],
        "served_at": cache.utc_now(),
        "request_latency_ms": page["request_latency_ms"],
        "error": None,
        "page_url": payload.get("page_url"),
        "http_status": payload.get("http_status"),
        "content_type": payload.get("content_type"),
        "annual_report_discoverable": len(annual) > 0,
        "concall_discoverable": len(concalls) > 0,
        "annual_report_link_count": len(annual),
        "concall_link_count": len(concalls),
        "document_headings": payload.get("document_headings") or [],
        "annual_report_sample": annual[0] if annual else None,
        "concall_sample": concalls[0] if concalls else None,
        "pdf_returned": bool(pdf_hit),
        "pdf_check": checked,
        "pdf_attempts": len(attempts),
        "document_cache_present": document is not None,
        "document_request_latency_ms": None if document is None else document.get("request_latency_ms"),
    }


def _miss(message: str, latency_ms: float | None = None, http_status: int | None = None, content_type: str | None = None) -> dict:
    return {
        "source": "screener",
        "ok": False,
        "stale": False,
        "from_cache": False,
        "fetched_at": None,
        "served_at": cache.utc_now(),
        "request_latency_ms": None if latency_ms is None else round(latency_ms, 1),
        "error": message,
        "http_status": http_status,
        "content_type": content_type,
        "annual_report_discoverable": False,
        "concall_discoverable": False,
        "pdf_returned": False,
        "pdf_check": None,
    }
