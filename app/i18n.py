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
speaks "pt-BR"/"zh-CN", gettext wants "pt_BR"/"zh_Hans". The conversion
happens once in `_bcp47_to_posix`; callers read the resolved dict in
`POSIX_MAP` and never inline the conversion.

SUPPORTED, POSIX_MAP, and LANGUAGE_LABELS are all derived from the
filesystem at import time by `_discover()` -- adding a new locale means
dropping its catalogs into app/static/i18n/ and app/translations/, not
editing three constants in lock-step. The only hand-maintained bits are
DEFAULT, _LAUNCH_OPT_OUT, and _LABEL_OVERRIDES below.

lazy_gettext resolves against a ContextVar the i18n middleware sets per
request, so module-level constants (validator messages, schema field
descriptions) evaluate in the right locale at str()-coerce time without
the caller having to thread a Request through.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Callable
from functools import cache, lru_cache
from pathlib import Path

from babel import Locale
from babel.core import UnknownLocaleError
from babel.support import LazyProxy, Translations
from fastapi import Request

DEFAULT: str = "en"

# Tags that have complete catalogs on disk but should stay SUPPORTED-only
# (reachable via ?lang=, cookie, preferred_language; invisible in the
# picker). Useful for staging a locale whose translations are still under
# review. Default: empty -- every discovered locale launches in the picker.
_LAUNCH_OPT_OUT: set[str] = set()

# Endonym overrides where Babel's CLDR default isn't what we want to ship.
# Most locales need no entry here. This table carries aesthetic refinements
# over CLDR's raw defaults: title-casing the Romance/Slavic endonyms
# (CLDR ships them lowercase per linguistic convention, but our picker's
# other entries are title-cased, so visual consistency wins) and the
# common-usage abbreviation for Chinese (simplified/traditional rather
# than CLDR's verbose "中文 (简体, 中国)" form). A new locale inherits the
# CLDR default; add an entry here only if the default renders wrong.
_LABEL_OVERRIDES: dict[str, str] = {
    "pt-BR": "Português (Brasil)",
    "es": "Español",
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
    "fr": "Français",
    "ru": "Русский",
}

_MODULE_DIR = Path(__file__).parent
_TRANSLATIONS_DIR = _MODULE_DIR / "translations"
_JS_CATALOG_DIR = _MODULE_DIR / "static" / "i18n"


# ---------------------------------------------------------------------------
# Locale discovery
# ---------------------------------------------------------------------------


def _bcp47_to_posix(tag: str) -> str:
    """Return the gettext on-disk dir name for a BCP-47 tag.

    Babel's Locale.parse resolves 'zh-CN' to a locale carrying both the
    territory (CN) and the script (Hans, via likely-subtag). Our gettext
    catalogs for Chinese are keyed on script only (zh_Hans, not
    zh_Hans_CN), so this drops the territory in that specific case.
    For everything else, Babel's str(loc) is the right POSIX name
    ('pt_BR', 'en', 'ja', etc.)."""
    loc = Locale.parse(tag.replace("-", "_"))
    if loc.language == "zh" and loc.script:
        return f"{loc.language}_{loc.script}"
    return str(loc)


def _label_for(tag: str) -> str:
    """Picker-visible endonym. Overrides win; CLDR default otherwise."""
    if tag in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[tag]
    loc = Locale.parse(tag.replace("-", "_"))
    return loc.get_display_name(locale=loc)


def direction_for(tag: str) -> str:
    """Return 'rtl' for right-to-left scripts (Arabic, Hebrew, Farsi, Urdu,
    etc.), 'ltr' for everything else. Sourced from CLDR via Babel -- no
    hand-maintained RTL tag list. Rendered into <html dir="..."> so the
    browser picks up direction and CSS logical properties flip the layout
    correctly.

    Falls back to 'ltr' on an unknown tag (same tolerance as resolve_locale:
    a bad tag is a UX degrade, not an error)."""
    try:
        return Locale.parse(tag.replace("-", "_")).text_direction
    except UnknownLocaleError:
        return "ltr"


