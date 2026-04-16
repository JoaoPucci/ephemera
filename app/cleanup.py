"""Background cleanup: purge expired secrets and old tracked metadata."""
import asyncio
import logging

from .config import get_settings
from . import models


log = logging.getLogger("ephemera.cleanup")


def run_once() -> tuple[int, int]:
    settings = get_settings()
    expired = models.purge_expired()
    tracked = models.purge_tracked_metadata(settings.tracked_retention_seconds)
    if expired or tracked:
        log.info("cleanup purged expired=%d tracked=%d", expired, tracked)
    return expired, tracked


async def cleanup_loop():
    settings = get_settings()
    while True:
        try:
            run_once()
        except Exception:  # pragma: no cover
            log.exception("cleanup iteration failed")
        await asyncio.sleep(settings.cleanup_interval_seconds)
