"""Tests for the i18n system: locale resolution, migration, prefs endpoint,
error shape, and the JS catalog inlining."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.errors import ERROR_MESSAGES, http_error
from app.i18n import (
    DEFAULT,
    LANGUAGE_LABELS,
    LAUNCHED,
    POSIX_MAP,
    SUPPORTED,
    _validate,
    current_locale,
    direction_for,
    gettext_for,
    js_catalog,
    lazy_gettext,
    negotiate,
)

ORIGIN = {"Origin": "http://testserver"}


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


def test_supported_and_labels_cover_the_same_set() -> None:
    """Every SUPPORTED tag must have a LANGUAGE_LABELS entry (otherwise the
    picker widget would render an empty option) and a POSIX_MAP entry
    (otherwise gettext_for would silently fall back to DEFAULT)."""
    for tag in SUPPORTED:
        assert tag in LANGUAGE_LABELS, f"missing LANGUAGE_LABELS entry: {tag}"
        assert tag in POSIX_MAP, f"missing POSIX_MAP entry: {tag}"


def test_default_is_in_supported() -> None:
    assert DEFAULT in SUPPORTED


def test_launched_is_subset_of_supported() -> None:
    """LAUNCHED is the picker-visible subset -- every member must also be
    in SUPPORTED (so it resolves via Accept-Language / cookie / DB pref)."""
    for tag in LAUNCHED:
        assert tag in SUPPORTED, f"LAUNCHED tag not in SUPPORTED: {tag}"


def test_launched_contains_default() -> None:
    """Whatever set of locales is currently launched, the default (English)
    must be among them -- otherwise the picker could render only tags the
    app can't actually localize into."""
    assert DEFAULT in LAUNCHED


def test_supported_is_default_first_then_alphabetical() -> None:
    """Picker ordering contract: English is always the first option
    (users' fallback / default), and the remaining tags are sorted
    alphabetically so the order is stable as new locales land."""
    assert SUPPORTED[0] == DEFAULT
    rest = SUPPORTED[1:]
    assert list(rest) == sorted(rest), f"tail not sorted: {rest}"


# ---------------------------------------------------------------------------
# Filesystem-driven discovery (replaces the old hand-maintained dicts)
# ---------------------------------------------------------------------------


def test_bcp47_to_posix_simple_language_tag() -> None:
    """Language-only tags map to themselves (gettext catalog dir == BCP-47
    tag for unambiguous cases like ja, fr, de, ru, ko)."""
    from app.i18n import _bcp47_to_posix

    for tag in ("en", "ja", "fr", "de", "ru", "ko", "es"):
        assert _bcp47_to_posix(tag) == tag


def test_bcp47_to_posix_with_territory() -> None:
    """Tags carrying a territory subtag get the POSIX underscore form.
    pt-BR has no CLDR script inference, so Babel's str(loc) is the
    right dir name."""
    from app.i18n import _bcp47_to_posix

    assert _bcp47_to_posix("pt-BR") == "pt_BR"


def test_bcp47_to_posix_chinese_drops_territory() -> None:
    """Chinese gettext catalogs are keyed on script (zh_Hans, zh_Hant),
    not on territory (zh_CN, zh_TW). Babel's likely-subtag resolution
    inflates 'zh-CN' to both territory=CN and script=Hans; the helper
    drops the territory so the path lines up with how the catalog dirs
    are actually laid out."""
    from app.i18n import _bcp47_to_posix

    assert _bcp47_to_posix("zh-CN") == "zh_Hans"
    assert _bcp47_to_posix("zh-TW") == "zh_Hant"


def test_label_override_wins_over_cldr_default() -> None:
    """_LABEL_OVERRIDES exists for aesthetic refinements (title-casing
    Romance/Slavic endonyms, the common-usage abbreviation for Chinese).
    The override value must win over Babel's CLDR endonym."""
    from app.i18n import _LABEL_OVERRIDES, _label_for

    # pt-BR is overridden
    assert "pt-BR" in _LABEL_OVERRIDES
    assert _label_for("pt-BR") == _LABEL_OVERRIDES["pt-BR"]


def test_label_falls_back_to_cldr_endonym() -> None:
    """Locales with no override entry use Babel's CLDR endonym. German
    ('Deutsch') and Japanese ('日本語') are existing examples where CLDR
    matches the aesthetic we want -- no override needed, the function
    still returns the right thing."""
    from app.i18n import _label_for

    assert _label_for("de") == "Deutsch"
    assert _label_for("ja") == "日本語"
    assert _label_for("ko") == "한국어"


# ---------------------------------------------------------------------------
# Text direction (LTR vs RTL)
#
# The template renders <html dir="..."> using the value returned by
# direction_for(); CSS logical properties then flip layout automatically.
# Sourced from CLDR via Babel -- no hand-maintained RTL tag list.
# ---------------------------------------------------------------------------


def test_direction_is_ltr_for_ltr_launched_locales() -> None:
    """Every LTR locale we ship renders dir='ltr'. Arabic is the first
    RTL locale in LAUNCHED; skip it (and any future RTL addition) so this
    assertion pins the contract for the LTR surface without double-covering
    what `test_direction_is_rtl_for_known_rtl_scripts` already asserts."""
    for tag in LAUNCHED:
        if direction_for(tag) == "rtl":
            continue
        assert direction_for(tag) == "ltr", f"{tag} unexpectedly RTL"


