"""Tiny in-memory sliding-window rate limiter, keyed by client IP."""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self, max_hits: int, window_seconds: int):
        self.max_hits = max_hits
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max_hits:
                raise HTTPException(status_code=429, detail="rate limited")
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


reveal_limiter = RateLimiter(max_hits=10, window_seconds=60)


def reveal_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    reveal_limiter.check(ip)