@lru_cache(maxsize=1)
def _discover() -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
    """Walk app/static/i18n/*.json for candidate BCP-47 tags; for each,
    verify the matching gettext catalog dir exists. Return the triple
    (supported, posix_map, labels).

    English is the source of truth, so its JSON catalog is required but no
    .po is needed (the msgids in templates ARE the English source).

    Silent on half-shipped locales (JSON without .po or vice versa) -- the
    discovery just skips them. That lets a translator iterate on a locale
    before it becomes visible anywhere. Silent on tags Babel doesn't know
    (can't resolve a POSIX dir name) -- same rationale.

    Order: DEFAULT first, the rest alphabetical by BCP-47 tag. Keeps the
    picker's first option stable (English) and the remaining options
    predictably ordered."""
    tags: list[str] = []
    posix_map: dict[str, str] = {}
    labels: dict[str, str] = {}
    for json_path in sorted(_JS_CATALOG_DIR.glob("*.json")):
        tag = json_path.stem
        try:
            posix = _bcp47_to_posix(tag)
        except UnknownLocaleError:
            continue
        if tag != DEFAULT:
            po_path = _TRANSLATIONS_DIR / posix / "LC_MESSAGES" / "messages.po"
            if not po_path.exists():
                continue
        tags.append(tag)
        posix_map[tag] = posix
        labels[tag] = _label_for(tag)
    tags.sort(key=lambda t: (t != DEFAULT, t))
    return tuple(tags), posix_map, labels


SUPPORTED, POSIX_MAP, LANGUAGE_LABELS = _discover()

# Every discovered locale lands in the picker by default. Add a tag to
# _LAUNCH_OPT_OUT above to keep it SUPPORTED-only.
LAUNCHED: tuple[str, ...] = tuple(
    tag for tag in SUPPORTED if tag not in _LAUNCH_OPT_OUT
)


# Set by the i18n middleware per request; read by lazy_gettext at str()-coerce
# time. Default covers direct-imports in tests, the admin CLI, and any other
# path where no middleware runs -- lazy strings render as English instead of
# blowing up on a missing ContextVar value.
current_locale: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ephemera_locale", default=DEFAULT
)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _validate(tag: str | None) -> str | None:
    """Return the canonical SUPPORTED form of a candidate tag (case-
    insensitive), or None if it isn't supported."""
    if not tag:
        return None
    lowered = {s.lower(): s for s in SUPPORTED}
    return lowered.get(tag.lower())


_CHINESE_ROUTING: dict[str, str] = {
    # Simplified targets -> zh-CN. Singapore (SG) aligned to PRC Simplified
    # since 1976; bare `zh` defaults here because the Simplified speaker
    # base is ~30x larger and a bare `zh` header typically means a
    # misconfigured client rather than a specific signal.
    "zh": "zh-CN",
    "zh-sg": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-hans-cn": "zh-CN",
    "zh-hans-sg": "zh-CN",
    # Traditional targets -> zh-TW. Hong Kong (HK) and Macao (MO) use
    # traditional script; we serve them the Taiwan catalog rather than
    # maintaining a third variant. HK vocabulary differs slightly in the
    # domains Microsoft/Apple earn their third locale over (network,
    # software, printer), but ephemera's vocabulary (secret, passphrase,
    # expired, destroyed) reads identically in both registers -- "Taiwan-
    # flavored to a HK reader but not served wrong content." Revisit if
    # ephemera ever scales to end-users at HK/MO scale.
    "zh-hk": "zh-TW",
    "zh-mo": "zh-TW",
    "zh-hant": "zh-TW",
    "zh-hant-hk": "zh-TW",
    "zh-hant-mo": "zh-TW",
    "zh-hant-tw": "zh-TW",
}


