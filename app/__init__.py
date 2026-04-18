"""Ephemera FastAPI app factory."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from . import cleanup, models
from .routes import receiver, sender


STATIC_DIR = Path(__file__).resolve().parent / "static"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Start conservative -- HSTS is sticky, so if the cert ever breaks, browsers
    # that saw a long max-age will refuse HTTP for that long. Once the deployment
    # has been stable through at least one Let's Encrypt renewal, bump this to
    # `max-age=31536000; includeSubDomains; preload` and consider HSTS preload.
    "Strict-Transport-Security": "max-age=86400",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    models.init_db()
    task = asyncio.create_task(cleanup.cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    app.include_router(sender.router)
    app.include_router(receiver.router)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
