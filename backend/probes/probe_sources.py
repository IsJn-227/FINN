"""Probe Yahoo Finance, NSE announcements, and screener.in.

Modes:
  live         real requests, cache successful responses
  cache-only   read the cache, and fail if any network call is attempted
  force-fail   do not use the network; return a cached payload with stale=true

Run from the backend directory:
  py -m probes.probe_sources live
  py -m probes.probe_sources cache-only
  py -m probes.probe_sources force-fail
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from sources import cache, net
from sources.nse import fetch as fetch_nse
from sources.screener import fetch as fetch_screener
from sources.yahoo import fetch as fetch_yahoo

SYMBOLS = ("RELIANCE", "DIXON", "PERSISTENT")
UNCACHED_SYMBOL = "NOCACHE"
REPORT_DIR = cache.REPO_ROOT / "data" / "probe-runs"
CACHE_NAMES = {
    "yahoo": ("response",),
    "nse": ("response",),
    "screener": ("page", "document"),
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"live", "cache-only", "force-fail"}:
        print("usage: py -m probes.probe_sources live|cache-only|force-fail", file=sys.stderr)
        return 2
    mode = argv[1]
    internal = {"cache-only": "cache_only", "force-fail": "force_fail", "live": "live"}[mode]
    blocked = mode != "live"
    if blocked:
        os.environ["FINN_BLOCK_NETWORK"] = "1"
        install_socket_guard()
    else:
        os.environ.pop("FINN_BLOCK_NETWORK", None)
    net.reset_attempts()
    before = _mtimes()
    started = cache.utc_now()
    report = {
        "mode": mode,
        "started_at": started,
        "symbols": {},
        "uncached_symbol": None,
    }
    for symbol in SYMBOLS:
        report["symbols"][symbol] = {
            "yahoo": fetch_yahoo(symbol, internal),
            "nse": fetch_nse(symbol, internal),
            "screener": fetch_screener(symbol, internal),
        }
    if mode == "force-fail":
        report["uncached_symbol"] = {
            "symbol": UNCACHED_SYMBOL,
            "yahoo": fetch_yahoo(UNCACHED_SYMBOL, internal),
            "nse": fetch_nse(UNCACHED_SYMBOL, internal),
            "screener": fetch_screener(UNCACHED_SYMBOL, internal),
        }
    after = _mtimes()
    report["finished_at"] = cache.utc_now()
    report["network_attempts"] = net.ATTEMPTS
    report["cache_files_rewritten"] = _rewritten(before, after) if blocked else []
    problems = _problems(mode, report)
    report["problems"] = problems
    _write(mode, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if problems else 0


def install_socket_guard() -> None:
    def blocked_connect(self, address):  # noqa: ANN001
        raise RuntimeError(f"network blocked: {address}")

    def blocked_create_connection(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError(f"network blocked: {args!r}")

    socket.socket.connect = blocked_connect  # type: ignore[method-assign]
    socket.create_connection = blocked_create_connection  # type: ignore[assignment]


def _mtimes() -> dict[str, float]:
    found = {}
    for symbol in SYMBOLS:
        for source, names in CACHE_NAMES.items():
            for name in names:
                stamp = cache.mtime(source, symbol, name)
                if stamp is not None:
                    found[str(cache.cache_path(source, symbol, name))] = stamp
    return found


def _rewritten(before: dict[str, float], after: dict[str, float]) -> list[str]:
    changed = []
    for path, stamp in after.items():
        if path not in before or before[path] != stamp:
            changed.append(path)
    return changed


def _problems(mode: str, report: dict) -> list[str]:
    problems = []
    if mode == "live":
        return problems
    if report["network_attempts"] != 0:
        problems.append(f"network_attempts was {report['network_attempts']}")
    if report["cache_files_rewritten"]:
        problems.append("cache files were rewritten while the network was blocked")
    for symbol, sources in report["symbols"].items():
        for source, result in sources.items():
            cached_names = [name for name in CACHE_NAMES[source] if cache.exists(source, symbol, name)]
            if cached_names and mode == "cache-only":
                if not result.get("from_cache") or result.get("stale") or not result.get("ok"):
                    problems.append(f"{symbol} {source} cache-only result was not a fresh cache read")
            if cached_names and mode == "force-fail":
                if not (result.get("ok") and result.get("stale") is True and result.get("from_cache") is True):
                    problems.append(f"{symbol} {source} force-fail did not return stale cache")
            if not cached_names and mode == "force-fail":
                if result.get("ok") or result.get("stale") or "fallback did not succeed" not in (result.get("error") or ""):
                    problems.append(f"{symbol} {source} missing cache was not reported as a failed fallback")
    if mode == "force-fail":
        for source, result in report["uncached_symbol"].items():
            if source == "symbol":
                continue
            if result.get("ok") or result.get("stale") or "fallback did not succeed" not in (result.get("error") or ""):
                problems.append(f"{UNCACHED_SYMBOL} {source} missing cache was not reported as a failed fallback")
    return problems


def _write(mode: str, report: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = report["started_at"].replace(":", "").replace("-", "")
    path = REPORT_DIR / f"{mode}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main(sys.argv))
