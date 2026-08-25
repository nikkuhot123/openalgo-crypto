"""Cross-process token bucket for per-account broker request quotas.

2026-08-25 incident. Flattrade caps requests at 120/minute PER USER. Steady
state was 23-75 strategy calls/min, but each fans out to one or more broker
calls -- `optionsymbol` re-fetches the underlying LTP internally on every call --
so peaks reached 133-136/min. Once over the cap the broker rejected everything,
including quotes needed to manage open positions.

The existing handling made it worse: on a rate-limit error it retried with
exponential backoff, spending MORE of an already-exhausted per-minute quota.
344 rate-limit errors were logged in 16 minutes. The `time.sleep` in that retry
path also parks a greenlet inside a SINGLE eventlet worker, so the request queue
grew until the API stopped answering new callers at all -- two live MIS
positions could not be exited and had to be closed by hand at the broker.

Retrying is the wrong shape for a quota. This throttles BEFORE the call so the
cap is never reached.

Why cross-process: the quota is per trading account, but callers live in
different processes (the gunicorn worker and the websocket_proxy subprocess). A
per-process limiter would let each spend the full budget. State therefore lives
in a small file guarded by an advisory lock.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:  # POSIX only; degrades to per-process on Windows dev boxes
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from utils.logging import get_logger

logger = get_logger(__name__)

# Headroom under the broker's real cap. The limiter cannot see calls made by
# anything outside this host (a mobile app, the broker's own web terminal), so
# spending the full 120 would still be brittle.
DEFAULT_LIMIT = int(os.getenv("BROKER_RATE_LIMIT_PER_MIN", "100"))
WINDOW_SECS = 60.0
# Longest a caller will wait for capacity. Beyond this the call is refused so a
# stuck request cannot pin a worker; the caller's own retry can decide.
MAX_WAIT_SECS = float(os.getenv("BROKER_RATE_LIMIT_MAX_WAIT", "8"))

_STATE_DIR = Path(os.getenv("BROKER_RATE_LIMIT_DIR", "log")) / "ratelimit"


class _Bucket:
    """Fixed-window counter. Coarser than a sliding window, but it matches how
    the broker actually counts ("N in a current minute"), and matching the
    server's accounting is what keeps us under it."""

    def __init__(self, name: str, limit: int = DEFAULT_LIMIT):
        self.limit = max(1, int(limit))
        self.path = _STATE_DIR / f"{name}.json"
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"rate-limit state dir unavailable ({e}); running unthrottled")
            self.path = None

    def _read(self, fh) -> tuple[float, int]:
        try:
            fh.seek(0)
            raw = fh.read()
            if not raw.strip():
                return 0.0, 0
            d = json.loads(raw)
            return float(d.get("window", 0.0)), int(d.get("count", 0))
        except (ValueError, TypeError):
            return 0.0, 0

    def _write(self, fh, window: float, count: int) -> None:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"window": window, "count": count}))
        fh.flush()
        # Deliberately NO os.fsync. This is a per-minute counter, not a ledger --
        # losing it to a power cut costs one minute of accounting. fsync is an
        # unpatched BLOCKING syscall under eventlet, so calling it on every
        # broker request would park the ONLY worker on disk I/O: the same
        # starvation this limiter exists to prevent.

    def acquire(self) -> bool:
        """Reserve one call. False if capacity did not free within MAX_WAIT_SECS."""
        if self.path is None:
            return True
        deadline = time.time() + MAX_WAIT_SECS
        while True:
            now = time.time()
            slot = now - (now % WINDOW_SECS)
            try:
                with open(self.path, "a+", encoding="utf-8") as fh:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    try:
                        window, count = self._read(fh)
                        if window != slot:  # new minute -> reset
                            window, count = slot, 0
                        if count < self.limit:
                            self._write(fh, window, count + 1)
                            return True
                        wait = (slot + WINDOW_SECS) - now
                    finally:
                        if fcntl is not None:
                            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError as e:
                logger.warning(f"rate-limit state unreadable ({e}); allowing call")
                return True

            if now >= deadline:
                logger.warning(
                    f"broker rate limit: {count}/{self.limit} used this minute and "
                    f"no capacity within {MAX_WAIT_SECS:.0f}s -- refusing the call "
                    f"rather than queueing it"
                )
                return False
            # Sleep in slices: under eventlet this yields the hub, so one
            # throttled caller never blocks the others.
            time.sleep(min(0.25, max(0.02, wait)))

    def penalise(self) -> None:
        """Burn the rest of the window after the broker reports a breach.

        The broker's counter and ours disagree at this point (it can see callers
        we cannot), so the only safe move is to stop spending until the window
        rolls. Retrying inside an exhausted window is what produced 344 errors.
        """
        if self.path is None:
            return
        now = time.time()
        slot = now - (now % WINDOW_SECS)
        try:
            with open(self.path, "a+", encoding="utf-8") as fh:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    self._write(fh, slot, self.limit)
                finally:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            logger.warning(
                f"broker reported a rate-limit breach -- window burned, no "
                f"further calls for {(slot + WINDOW_SECS) - now:.1f}s"
            )
        except OSError:
            pass


_buckets: dict[str, _Bucket] = {}


def bucket(broker: str, limit: int | None = None) -> _Bucket:
    b = _buckets.get(broker)
    if b is None:
        b = _Bucket(broker, DEFAULT_LIMIT if limit is None else limit)
        _buckets[broker] = b
    return b


def acquire(broker: str, limit: int | None = None) -> bool:
    return bucket(broker, limit).acquire()


def penalise(broker: str) -> None:
    bucket(broker).penalise()