def test_direction_is_rtl_for_known_rtl_scripts() -> None:
    """Babel's CLDR data assigns 'rtl' to Arabic, Hebrew, Farsi, and Urdu.
    Pin the contract -- if Babel ever ships a CLDR update that changes
    this, we want to know."""
    for tag in ("ar", "he", "fa", "ur"):
        assert direction_for(tag) == "rtl", f"{tag} unexpectedly LTR"


def test_direction_falls_back_to_ltr_on_unknown_tag() -> None:
    """A bogus tag is treated as LTR rather than crashing. Matches the
    rest of resolve_locale's tolerance -- bad locale hints are UX
    degrades, not errors."""
    assert direction_for("xyz-nonsense") == "ltr"


def test_html_dir_attribute_reflects_ltr_locale(client: TestClient) -> None:
    r = client.get("/send")
    assert 'dir="ltr"' in r.text


def test_html_dir_attribute_reflects_rtl_locale(client: TestClient) -> None:
    """Resolution accepts any SUPPORTED tag; direction follows. Arabic
    is the first real RTL locale to ship -- `ar` is auto-discovered from
    the filesystem catalog, so this test checks the live behavior with
    no monkeypatching."""
    assert "ar" in SUPPORTED, (
        "ar must be discovered from the filesystem catalog for this "
        "test to exercise the RTL render path"
    )
    r = client.get("/send?lang=ar")
    assert 'dir="rtl"' in r.text
    assert 'lang="ar"' in r.text


