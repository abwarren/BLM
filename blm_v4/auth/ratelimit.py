"""Login throttling — an in-memory sliding window, per identity and per IP.

Deliberately NOT persisted: a restart clearing the counters is harmless
(an attacker cannot trigger a restart) and it keeps the guard off the write
path.  The DB audit log in ``store.py`` remains the durable forensic trail.

Two independent windows apply, whichever trips first wins:

  * per-username  — stops password guessing against one account, and is
    normalised case-insensitively (``Admin`` and ``admin`` share a bucket).
  * per-IP        — stops spraying many usernames from one source.

Both are bounded: the map is swept on insert so a flood of distinct keys
cannot grow memory without limit.

All state is guarded by a lock — the API runs on a thread pool.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Window:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


@dataclass
class LimitDecision:
    allowed: bool
    retry_after_s: int = 0
    reason: str = ""


class LoginRateLimiter:
    """Sliding-window failure counter with a post-threshold lockout."""

    #: hard cap on tracked keys, so a flood cannot grow memory unbounded
    MAX_KEYS = 4096

    def __init__(self, *, max_attempts: int = 8, window_s: int = 300,
                 lockout_s: int = 300):
        self.max_attempts = max(1, int(max_attempts))
        self.window_s = max(1, int(window_s))
        self.lockout_s = max(1, int(lockout_s))
        self._lock = threading.Lock()
        self._by_user: dict[str, _Window] = {}
        self._by_ip: dict[str, _Window] = {}

    # ── internals ─────────────────────────────────────────────────
    def _sweep(self, now: float) -> None:
        if len(self._by_user) + len(self._by_ip) <= self.MAX_KEYS:
            return
        horizon = now - self.window_s
        for store in (self._by_user, self._by_ip):
            for key in list(store.keys()):
                w = store[key]
                if w.locked_until < now and not any(
                        t > horizon for t in w.failures):
                    store.pop(key, None)

    def _decide(self, w: _Window, now: float) -> LimitDecision:
        if w.locked_until > now:
            return LimitDecision(False, int(w.locked_until - now) + 1, "locked")
        horizon = now - self.window_s
        w.failures[:] = [t for t in w.failures if t > horizon]
        if len(w.failures) >= self.max_attempts:
            # window exhausted → lock for lockout_s and clear the window
            w.locked_until = now + self.lockout_s
            w.failures.clear()
            return LimitDecision(False, self.lockout_s, "window_exhausted")
        return LimitDecision(True)

    # ── public API ────────────────────────────────────────────────
    def check(self, username: str, ip: str) -> LimitDecision:
        """May this attempt proceed?  Does not record anything."""
        now = time.monotonic()
        ukey = (username or "").strip().lower()
        ikey = ip or "-"
        with self._lock:
            self._sweep(now)
            for store, key in ((self._by_user, ukey), (self._by_ip, ikey)):
                dec = self._decide(store.setdefault(key, _Window()), now)
                if not dec.allowed:
                    return dec
        return LimitDecision(True)

    def record_failure(self, username: str, ip: str) -> None:
        now = time.monotonic()
        ukey = (username or "").strip().lower()
        ikey = ip or "-"
        with self._lock:
            self._sweep(now)
            for store, key in ((self._by_user, ukey), (self._by_ip, ikey)):
                store.setdefault(key, _Window()).failures.append(now)

    def record_success(self, username: str, ip: str) -> None:
        """A successful login clears both buckets for that identity."""
        ukey = (username or "").strip().lower()
        ikey = ip or "-"
        with self._lock:
            self._by_user.pop(ukey, None)
            self._by_ip.pop(ikey, None)

    def reset(self) -> None:
        with self._lock:
            self._by_user.clear()
            self._by_ip.clear()
