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


def _build_pwa_manifest(settings) -> dict:
    """PWA manifest, with deployment-label-aware name and icon list.

    Empty `deployment_label` is the prod posture: name="ephemera" and
    a visually-light tile (light-bg / dark-glyph). Any non-empty label
    suffixes the name ("ephemera-{label}") and flips the tile to
    visually-dark (dark-bg / light-glyph) so a dev / staging install
    on the same phone is at-a-glance distinguishable from prod.

    File-naming reminder: in app/static/icons/ the suffixes describe
    the OS theme the variant is *for*, not how the variant looks.
    `icon-*-light-*` is the dark-bg/light-glyph asset (intended for a
    light OS); `icon-*-dark-*` is the light-bg/dark-glyph asset
    (intended for a dark OS). Each posture pins ONE colourway so the
    captured tile is consistent across OS themes -- the captured icon
    doesn't auto-flip with the OS later, so listing both colourways
    would leave install-time visual identity up to the browser's
    chosen heuristic, which is exactly what we don't want for
    distinguishability between dev and prod.
    """
    suffix = f"-{settings.deployment_label}" if settings.deployment_label else ""
    name = f"ephemera{suffix}"
    # dev (any label) -> visually-dark tile via icon-*-light-* assets;
    # prod (no label) -> visually-light tile via icon-*-dark-* assets.
    variants = ("light",) if settings.deployment_label else ("dark",)
    icons = [
        {
            "src": f"/static/icons/icon-{purpose}-{variant}-{size}.png",
            "sizes": f"{size}x{size}",
            "type": "image/png",
            "purpose": purpose,
        }
        for purpose in ("any", "maskable")
        for variant in variants
        for size in (192, 512)
    ]
    return {
        "name": name,
        "short_name": name,
        "lang": "en",
        "dir": "ltr",
        "id": "/",
        "start_url": "/send?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "theme_color": "#09090b",
        "background_color": "#fafafa",
        "categories": ["productivity", "security"],
        "icons": icons,
    }


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

    # ---- PWA manifest ----------------------------------------------------
    # Served as a route rather than a static file so the operator can flip
    # name + icon variant per environment via EPHEMERA_DEPLOYMENT_LABEL
    # (see app/config.py and app/__init__.py:_build_pwa_manifest). Sets
    # the spec'd application/manifest+json MIME explicitly -- browsers
    # reject manifests served as application/octet-stream.
    #
    # Two paths bind to the same handler. /manifest.webmanifest is the
    # canonical URL (what the layout's <link rel="manifest"> points at
    # going forward). /static/manifest.webmanifest is a legacy alias for
    # already-installed PWAs whose browsers captured the old static-file
    # URL before this change -- letting that path 404 would silently
    # block manifest updates from propagating to those installs (Chrome
    # may stop polling the manifest after a 404 and serve cached
    # name/icon/start_url forever). The static mount underneath is
    # registered AFTER these routes, so the explicit handler wins for
    # this specific path while every other /static/* request falls
    # through to StaticFiles as usual.

    def _manifest_response():
        return JSONResponse(
            _build_pwa_manifest(get_settings()),
            media_type="application/manifest+json",
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return _manifest_response()

    @app.get("/static/manifest.webmanifest", include_in_schema=False)
    def manifest_legacy_path():
        return _manifest_response()

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
