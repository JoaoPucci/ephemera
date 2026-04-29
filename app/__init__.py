"""Ephemera FastAPI app factory.

Module surface kept tight: the lifespan, the create_app factory, and
the route + static mounts. Concerns broken out into siblings:

    app.security_headers  CSP / Permissions-Policy / HSTS constants +
                          the middleware that stamps them on every
                          response.
    app.i18n              `locale_middleware`, the per-request locale
                          resolver + ContextVar set/reset.
    app.templates/_docs.html  the Swagger UI HTML shell, served at /docs
                              behind verify_api_token_or_session.
"""

import asyncio
import mimetypes
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cleanup, models
from .config import get_settings
from .dependencies import verify_api_token_or_session
from .i18n import locale_middleware
from .routes import prefs, receiver, sender

# Re-export so existing `from app import SECURITY_HEADERS` callers (tests)
# keep working without churn. Authoritative definition lives in
# app.security_headers; this is just an alias.
from .security_headers import (  # noqa: F401  (public re-export)
    CSP,
    PERMISSIONS_POLICY,
    SECURITY_HEADERS,
    add_security_headers,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# StaticFiles delegates Content-Type lookup to Python's mimetypes module,
# which doesn't know about .webmanifest by default and falls back to
# application/octet-stream. Browsers reject manifests served under that
# MIME, so register the spec'd type before the static mount is built.
mimetypes.add_type("application/manifest+json", ".webmanifest")

# Single shared Jinja2 environment. The i18n extension enables {% trans %}
# blocks (we use {{ _("...") }} instead, but the extension also tells the
# pybabel extractor to treat the template as translation-aware). Route
# handlers build their context through app.i18n.template_context(request).
TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_DIR))
TEMPLATES.env.add_extension("jinja2.ext.i18n")


@asynccontextmanager
async def lifespan(app: FastAPI):
    models.init_db()
    task = asyncio.create_task(cleanup.cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    # docs_url / redoc_url / openapi_url all set to None so FastAPI serves
    # nothing at those paths by default. We re-mount /docs and /openapi.json
    # ourselves below, behind verify_api_token_or_session, so the API surface
    # remains browsable by operators while staying invisible to unauthenticated
    # probes. /redoc stays permanently off -- one UI surface is enough.
    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Middlewares: registration order is reverse-execution order. The
    # security-headers middleware is registered FIRST so it runs LAST
    # in the response phase -- it stamps the final response after every
    # other handler / middleware has finished, preventing any inner
    # layer from setting a conflicting header.
    app.middleware("http")(add_security_headers)
    app.middleware("http")(locale_middleware)

    # ---- Health probe -----------------------------------------------------
    # Unauthenticated, rate-limit-exempt liveness + readiness check. Touches
    # the DB with a no-op query and confirms the secret key is loaded. The
    # auto-deploy workflow polls this after `systemctl restart` to catch
    # regressions that would return 200 on /send while the app is actually
    # broken (DB unreachable, missing env, WAL permission flip). Returns
    # {"ok": true} 200 on success, {"ok": false, "reason": ...} 503 on any
    # failure. Excluded from the OpenAPI schema to match the audit posture
    # of /docs and /openapi.json (no unauth-visible surface advertisement).

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        settings = get_settings()
        if not settings.secret_key:
            return JSONResponse(
                {"ok": False, "reason": "missing_secret_key"}, status_code=503
            )
        try:
            models.ping()
        except Exception:
            return JSONResponse(
                {"ok": False, "reason": "db_unreachable"}, status_code=503
            )
        return JSONResponse({"ok": True}, status_code=200)

    # ---- Auth-gated API docs ---------------------------------------------
    # Swagger UI assets live under app/static/swagger/ (pinned versions;
    # see app/static/swagger/README.md for the update recipe). init.js is
    # loaded as a separate <script src="..."> so the shell contains no
    # inline script blocks and the CSP stays at script-src 'self'. The
    # /openapi.json endpoint returns the same schema FastAPI would have
    # served by default, just wrapped in an auth check.

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json(_user=Depends(verify_api_token_or_session)):
        return JSONResponse(
            get_openapi(title=app.title, version=app.version, routes=app.routes),
        )

    @app.get("/docs", include_in_schema=False)
    def swagger_ui(request: Request, _user=Depends(verify_api_token_or_session)):
        return TEMPLATES.TemplateResponse(request, "_docs.html")

    app.include_router(sender.router)
    app.include_router(receiver.router)
    app.include_router(prefs.router)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
