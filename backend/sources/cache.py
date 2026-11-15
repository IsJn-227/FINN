"""Read and write successful source payloads under the gitignored data directory."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = REPO_ROOT / "data" / "cache"

SOURCES = ("yahoo", "nse", "screener")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def cache_path(source: str, symbol: str, name: str = "response") -> Path:
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")
    safe = symbol.strip().upper()
    if not safe.isalnum():
        raise ValueError(f"unexpected symbol: {symbol}")
    return CACHE_ROOT / source / f"{safe}.{name}.json"


def exists(source: str, symbol: str, name: str = "response") -> bool:
    return cache_path(source, symbol, name).is_file()


def save(source: str, symbol: str, payload: dict, request_latency_ms: float, name: str = "response") -> Path:
    path = cache_path(source, symbol, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "saved_at": utc_now(),
        "request_latency_ms": round(request_latency_ms, 1),
        "payload": payload,
    }
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def load(source: str, symbol: str, name: str = "response") -> dict | None:
    path = cache_path(source, symbol, name)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def mtime(source: str, symbol: str, name: str = "response") -> float | None:
    path = cache_path(source, symbol, name)
    if not path.is_file():
        return None
    return path.stat().st_mtime