def negotiate(accept_language: str | None) -> str:
    """Best-match a SUPPORTED locale from an Accept-Language header. Scans
    entries in order (browsers emit the preferred locale first) and returns
    the first exact-or-aliased-or-primary-subtag hit. No q-value weighting
    -- not worth the complexity for a ten-locale list.

    Chinese variant routing fires between exact-match and primary-subtag:
    `zh-SG` / `zh-Hans*` route to `zh-CN`; `zh-HK` / `zh-MO` / `zh-Hant*`
    route to `zh-TW`; bare `zh` routes to `zh-CN`. The alias only applies
    when the target catalog is actually in SUPPORTED -- if a future
    deployment drops zh-CN or zh-TW, the routing silently disables
    rather than returning a tag that doesn't resolve."""
    if not accept_language:
        return DEFAULT
    lowered = {s.lower(): s for s in SUPPORTED}
    for part in accept_language.split(","):
        tag = part.split(";", 1)[0].strip()
        if not tag:
            continue
        canonical = tag.lower()
        hit = lowered.get(canonical)
        if hit:
            return hit
        alias = _CHINESE_ROUTING.get(canonical)
        if alias and alias in SUPPORTED:
            return alias
        primary = canonical.split("-", 1)[0]
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


@cache
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


@cache
def js_catalog(locale: str) -> dict:
    """Load the JS-side JSON catalog for a locale, falling back to an empty
    dict when the file is missing or malformed. The English catalog is the
    source of truth for JS strings; every other locale is an overlay --
    the i18n.js shim falls back to English for any missing key, so a stub
    {} is a valid "not translated yet" state.

    Template authors should pull this in through template_context() rather
    than calling directly so the fallback (English) gets embedded alongside."""
    path = _JS_CATALOG_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def template_context(request: Request) -> dict:
    """Base context dict for every Jinja2 TemplateResponse. Provides
    `request` (required by FastAPI's Jinja2 integration), the current
    `locale` (for the <html lang=""> attribute and conditional rendering),
    `_` (a gettext callable bound to the request's locale so
    `{{ _("...") }}` in templates resolves correctly), and
    `is_authenticated` (exposed as a body data-attribute so client-side JS
    can decide whether to hit authed-only endpoints without a probe round-
    trip). Route handlers merge any page-specific keys on top of this.

    js_catalog and js_fallback are inlined into the page <head> so the
    shim resolves t() calls without a second fetch (skips the flash of
    untranslated strings that an async JSON fetch would create)."""
    # Lazy import mirrors resolve_locale() -- keeps app.i18n importable from
    # low layers without pulling the dependencies graph at import time.
    from .dependencies import current_user_id
    from .version import VERSION

    locale = getattr(request.state, "locale", DEFAULT)
    return {
        "request": request,
        "locale": locale,
        # Rendered into <html dir="..."> so CSS logical properties
        # (margin-inline-start, inset-inline-start, etc.) flip for RTL
        # scripts (ar/he/fa/ur). Sourced from CLDR via Babel; zero hand-
        # maintained RTL set.
        "dir": direction_for(locale),
        "_": gettext_for(locale),
        # `launched` drives the picker; `supported` is the resolution surface
        # (still queryable via ?lang=, cookie, DB pref). The picker hides
        # entirely at render time when launched has <2 members.
        "launched": LAUNCHED,
        "supported": SUPPORTED,
        "language_labels": LANGUAGE_LABELS,
        "js_catalog": js_catalog(locale),
        "js_fallback": js_catalog(DEFAULT),
        # Not an auth surface -- just a rendering hint for JS. The server's
        # real auth happens at endpoint-level dependencies. A forged
        # data-authenticated attribute gets a 401 on the next write call.
        "is_authenticated": current_user_id(request) is not None,
        # `git describe` at module-import time; rendered into the bottom-
        # center footer so operators can see what's deployed without
        # SSH'ing in. Tag name on a clean production deploy; tag-N-gsha
        # shape when drift is present.
        "version": VERSION,
    }


__all__ = [
    "SUPPORTED",
    "LAUNCHED",
    "DEFAULT",
    "POSIX_MAP",
    "LANGUAGE_LABELS",
    "current_locale",
    "direction_for",
    "negotiate",
    "resolve_locale",
    "get_locale",
    "gettext_for",
    "lazy_gettext",
    "template_context",
]
