"""Locale resolution, catalog loading, and the `lazy_gettext` proxy.

Precedence for the request locale:
  1. ?lang=xx query param   (testing + shareable forced-locale links)
  2. ephemera_lang_v1 cookie (anonymous users; set by the picker widget)
  3. users.preferred_language (authenticated users)
  4. Accept-Language header
  5. DEFAULT

Unknown/unsupported tags fall through silently -- locale is advisory, so a
bad hint must never 400 a request.

BCP-47 (web wire format) vs POSIX (on-disk catalog directory): web side
speaks "pt-BR"/"zh-CN", gettext wants "pt_BR"/"zh_Hans". POSIX_MAP is the
one place the conversion happens -- don't do it inline anywhere else.

lazy_gettext resolves against a ContextVar the i18n middleware sets per
request, so module-level constants (validator messages, schema field
descriptions) evaluate in the right locale at str()-coerce time without
the caller having to thread a Request through.
"""
from __future__ import annotations

import contextvars
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from babel.support import LazyProxy, Translations
from fastapi import Request


SUPPORTED: tuple[str, ...] = ("en", "ja", "pt-BR", "es", "zh-CN", "zh-TW")
DEFAULT: str = "en"

# BCP-47 (web wire format) -> POSIX (on-disk catalog directory).
POSIX_MAP: dict[str, str] = {
    "en": "en",
    "ja": "ja",
    "pt-BR": "pt_BR",
    "es": "es",
    "zh-CN": "zh_Hans",
    "zh-TW": "zh_Hant",
}

# Set by the i18n middleware per request; read by lazy_gettext at str()-coerce
# time. Default covers direct-imports in tests, the admin CLI, and any other
# path where no middleware runs -- lazy strings render as English instead of
# blowing up on a missing ContextVar value.
current_locale: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ephemera_locale", default=DEFAULT
)

_TRANSLATIONS_DIR = Path(__file__).parent / "translations"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _validate(tag: Optional[str]) -> Optional[str]:
    """Return the canonical SUPPORTED form of a candidate tag (case-
    insensitive), or None if it isn't supported."""
    if not tag:
        return None
    lowered = {s.lower(): s for s in SUPPORTED}
    return lowered.get(tag.lower())


def negotiate(accept_language: Optional[str]) -> str:
    """Best-match a SUPPORTED locale from an Accept-Language header. Scans
    entries in order (browsers emit the preferred locale first) and returns
    the first exact-or-primary-subtag hit. No q-value weighting -- not worth
    the complexity for a six-locale list."""
    if not accept_language:
        return DEFAULT
    lowered = {s.lower(): s for s in SUPPORTED}
    for part in accept_language.split(","):
        tag = part.split(";", 1)[0].strip()
        if not tag:
            continue
        hit = lowered.get(tag.lower())
        if hit:
            return hit
        primary = tag.split("-", 1)[0].lower()
        if primary in lowered:
            return lowered[primary]
    return DEFAULT


def resolve_locale(request: Request) -> str:
    """Full precedence walk. Called once per request by the i18n middleware;
    handlers normally read the cached result from request.state.locale rather
    than calling this function a second time."""
    # Lazy imports to keep app.i18n importable from low layers without
    # dragging in the whole dependency graph at import time.
    from .dependencies import current_user_id
    from .models import users as users_model

    lang = _validate(request.query_params.get("lang"))
    if lang:
        return lang

    lang = _validate(request.cookies.get("ephemera_lang_v1"))
    if lang:
        return lang

    uid = current_user_id(request)
    if uid is not None:
        user = users_model.get_user_by_id(uid)
        if user:
            lang = _validate(user.get("preferred_language"))
            if lang:
                return lang

    return negotiate(request.headers.get("accept-language"))


def get_locale(request: Request) -> str:
    """FastAPI dependency. Returns the locale the middleware stashed on
    request.state, or resolves from scratch when the middleware hasn't run
    (unit tests that bypass the app stack, and only those)."""
    cached = getattr(request.state, "locale", None)
    if cached:
        return cached
    return resolve_locale(request)


# ---------------------------------------------------------------------------
# Translation catalog
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _translations_for(posix: str) -> Translations:
    """Load the gettext catalog for a POSIX locale. Cached forever -- messages
    don't change at runtime. A missing .mo yields Babel's null Translations,
    so untranslated msgids render as themselves instead of 500'ing."""
    return Translations.load(
        dirname=str(_TRANSLATIONS_DIR),
        locales=[posix],
        domain="messages",
    )


def gettext_for(locale: str) -> Callable[[str], str]:
    """Return a gettext callable bound to an explicit locale. Pass into
    Jinja2 template contexts as `_`, or bind locally in route handlers that
    raise translated HTTPExceptions."""
    posix = POSIX_MAP.get(locale, POSIX_MAP[DEFAULT])
    return _translations_for(posix).gettext


def _resolve_lazy(message: str) -> str:
    return gettext_for(current_locale.get())(message)


def lazy_gettext(message: str) -> LazyProxy:
    """Late-binding translation wrapper for module-level constants (validator
    messages, schema field descriptions). The returned proxy re-resolves on
    every str()-coerce against the per-request locale in `current_locale`,
    so a single import-time `_("foo")` still renders per-request."""
    return LazyProxy(_resolve_lazy, message, enable_cache=False)


def template_context(request: Request) -> dict:
    """Base context dict for every Jinja2 TemplateResponse. Provides
    `request` (required by FastAPI's Jinja2 integration), the current
    `locale` (for the <html lang=""> attribute and conditional rendering),
    and `_` (a gettext callable bound to the request's locale so
    `{{ _("...") }}` in templates resolves correctly). Route handlers
    merge any page-specific keys on top of this."""
    locale = getattr(request.state, "locale", DEFAULT)
    return {
        "request": request,
        "locale": locale,
        "_": gettext_for(locale),
    }


__all__ = [
    "SUPPORTED",
    "DEFAULT",
    "POSIX_MAP",
    "current_locale",
    "negotiate",
    "resolve_locale",
    "get_locale",
    "gettext_for",
    "lazy_gettext",
    "template_context",
]
