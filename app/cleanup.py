"""Background cleanup: purge expired secrets, old tracked metadata,
and aged-out rate-limiter buckets."""

import asyncio
import logging

from . import limiter, models
from .config import get_settings

log = logging.getLogger("ephemera.cleanup")


def run_once() -> tuple[int, int, int]:
    settings = get_settings()
    expired = models.purge_expired()
    tracked = models.purge_tracked_metadata(settings.tracked_retention_seconds)
    # Drop aged-out keys across all limiter instances. Bounds memory
    # growth under sustained IP-rotation traffic where each key hits
    # the limiter once and never returns (the in-check lazy GC handles
    # returning IPs; this handles the never-returning case).
    evicted = sum(
        lim.sweep()
        for lim in (
            limiter.reveal_limiter,
            limiter.login_limiter,
            limiter.create_limiter,
            limiter.read_limiter,
        )
    )
    if expired or tracked or evicted:
        log.info(
            "cleanup purged expired=%d tracked=%d limiter-evicted=%d",
            expired,
            tracked,
            evicted,
        )
    return expired, tracked, evicted


async def cleanup_loop():
    settings = get_settings()
    while True:
        try:
            run_once()
        except Exception:  # pragma: no cover
            log.exception("cleanup iteration failed")
        await asyncio.sleep(settings.cleanup_interval_seconds)
