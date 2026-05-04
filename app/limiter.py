"""Tiny in-memory sliding-window rate limiter, keyed by client IP."""

import threading
import time
from collections import deque

from fastapi import HTTPException, Request


class RateLimiter:
    """Sliding-window counter, keyed by (usually) client IP.

    Memory hygiene:

    - `check()` does lazy GC: if the key's deque ages down to empty, the
      entry is removed from the dict. Keeps a key from persisting after
      its window lapses when the same caller comes back later.
    - `sweep()` does periodic GC: walks every key, drops entries whose
      deques are fully aged out. Called from cleanup.run_once so an
      IP that hits once and never returns doesn't occupy a dict slot
      forever. The lazy path alone can't close that case — nothing
      triggers a re-read of a key that's never queried again.

    Together the two paths bound dict growth under both normal traffic
    (same IPs returning) and adversarial IP-rotation.
    """

    def __init__(self, max_hits: int, window_seconds: int):
        self.max_hits = max_hits
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is not None:
                while q and now - q[0] > self.window:
                    q.popleft()
                if not q:
                    # Bucket aged to empty -- drop the entry rather than
                    # keep an empty deque around. Re-created below if the
                    # caller is about to register a fresh hit.
                    del self._hits[key]
                    q = None

            hits = 0 if q is None else len(q)
            if hits >= self.max_hits:
                raise HTTPException(status_code=429, detail="rate limited")

            if q is None:
                q = deque()
                self._hits[key] = q
            q.append(now)

    def sweep(self) -> int:
        """Drop any keys whose deques are fully aged out. Returns the
        number of keys evicted. Called periodically from cleanup.run_once
        so rotating-IP traffic can't grow the dict without bound."""
        now = time.monotonic()
        evicted = 0
        with self._lock:
            # Snapshot so we can mutate during iteration.
            for key in list(self._hits):
                q = self._hits[key]
                while q and now - q[0] > self.window:
                    q.popleft()
                if not q:
                    del self._hits[key]
                    evicted += 1
        return evicted

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


reveal_limiter = RateLimiter(max_hits=10, window_seconds=60)
login_limiter = RateLimiter(max_hits=10, window_seconds=60)
create_limiter = RateLimiter(max_hits=60, window_seconds=3600)
# Applied to reads that aren't covered by the more specific limiters above
# (/api/me, /api/secrets/tracked, /api/secrets/{sid}/status, /s/{token}/meta).
# Generous budget: a browsing user hits /status once per poll cycle and the
# rest once per page load; 300 req/min leaves plenty of headroom while
# still capping `meta`-spam style DoS from a single IP.
read_limiter = RateLimiter(max_hits=300, window_seconds=60)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def reveal_rate_limit(request: Request) -> None:
    reveal_limiter.check(_client_ip(request))


def login_rate_limit(request: Request) -> None:
    login_limiter.check(_client_ip(request))


def create_rate_limit(request: Request) -> None:
    """Per-credential rate limit on secret creation. Three keying shapes
    in priority order, all stringified to a stable namespace so the
    limiter's dict key is always `str` and the prefixes can't collide:

      bearer:<token_id>          -- the caller authenticated via a
                                    valid `Authorization: Bearer ...`
                                    token. Each token gets its own
                                    bucket regardless of source IP, so
                                    two tokens from the same office NAT
                                    don't compete for one shared budget.
      session:<user_id>:<gen>    -- the caller has a valid session cookie.
                                    Two browsers logged in as the same
                                    user share the bucket; rotating
                                    credentials advances <gen> and so
                                    starts a fresh bucket.
      <client_ip>                -- unauthenticated fallback. Authed
                                    routes refuse the request after
                                    this dep runs, but the IP key keeps
                                    the limiter safe even if a future
                                    route mounts this dep without an
                                    auth dep behind it.

    Bearer keying takes precedence over session keying so a CI script
    that happens to also send a session cookie (unusual but possible
    under shared-laptop dev flows) is still bucketed by the credential
    it actively presents."""
    from .config import get_settings
    from .dependencies import read_session_cookie, resolve_bearer_token

    bearer_row = resolve_bearer_token(request)
    if bearer_row is not None:
        identity = f"bearer:{bearer_row['id']}"
    else:
        raw = request.cookies.get(get_settings().session_cookie_name)
        key = read_session_cookie(raw) if raw else None
        identity = f"session:{key[0]}:{key[1]}" if key else _client_ip(request)
    create_limiter.check(identity)


def read_rate_limit(request: Request) -> None:
    read_limiter.check(_client_ip(request))