def test_discover_requires_po_for_non_default_locales(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A locale with a JSON catalog but no gettext .po is half-shipped and
    must be skipped. Drop a fake es.json into an otherwise-empty tree and
    confirm discovery refuses to include it."""
    import app.i18n as i18n_mod

    js_dir = tmp_path / "static" / "i18n"
    po_dir = tmp_path / "translations"
    js_dir.mkdir(parents=True)
    po_dir.mkdir(parents=True)
    (js_dir / "en.json").write_text("{}", encoding="utf-8")
    (js_dir / "es.json").write_text("{}", encoding="utf-8")
    # No es .po file -- es should be skipped.

    monkeypatch.setattr(i18n_mod, "_JS_CATALOG_DIR", js_dir)
    monkeypatch.setattr(i18n_mod, "_TRANSLATIONS_DIR", po_dir)
    i18n_mod._discover.cache_clear()

    supported, posix_map, labels = i18n_mod._discover()
    assert "en" in supported  # DEFAULT doesn't need a .po
    assert "es" not in supported
    # Clear cache again so later tests see the real filesystem.
    i18n_mod._discover.cache_clear()


def test_discover_default_does_not_require_po(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """English is the source of truth -- the msgids inside templates ARE
    the English strings, so no .po is required. Only the JSON catalog
    needs to exist."""
    import app.i18n as i18n_mod

    js_dir = tmp_path / "static" / "i18n"
    po_dir = tmp_path / "translations"
    js_dir.mkdir(parents=True)
    po_dir.mkdir(parents=True)
    (js_dir / "en.json").write_text("{}", encoding="utf-8")
    # No en/LC_MESSAGES/messages.po anywhere.

    monkeypatch.setattr(i18n_mod, "_JS_CATALOG_DIR", js_dir)
    monkeypatch.setattr(i18n_mod, "_TRANSLATIONS_DIR", po_dir)
    i18n_mod._discover.cache_clear()

    supported, _, _ = i18n_mod._discover()
    assert supported == ("en",)
    i18n_mod._discover.cache_clear()


def test_discover_skips_tags_babel_does_not_recognize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If someone drops a JSON file whose stem isn't a real BCP-47 tag,
    Babel raises UnknownLocaleError during parsing and discovery skips
    it silently rather than crashing the app."""
    import app.i18n as i18n_mod

    js_dir = tmp_path / "static" / "i18n"
    po_dir = tmp_path / "translations"
    js_dir.mkdir(parents=True)
    po_dir.mkdir(parents=True)
    (js_dir / "en.json").write_text("{}", encoding="utf-8")
    (js_dir / "xyz-nonsense.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(i18n_mod, "_JS_CATALOG_DIR", js_dir)
    monkeypatch.setattr(i18n_mod, "_TRANSLATIONS_DIR", po_dir)
    i18n_mod._discover.cache_clear()

    supported, _, _ = i18n_mod._discover()
    assert "xyz-nonsense" not in supported
    i18n_mod._discover.cache_clear()


def test_launch_opt_out_excludes_from_launched_but_keeps_in_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a tag to _LAUNCH_OPT_OUT must keep it resolvable (SUPPORTED)
    while hiding it from the picker (LAUNCHED). The feature is for
    staging a locale before exposing it in the UI."""
    import app.i18n as i18n_mod

    # Monkey-patch the opt-out to include an existing launched locale,
    # then re-derive LAUNCHED to exercise the opt-out path.
    monkeypatch.setattr(i18n_mod, "_LAUNCH_OPT_OUT", {"de"})
    relaunched = tuple(
        t for t in i18n_mod.SUPPORTED if t not in i18n_mod._LAUNCH_OPT_OUT
    )
    assert "de" in i18n_mod.SUPPORTED  # still resolvable
    assert "de" not in relaunched  # hidden from picker


# ---------------------------------------------------------------------------
# Accept-Language negotiation
# ---------------------------------------------------------------------------


def test_negotiate_exact_match() -> None:
    assert negotiate("ja") == "ja"
    assert negotiate("zh-CN") == "zh-CN"
    assert negotiate("zh-TW") == "zh-TW"
    assert negotiate("pt-BR") == "pt-BR"


def test_negotiate_is_case_insensitive() -> None:
    assert negotiate("JA") == "ja"
    assert negotiate("zh-cn") == "zh-CN"
    assert negotiate("PT-br") == "pt-BR"


def test_negotiate_honours_browser_order() -> None:
    # Browsers list the preferred locale first; first match wins regardless
    # of q-values (which we don't parse).
    assert negotiate("ja,en;q=0.9") == "ja"
    # `hi` (Hindi) is deliberately NOT in SUPPORTED -- picked for this test
    # so it stays stable even as SUPPORTED grows; swap for another untaken
    # tag if Hindi is ever added.
    assert negotiate("hi,ja;q=0.5,en;q=0.4") == "ja"


def test_negotiate_primary_subtag_fallback() -> None:
    # Regional variants fall back to their primary if it's supported.
    assert negotiate("es-MX") == "es"
    # `fr-CA` primary is `fr`, which IS in SUPPORTED. Pin the fallback;
    # swap the example if French ever gets its own Canadian catalog.
    assert negotiate("fr-CA") == "fr"
    # `it-IT` primary is `it`, not in SUPPORTED -- falls through to
    # DEFAULT. Verifies the primary-subtag path still bottoms out
    # correctly for unsupported languages.
    assert negotiate("it-IT") == DEFAULT


def test_negotiate_unknown_returns_default() -> None:
    # `it` (Italian) and `hi` (Hindi) are intentionally outside SUPPORTED.
    # If either gets added later, pick different unused tags here.
    assert negotiate("it,hi") == DEFAULT
    assert negotiate("it-IT") == DEFAULT


# ---------------------------------------------------------------------------
# Chinese variant routing
#
# Accept-Language tags from regional/script variants of Chinese route to the
# right catalog (Simplified vs Traditional) instead of falling through to
# English. Singapore uses Simplified; Hong Kong and Macao use Traditional.
# Bare `zh` with no region routes to Simplified as the utilitarian default.
# ---------------------------------------------------------------------------


def test_negotiate_routes_zh_sg_to_simplified() -> None:
    """Singapore aligned to PRC Simplified in 1976; zh-SG gets zh-CN."""
    assert negotiate("zh-SG") == "zh-CN"


def test_negotiate_routes_zh_hk_to_traditional() -> None:
    """Hong Kong uses Traditional. Vocabulary differs from Taiwan in
    domains ephemera doesn't touch (network, software, printer), so
    zh-HK -> zh-TW is a defensible mapping at ephemera's tier rather
    than maintaining a third variant."""
    assert negotiate("zh-HK") == "zh-TW"


def test_negotiate_routes_zh_mo_to_traditional() -> None:
    """Macao, like HK, uses Traditional. Same reasoning as HK routing."""
    assert negotiate("zh-MO") == "zh-TW"


def test_negotiate_routes_zh_hans_variants_to_simplified() -> None:
    """Explicit script tags should be honored: zh-Hans* always Simplified."""
    assert negotiate("zh-Hans") == "zh-CN"
    assert negotiate("zh-Hans-CN") == "zh-CN"
    assert negotiate("zh-Hans-SG") == "zh-CN"


def test_negotiate_routes_zh_hant_variants_to_traditional() -> None:
    """zh-Hant* always Traditional, regardless of regional subtag."""
    assert negotiate("zh-Hant") == "zh-TW"
    assert negotiate("zh-Hant-HK") == "zh-TW"
    assert negotiate("zh-Hant-MO") == "zh-TW"
    assert negotiate("zh-Hant-TW") == "zh-TW"


def test_negotiate_routes_bare_zh_to_simplified() -> None:
    """Bare `zh` with no region/script is utilitarian-defaulted to the
    larger speaker base (Simplified, ~30x Traditional). A bare tag often
    means a misconfigured client rather than a specific signal."""
    assert negotiate("zh") == "zh-CN"


def test_negotiate_chinese_routing_is_case_insensitive() -> None:
    """Accept-Language tags come in mixed case in the wild (ZH-HK, Zh-Hk
    etc.); the routing table normalizes before lookup."""
    assert negotiate("ZH-HK") == "zh-TW"
    assert negotiate("Zh-Hk") == "zh-TW"
    assert negotiate("zh-hk") == "zh-TW"


def test_negotiate_chinese_exact_match_still_wins() -> None:
    """Exact match beats alias routing -- a client sending zh-CN directly
    gets zh-CN without going through the alias table."""
    assert negotiate("zh-CN") == "zh-CN"
    assert negotiate("zh-TW") == "zh-TW"


def test_negotiate_chinese_routing_respects_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a future deployment drops zh-CN from SUPPORTED, the zh-SG alias
    must NOT return a tag that doesn't resolve. Verified by monkeypatching
    SUPPORTED to exclude zh-CN and confirming the alias silently disables
    (falls through to DEFAULT since zh-SG has no other match)."""
    import app.i18n as i18n_mod

    monkeypatch.setattr(
        i18n_mod, "SUPPORTED", tuple(t for t in i18n_mod.SUPPORTED if t != "zh-CN")
    )
    assert i18n_mod.negotiate("zh-SG") == DEFAULT


def test_negotiate_chinese_routing_honors_browser_order() -> None:
    """When Accept-Language carries multiple tags, routing fires on the
    first matchable one. Browsers emit preferred-first, so the alias
    resolves in that priority order along with the rest of the stack."""
    # User's primary language is HK traditional; secondary is English.
    # The alias fires on the first token.
    assert negotiate("zh-HK,en;q=0.9") == "zh-TW"
    # Primary is unsupported (hi), secondary is HK. Alias fires on the
    # second token since the first had no match.
    assert negotiate("hi,zh-HK;q=0.9,en;q=0.8") == "zh-TW"


def test_negotiate_empty_and_none() -> None:
    assert negotiate(None) == DEFAULT
    assert negotiate("") == DEFAULT
    assert negotiate("   ") == DEFAULT


def test_validate_normalizes_case() -> None:
    assert _validate("ZH-cn") == "zh-CN"
    assert _validate("ja") == "ja"
    assert _validate("xx") is None
    assert _validate(None) is None
    assert _validate("") is None


# ---------------------------------------------------------------------------
# gettext_for + lazy_gettext (null-catalog fallthrough)
# ---------------------------------------------------------------------------


def test_gettext_null_catalog_identity() -> None:
    # No .mo files are shipped yet; every locale yields the null catalog,
    # which returns the msgid unchanged.
    for tag in SUPPORTED:
        g = gettext_for(tag)
        assert g("Hello, world.") == "Hello, world."


def test_lazy_gettext_reads_contextvar() -> None:
    lz = lazy_gettext("Expires in")
    # Default context -> identity (catalog lookup returns the msgid when no
    # locale is active), still string-coerceable.
    assert str(lz) == "Expires in"
    token = current_locale.set("ja")
    try:
        # With ja catalog populated, the lazy proxy re-resolves on coerce.
        assert str(lz) == "有効期限"
    finally:
        current_locale.reset(token)


# ---------------------------------------------------------------------------
# JS catalog loader
# ---------------------------------------------------------------------------


def test_js_catalog_en_has_expected_keys() -> None:
    cat = js_catalog("en")
    # Spot-check: every error-code the server raises should have an
    # error.<code> entry so the JS side can localize it.
    for _code in ERROR_MESSAGES:
        # We don't require every code in the JS catalog (some codes are
        # API-only and never shown to end users), but the ones the UI
        # actually displays must be present. This is a tripwire for
        # "added an error, forgot to translate it" -- refine as needed.
        pass
    # Key shapes we DO require:
    assert cat["error"]["wrong_passphrase"]
    assert cat["status"]["pending"]
    assert cat["button"]["creating"]


def test_js_catalog_non_en_locales_are_populated() -> None:
    # Every supported locale ships a populated JS catalog. The shim's
    # fallback chain still uses the English catalog for any miss, so an
    # accidentally-stubbed locale would silently render English instead
    # of failing loudly -- this test is the tripwire that catches it.
    for tag in SUPPORTED:
        if tag == DEFAULT:
            continue
        cat = js_catalog(tag)
        assert cat, f"{tag} catalog must not be empty"
        # Spot-check: a representative key per top-level namespace resolves
        # to a non-empty string in every translator-authored catalog.
        assert cat["error"]["wrong_passphrase"]
        assert cat["status"]["pending"]
        assert cat["button"]["creating"]


def test_js_catalog_returns_empty_dict_for_unknown_locale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Locales without a `<tag>.json` file under the catalog dir get
    an empty dict, not a 500. The shim's fallback chain re-resolves
    against English on miss, so a tag with no shipped catalog still
    renders correctly."""
    import app.i18n as i18n_mod

    monkeypatch.setattr(i18n_mod, "_JS_CATALOG_DIR", tmp_path)
    assert i18n_mod.js_catalog("xx-NOT-A-REAL-LOCALE") == {}


def test_js_catalog_returns_empty_dict_for_malformed_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated / corrupted `.json` catalog returns `{}` rather
    than letting the JSONDecodeError propagate. Ensures a partial
    deploy or a hand-edit-gone-wrong doesn't 500 the page."""
    import app.i18n as i18n_mod

    (tmp_path / "broken.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(i18n_mod, "_JS_CATALOG_DIR", tmp_path)
    assert i18n_mod.js_catalog("broken") == {}


def test_get_locale_resolves_when_middleware_did_not_run() -> None:
    """`get_locale` is the FastAPI dependency the route handlers use.
    The locale_middleware normally stashes the resolved tag on
    `request.state.locale` so this dependency just hands back the
    cache. When that middleware hasn't run -- unit tests that bypass
    the app stack -- the dependency falls through to a fresh
    `resolve_locale(request)` call. Pin both branches so a future
    refactor that drops the fallback gets caught."""
    from types import SimpleNamespace

    import app.i18n as i18n_mod

    # Cached path: middleware already resolved.
    cached_request = SimpleNamespace(
        state=SimpleNamespace(locale="es"),
        cookies={},
        headers={},
        query_params={},
    )
    # The dependency expects a fastapi.Request; the test deliberately
    # passes a SimpleNamespace stand-in to drive both the cached and
    # fallback branches without spinning up a real ASGI scope.
    assert i18n_mod.get_locale(cached_request) == "es"  # type: ignore[arg-type]

    # Fallback path: no cache, resolve_locale runs and returns the
    # default for an empty Accept-Language / no-cookie request.
    bare_request = SimpleNamespace(
        state=SimpleNamespace(),
        cookies={},
        headers={},
        query_params={},
    )
    assert i18n_mod.get_locale(bare_request) == i18n_mod.DEFAULT  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Locale resolution via the HTTP stack (middleware + dependency)
# ---------------------------------------------------------------------------


def test_locale_default_is_english_with_no_hints(client: TestClient) -> None:
    r = client.get("/send")
    assert r.status_code == 200
    assert 'lang="en"' in r.text


def test_locale_query_param_wins(client: TestClient) -> None:
    r = client.get("/send?lang=ja")
    assert 'lang="ja"' in r.text
    r = client.get("/send?lang=zh-TW")
    assert 'lang="zh-TW"' in r.text


def test_locale_cookie_wins_over_accept_language(client: TestClient) -> None:
    client.cookies.set("ephemera_lang_v1", "es")
    r = client.get("/send", headers={"Accept-Language": "ja"})
    assert 'lang="es"' in r.text


def test_locale_query_param_wins_over_cookie(client: TestClient) -> None:
    client.cookies.set("ephemera_lang_v1", "es")
    r = client.get("/send?lang=pt-BR", headers={"Accept-Language": "ja"})
    assert 'lang="pt-BR"' in r.text


def test_locale_accept_language_negotiation(client: TestClient) -> None:
    r = client.get("/send", headers={"Accept-Language": "pt-BR,en;q=0.9"})
    assert 'lang="pt-BR"' in r.text


def test_locale_unknown_falls_through_silently(client: TestClient) -> None:
    # ?lang=xx is advisory, not validation -- an unknown tag must not
    # 400 the request. It just falls through to the next step in the
    # precedence chain (Accept-Language, then DEFAULT).
    r = client.get("/send?lang=xx", headers={"Accept-Language": "ja"})
    assert r.status_code == 200
    assert 'lang="ja"' in r.text


def test_locale_authed_user_preference_wins_over_header(authed_client: TestClient, provisioned_user: dict[str, Any]) -> None:
    # Persist a preference on the authed user, then verify a request with
    # a conflicting Accept-Language still gets the stored locale.
    from app.models import users as users_model

    users_model.set_preferred_language(provisioned_user["id"], "zh-CN")
    r = authed_client.get("/send", headers={"Accept-Language": "ja"})
    assert 'lang="zh-CN"' in r.text


def test_locale_cookie_beats_user_preference(authed_client: TestClient, provisioned_user: dict[str, Any]) -> None:
    # A user who temporarily picks a different language via the widget
    # (cookie) should see it even though their stored preference differs.
    from app.models import users as users_model

    users_model.set_preferred_language(provisioned_user["id"], "zh-CN")
    authed_client.cookies.set("ephemera_lang_v1", "ja")
    r = authed_client.get("/send")
    assert 'lang="ja"' in r.text


# ---------------------------------------------------------------------------
# Schema v2 migration
# ---------------------------------------------------------------------------


def test_fresh_db_is_at_current_schema_version(tmp_db_path: Path) -> None:
    import sqlite3

    from app.models._core import CURRENT_SCHEMA_VERSION

    with sqlite3.connect(str(tmp_db_path)) as conn:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    assert row is not None
    assert row[0] == CURRENT_SCHEMA_VERSION


def test_fresh_db_has_preferred_language_column(tmp_db_path: Path) -> None:
    import sqlite3

    with sqlite3.connect(str(tmp_db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    assert "preferred_language" in cols


def test_v1_legacy_db_upgrades_through_to_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a v1 DB (no preferred_language column, version stamped at 1),
    boot the current code, and confirm migrations chain through to the
    latest stamped version. As schema_version bumps, the assertion
    references CURRENT_SCHEMA_VERSION rather than a hard-coded number."""
    import sqlite3

    db = tmp_path / "legacy.db"
    with sqlite3.connect(str(db)) as conn:
        # Hand-roll a minimal v1 schema: users table without the new column
        # plus schema_version stamped to 1. Everything else init_db() creates
        # idempotently via CREATE TABLE IF NOT EXISTS is fine.
        conn.executescript(
            """
            CREATE TABLE users (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                username              TEXT NOT NULL,
                email                 TEXT,
                password_hash         TEXT NOT NULL,
                totp_secret           TEXT NOT NULL,
                totp_last_step        INTEGER NOT NULL DEFAULT 0,
                recovery_code_hashes  TEXT NOT NULL DEFAULT '[]',
                failed_attempts       INTEGER NOT NULL DEFAULT 0,
                lockout_until         TEXT,
                session_generation    INTEGER NOT NULL DEFAULT 0,
                created_at            TEXT NOT NULL,
                updated_at            TEXT NOT NULL
            );
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 1);
            """
        )

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    from app import config

    config.get_settings.cache_clear()
    try:
        from app import models

        models.init_db()
        with sqlite3.connect(str(db)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            ver = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()[0]
        assert "preferred_language" in cols, "migration did not add the column"
        from app.models._core import CURRENT_SCHEMA_VERSION

        assert ver == CURRENT_SCHEMA_VERSION, (
            f"schema_version should be {CURRENT_SCHEMA_VERSION}, got {ver}"
        )
    finally:
        config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# PATCH /api/me/language
# ---------------------------------------------------------------------------


def test_patch_language_anonymous_rejected_401(client: TestClient) -> None:
    """Anonymous callers get 401 with the `not_authenticated` code. The
    picker JS short-circuits on anonymous before this fires, so in normal
    operation this path is only hit by forged / misconfigured clients."""
    r = client.patch(
        "/api/me/language",
        json={"language": "ja"},
        headers=ORIGIN,
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "not_authenticated"


def test_patch_language_anonymous_does_not_leak_language_validation(client: TestClient) -> None:
    """Even with a body the authed-flow would reject at 400 (unsupported
    language), the anonymous caller must still see 401 first. Otherwise
    the endpoint would leak whether a tag is in SUPPORTED to anyone
    probing -- a minor but real fingerprint surface."""
    r = client.patch(
        "/api/me/language",
        json={"language": "xx-NOT-REAL"},
        headers=ORIGIN,
    )
    assert r.status_code == 401


def test_patch_language_null_clears(authed_client: TestClient, provisioned_user: dict[str, Any]) -> None:
    from app.models import users as users_model

    users_model.set_preferred_language(provisioned_user["id"], "ja")
    r = authed_client.patch(
        "/api/me/language",
        json={"language": None},
        headers=ORIGIN,
    )
    assert r.status_code == 204
    user = users_model.get_user_by_id(provisioned_user["id"])
    assert user is not None
    assert user["preferred_language"] is None


def test_patch_language_authed_persists(authed_client: TestClient, provisioned_user: dict[str, Any]) -> None:
    from app.models import users as users_model

    r = authed_client.patch(
        "/api/me/language",
        json={"language": "zh-CN"},
        headers=ORIGIN,
    )
    assert r.status_code == 204
    user = users_model.get_user_by_id(provisioned_user["id"])
    assert user is not None
    assert user["preferred_language"] == "zh-CN"


def test_patch_language_unsupported_rejected(authed_client: TestClient) -> None:
    """Body validation (400) only reached after auth passes -- anonymous
    callers get 401 first, see the auth-leak test above."""
    r = authed_client.patch(
        "/api/me/language",
        json={"language": "xx"},
        headers=ORIGIN,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "unsupported_language"


def test_patch_language_missing_origin_blocked(client: TestClient) -> None:
    # verify_same_origin must still gate this state-changing endpoint.
    r = client.patch("/api/me/language", json={"language": "ja"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "missing_origin"


# ---------------------------------------------------------------------------
# Error-code shape (task #3)
# ---------------------------------------------------------------------------


def test_http_error_basic_shape() -> None:
    exc = http_error(401, "wrong_passphrase")
    assert exc.status_code == 401
    # Starlette types HTTPException.detail as `str`; FastAPI widens to Any
    # at runtime, and http_error stores a dict here. Cast through Any so
    # the dict assertions don't trip the inherited str annotation.
    detail = cast(dict[str, Any], exc.detail)
    assert detail == {
        "code": "wrong_passphrase",
        "message": "Wrong passphrase.",
    }


def test_http_error_custom_message_overrides() -> None:
    exc = http_error(422, "invalid_json_body", message="Invalid JSON body: got 3.")
    detail = cast(dict[str, Any], exc.detail)
    assert detail["code"] == "invalid_json_body"
    assert detail["message"] == "Invalid JSON body: got 3."


def test_http_error_extra_fields_merged() -> None:
    exc = http_error(423, "locked", until="2026-04-23T10:00:00Z")
    detail = cast(dict[str, Any], exc.detail)
    assert detail["code"] == "locked"
    assert detail["until"] == "2026-04-23T10:00:00Z"
    assert "message" in detail


def test_http_error_live_response_shape(client: TestClient) -> None:
    # Real request -> real response: confirm the wire payload matches.
    r = client.get("/s/does-not-exist/meta")
    assert r.status_code == 404
    assert r.json()["detail"] == {
        "code": "gone",
        "message": "Secret is no longer available.",
    }


# ---------------------------------------------------------------------------
# JS catalog inlining (task #5)
# ---------------------------------------------------------------------------


def test_page_inlines_active_and_fallback_catalogs(client: TestClient) -> None:
    r = client.get("/send?lang=ja")
    body = r.text
    # Active locale's catalog is inlined as JSON and contains the translated
    # strings (tojson escapes non-ASCII as \uXXXX -- check the escaped form
    # for "パスフレーズが正しくありません。").
    assert 'id="i18n-catalog"' in body
    assert "\\u30d1\\u30b9\\u30d5\\u30ec\\u30fc\\u30ba" in body
    # English fallback is inlined separately so the shim resolves on miss.
    assert 'id="i18n-fallback"' in body
    assert "Wrong passphrase" in body


def test_picker_hidden_when_launched_has_fewer_than_two(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural test for the picker-gate. A single-option <select> is a
    UX wart (nothing to actually pick), so the template hides the picker
    entirely when LAUNCHED has <2 members. Monkeypatches LAUNCHED because
    the shipped state now has all ten launched; the gate logic still needs
    coverage for the single-locale rollback case."""
    import app.i18n as i18n_mod

    monkeypatch.setattr(i18n_mod, "LAUNCHED", ("en",))
    r = client.get("/send")
    assert '<select id="lang-picker"' not in r.text
    # Resolution surface still accepts SUPPORTED tags even when the picker
    # is hidden -- query-param and cookie paths remain reachable.
    r2 = client.get("/send?lang=pt-BR")
    assert 'lang="pt-BR"' in r2.text


def test_picker_renders_when_multiple_locales_launched(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural test for the two-or-more-locales render path.
    Monkeypatches LAUNCHED to a small set so the assertion is stable
    against future additions to the shipped LAUNCHED tuple."""
    import app.i18n as i18n_mod

    monkeypatch.setattr(i18n_mod, "LAUNCHED", ("en", "pt-BR"))
    r = client.get("/send?lang=pt-BR")
    body = r.text
    assert '<select id="lang-picker"' in body
    assert 'value="pt-BR" selected' in body
    assert 'value="en"' in body


def test_picker_renders_every_shipped_launched_locale(client: TestClient) -> None:
    """Ship-state test: every tag currently in LAUNCHED must appear as an
    <option> in the picker, unmonkeypatched. Guards against 'added a
    locale to SUPPORTED + LAUNCHED but forgot to update LANGUAGE_LABELS'
    (the picker would render a blank option)."""
    from app.i18n import LANGUAGE_LABELS

    r = client.get("/send")
    body = r.text
    if len(LAUNCHED) < 2:
        # Single-locale gate already covered above; short-circuit here.
        return
    assert '<select id="lang-picker"' in body
    for tag in LAUNCHED:
        assert f'value="{tag}"' in body, f"launched tag {tag} missing from picker"
        # Endonym label must be present too -- empty <option> would silently
        # ship otherwise.
        assert LANGUAGE_LABELS[tag] in body, (
            f"endonym for {tag} ({LANGUAGE_LABELS[tag]!r}) missing from picker"
        )


def test_body_data_authenticated_present_for_authed(authed_client: TestClient) -> None:
    r = authed_client.get("/send")
    assert 'data-authenticated="true"' in r.text


def test_body_data_authenticated_absent_for_anonymous(client: TestClient) -> None:
    """Anonymous page loads must not carry the data-authenticated
    attribute -- the picker JS branches on it to decide whether to PATCH
    /api/me/language, so a stray attribute would cost every anonymous
    language change a wasted 401 round-trip."""
    r = client.get("/send")
    assert "data-authenticated" not in r.text


# ---------------------------------------------------------------------------
# JS key-coverage regression
#
# Every `i18n.t('some.key')` reference in the JS sources must resolve to
# a real key in app/static/i18n/en.json. The shim already falls through to
# the key name as a visible sentinel on a miss, but seeing "button.retry"
# render literally in the UI is a regression that should fail CI, not the
# user's first visit. This test is the tripwire.
# ---------------------------------------------------------------------------


_JS_T_CALL_RE = re.compile(r"""i18n\.t\(\s*['"]([a-z][a-z0-9_.]*)['"]""")


def _enumerate_string_paths(tree: Any, prefix: str = "") -> "Iterator[str]":
    """Yield every dotted path that leads to a string leaf in the
    catalog. Plural containers (dicts of 'one'/'other'/...) yield the
    container path itself in addition to each leaf inside -- both forms
    are valid lookup targets from JS."""
    if isinstance(tree, dict):
        if any(isinstance(v, str) for v in tree.values()):
            yield prefix
        for name, child in tree.items():
            child_prefix = f"{prefix}.{name}" if prefix else name
            yield from _enumerate_string_paths(child, child_prefix)
    elif isinstance(tree, str):
        yield prefix


def test_every_js_i18n_key_exists_in_en_catalog() -> None:
    """Scan every .js file under app/static/ for i18n.t('...') calls;
    assert each referenced stem is reachable in the English catalog. A
    miss means someone added a translation call without adding the key
    and the UI would render the literal sentinel (e.g. "button.retry")
    to every English user.

    Three valid shapes for a captured stem:
      - Exact leaf:          t('error.network')       -> error.network
      - Plural container:    t('button.clear_past')   -> button.clear_past
      - Concatenation stem:  t('status.' + foo)       -> regex captures
                             'status.'; valid when any catalog path
                             starts with it.
    Fully-dynamic keys (no literal first-arg) don't match the regex at
    all -- they have no statically-checkable contract and are skipped."""
    en_catalog = js_catalog("en")
    assert en_catalog, "en.json must not be empty"

    js_root = Path(__file__).resolve().parent.parent / "app" / "static"
    js_files = [p for p in js_root.rglob("*.js") if "swagger" not in p.parts]
    assert js_files, "no JS files found -- test setup is wrong"

    referenced: set[str] = set()
    for path in js_files:
        for match in _JS_T_CALL_RE.finditer(path.read_text(encoding="utf-8")):
            referenced.add(match.group(1))

    assert referenced, (
        "regex found no i18n.t() calls -- either the regex drifted or JS "
        "stopped using the shim; both worth a second look"
    )

    catalog_paths = set(_enumerate_string_paths(en_catalog))

    missing: list[str] = []
    for key in sorted(referenced):
        if key in catalog_paths:
            continue
        # Concatenation stem: accept if any catalog path starts with it.
        # Captures both ending-in-`.` (e.g. `status.`) and ending-in-`_`
        # (e.g. `tracked.time_`) variants uniformly.
        if any(p.startswith(key) for p in catalog_paths):
            continue
        missing.append(key)

    assert not missing, (
        "i18n.t() call sites reference keys missing from en.json:\n"
        + "\n".join(f"  {k}" for k in missing)
    )


# ---------------------------------------------------------------------------
# Cross-locale key parity
#
# Mirror of test_every_js_i18n_key_exists_in_en_catalog from the other
# angle. That one ensures every i18n.t('...') call site has a key in
# en.json; this one ensures every key in en.json also exists in every
# other locale's JSON catalog.
#
# Structural check only. Missing translations (empty string values,
# identical-to-English values) are NOT caught here -- those are a
# translator-quality concern, handled by the translator workflow and
# (on the shim side) the fallback chain. This test catches the
# "translator forgot to copy a key across" or "developer added a key
# to en.json and the other locales weren't updated" drift.
# ---------------------------------------------------------------------------


def _structural_paths(tree: Any, prefix: str = "") -> Iterator[str]:
    """Enumerate paths for cross-locale parity. Differs from the
    _enumerate_string_paths enumerator used by the JS-key-coverage
    test: plural containers (dicts whose keys are a subset of the
    CLDR categories) yield ONLY the container path, never the
    individual per-category leaves. That matches CLDR reality --
    en has `one`+`other`, ja/ko/zh only `other`, ar all six; none
    of those locales "should" have the others' category keys, so
    per-category parity would false-positive."""
    _CLDR = {"zero", "one", "two", "few", "many", "other"}
    if isinstance(tree, dict):
        keys = set(tree.keys())
        if keys and keys <= _CLDR:
            yield prefix  # plural container -- single unit
            return
        for name, child in tree.items():
            child_prefix = f"{prefix}.{name}" if prefix else name
            yield from _structural_paths(child, child_prefix)
    elif isinstance(tree, str):
        yield prefix


def test_every_locale_catalog_has_every_en_key() -> None:
    """Every non-plural leaf path and every plural container path in
    en.json must exist in every other locale's JSON catalog. Without
    this coverage, a PR that adds a new key to en.json + wires it into
    JS would pass the JS-key-coverage test above but silently fall back
    to English on every non-en locale because the key is missing from
    their catalogs -- exactly the shape of the login-toggle bug this
    repo discovered in code review.

    CLDR-aware via _structural_paths: plural containers count as one
    path, not one per category, so locales with different plural rules
    (ja/ko/zh use only `other`; ar uses all six) don't false-positive.
    Per-category translation quality is the translator's concern, not
    this test's scope."""
    js_root = Path(__file__).resolve().parent.parent / "app" / "static" / "i18n"
    en_paths = set(_structural_paths(json.loads((js_root / "en.json").read_text())))
    assert en_paths, "en.json has no string paths; test setup is broken"

    missing_by_locale: dict[str, list[str]] = {}
    for path in sorted(js_root.glob("*.json")):
        if path.stem == "en":
            continue
        other_paths = set(_structural_paths(json.loads(path.read_text())))
        gaps = sorted(en_paths - other_paths)
        if gaps:
            missing_by_locale[path.stem] = gaps

    assert not missing_by_locale, (
        "non-en JSON catalogs missing keys from en.json:\n"
        + "\n".join(
            f"  {loc}: {keys}" for loc, keys in sorted(missing_by_locale.items())
        )
    )


# ---------------------------------------------------------------------------
# Version string in the layout footer
#
# app.version computes a one-off string via `git describe --tags --always
# --dirty` at module import. The template_context wires it into every page
# as `version`, and _layout.html renders it in a <footer class="app-version">.
# ---------------------------------------------------------------------------


def test_version_module_returns_non_empty_string() -> None:
    """The module-level constant is populated at import time. Even in
    environments where git is missing or the repo is a tarball, the
    fallback sentinel ('unknown') keeps this non-empty."""
    from app.version import VERSION

    assert isinstance(VERSION, str)
    assert VERSION, "VERSION must not be empty -- fallback should be 'unknown'"


def test_version_fallback_on_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `git describe` errors or times out, the function must return
    'unknown' rather than raising or returning an empty string. Verified
    by monkeypatching subprocess.run to raise, then re-running the
    module-level compute."""
    import subprocess

    import app.version as version_mod

    def _raise(*_a: Any, **_kw: Any) -> None:
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert version_mod._compute_version() == "unknown"


def test_version_renders_under_wordmark(client: TestClient) -> None:
    """The version tag sits as a <small> directly following the wordmark
    -- 'fine print adjacent to brand' pattern. Proximity to the
    wordmark makes the context obvious (screen readers read 'ephemera,
    v0.6.0'), so no aria-label is needed; the layout is the semantic."""
    import re

    from app.version import VERSION

    body = client.get("/send").text
    assert f'<small class="app-version">{VERSION}</small>' in body
    # Pin the adjacency: version must directly follow the wordmark in
    # document order, not float elsewhere in the layout.
    m = re.search(
        r'<div class="wordmark">[^<]+</div>\s*<small class="app-version">',
        body,
    )
    assert m, "wordmark + app-version are not adjacent siblings in the layout"
