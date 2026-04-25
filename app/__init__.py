"""Ephemera FastAPI app factory."""

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cleanup, models
from .config import get_settings
from .dependencies import verify_api_token_or_session
from .i18n import current_locale, resolve_locale
from .routes import prefs, receiver, sender

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Single shared Jinja2 environment. The i18n extension enables {% trans %}
# blocks (we use {{ _("...") }} instead, but the extension also tells the
# pybabel extractor to treat the template as translation-aware). Route
# handlers build their context through app.i18n.template_context(request).
TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_DIR))
TEMPLATES.env.add_extension("jinja2.ext.i18n")

# CSP: deny-by-default then explicitly enumerate what ephemera actually uses.
# Nothing is fetched cross-origin (no CDN, no web fonts, no analytics). The
# two non-'self' sources are (1) `data:` images for the reveal payload and
# the inline SVG chevrons in style.css, and (2) base-uri/form-action pinned
# to 'self' to blunt <base href> and form-repoint attacks.
CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "manifest-src 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)

# Camera/mic/geo/payment/USB/sensors aren't used anywhere. An empty allow-list
# is the cheapest defence-in-depth against a future regression that quietly
# adds such an API.
PERMISSIONS_POLICY = ", ".join(
    [
        "camera=()",
        "microphone=()",
        "geolocation=()",
        "payment=()",
        "usb=()",
        "accelerometer=()",
        "gyroscope=()",
        "magnetometer=()",
        "interest-cohort=()",
    ]
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": PERMISSIONS_POLICY,
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

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        # Unconditional set -- the middleware is the authority on these
        # headers. Any future route that tried to set a conflicting value
        # (say, a looser CSP for a debug endpoint) gets silently
        # overwritten here rather than quietly winning. Enforces the
        # "deny by default, uniformly" stance at the structural level;
        # the cross-route invariant test pins it at the test level too.
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response

    @app.middleware("http")
    async def set_request_locale(request: Request, call_next):
        # Static assets never render localized content; skip the resolver
        # (which would otherwise do a cookie parse + DB lookup on every
        # image/css/js fetch) for the hot path.
        if request.url.path.startswith("/static"):
            return await call_next(request)
        locale = resolve_locale(request)
        request.state.locale = locale
        # ContextVar gives lazy_gettext a reliable per-request locale without
        # threading Request through every module-level string. reset() in
        # finally is mandatory -- a leaked token silently bleeds one
        # request's locale into the next handler on the same worker.
        token = current_locale.set(locale)
        try:
            return await call_next(request)
        finally:
            current_locale.reset(token)

    # ---- Auth-gated API docs ---------------------------------------------
    # Swagger UI assets live under app/static/swagger/ (pinned versions;
    # see app/static/swagger/README.md for the update recipe). init.js is
    # loaded as a separate <script src="..."> so the shell contains no
    # inline script blocks and the CSP stays at script-src 'self'. The
    # /openapi.json endpoint returns the same schema FastAPI would have
    # served by default, just wrapped in an auth check.

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

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json(_user=Depends(verify_api_token_or_session)):
        return JSONResponse(
            get_openapi(title=app.title, version=app.version, routes=app.routes),
        )

    @app.get("/docs", include_in_schema=False)
    def swagger_ui(_user=Depends(verify_api_token_or_session)):
        return HTMLResponse(
            """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>ephemera API</title>
  <link rel="icon" type="image/png" href="/static/swagger/favicon-32x32.png">
  <link rel="stylesheet" href="/static/swagger/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="/static/swagger/swagger-ui-bundle.js"></script>
  <script src="/static/swagger/init.js"></script>
</body>
</html>
"""
        )

    app.include_router(sender.router)
    app.include_router(receiver.router)
    app.include_router(prefs.router)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
