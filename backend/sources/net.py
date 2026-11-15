"""Network attempt counter and the cache-mode block.

Live fetches call `note_live_attempt` before any socket work. Cache-only and
forced-failure runs set FINN_BLOCK_NETWORK=1, so an accidental live call fails
instead of reaching the network.
"""

from __future__ import annotations

import os

ATTEMPTS = 0


def reset_attempts() -> None:
    global ATTEMPTS
    ATTEMPTS = 0


def note_live_attempt(label: str) -> None:
    global ATTEMPTS
    ATTEMPTS += 1
    if os.environ.get("FINN_BLOCK_NETWORK") == "1":
        raise RuntimeError(f"network blocked before {label}")
