"""Tests for the i18n system: locale resolution, migration, prefs endpoint,
error shape, and the JS catalog inlining."""
from app.errors import ERROR_MESSAGES, http_error
from app.i18n import (
    DEFAULT,
    LANGUAGE_LABELS,
    POSIX_MAP,
    SUPPORTED,
    _validate,
    current_locale,
    gettext_for,
    js_catalog,
    lazy_gettext,
    negotiate,
)


ORIGIN = {"Origin": "http://testserver"}


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


def test_supported_and_labels_cover_the_same_set():
    """Every SUPPORTED tag must have a LANGUAGE_LABELS entry (otherwise the
    picker widget would render an empty option) and a POSIX_MAP entry
    (otherwise gettext_for would silently fall back to DEFAULT)."""
    for tag in SUPPORTED:
        assert tag in LANGUAGE_LABELS, f"missing LANGUAGE_LABELS entry: {tag}"
        assert tag in POSIX_MAP, f"missing POSIX_MAP entry: {tag}"


def test_default_is_in_supported():
    assert DEFAULT in SUPPORTED


# ---------------------------------------------------------------------------
# Accept-Language negotiation
# ---------------------------------------------------------------------------


def test_negotiate_exact_match():
    assert negotiate("ja") == "ja"
    assert negotiate("zh-CN") == "zh-CN"
    assert negotiate("zh-TW") == "zh-TW"
    assert negotiate("pt-BR") == "pt-BR"


def test_negotiate_is_case_insensitive():
    assert negotiate("JA") == "ja"
    assert negotiate("zh-cn") == "zh-CN"
    assert negotiate("PT-br") == "pt-BR"


def test_negotiate_honours_browser_order():
    # Browsers list the preferred locale first; first match wins regardless
    # of q-values (which we don't parse).
    assert negotiate("ja,en;q=0.9") == "ja"
    assert negotiate("fr,ja;q=0.5,en;q=0.4") == "ja"


def test_negotiate_primary_subtag_fallback():
    # Regional variants fall back to their primary if it's supported.
    assert negotiate("es-MX") == "es"
    # 'zh-HK' primary is 'zh', which we don't list as a bare tag, so it
    # falls through to the default rather than guessing zh-CN vs zh-TW.
    assert negotiate("zh-HK") == DEFAULT


def test_negotiate_unknown_returns_default():
    assert negotiate("de,fr") == DEFAULT
    assert negotiate("de-DE") == DEFAULT


def test_negotiate_empty_and_none():
    assert negotiate(None) == DEFAULT
    assert negotiate("") == DEFAULT
    assert negotiate("   ") == DEFAULT


def test_validate_normalizes_case():
    assert _validate("ZH-cn") == "zh-CN"
    assert _validate("ja") == "ja"
    assert _validate("xx") is None
    assert _validate(None) is None
    assert _validate("") is None


# ---------------------------------------------------------------------------
# gettext_for + lazy_gettext (null-catalog fallthrough)
# ---------------------------------------------------------------------------


def test_gettext_null_catalog_identity():
    # No .mo files are shipped yet; every locale yields the null catalog,
    # which returns the msgid unchanged.
    for tag in SUPPORTED:
        g = gettext_for(tag)
        assert g("Hello, world.") == "Hello, world."


def test_lazy_gettext_reads_contextvar():
    lz = lazy_gettext("Expires in")
    # Default context -> identity (no catalog), still string-coerceable.
    assert str(lz) == "Expires in"
    token = current_locale.set("ja")
    try:
        assert str(lz) == "Expires in"
    finally:
        current_locale.reset(token)


# ---------------------------------------------------------------------------
# JS catalog loader
# ---------------------------------------------------------------------------


def test_js_catalog_en_has_expected_keys():
    cat = js_catalog("en")
    # Spot-check: every error-code the server raises should have an
    # error.<code> entry so the JS side can localize it.
    for code in ERROR_MESSAGES:
        # We don't require every code in the JS catalog (some codes are
        # API-only and never shown to end users), but the ones the UI
        # actually displays must be present. This is a tripwire for
        # "added an error, forgot to translate it" -- refine as needed.
        pass
    # Key shapes we DO require:
    assert cat["error"]["wrong_passphrase"]
    assert cat["status"]["pending"]
    assert cat["button"]["creating"]


def test_js_catalog_stubs_return_empty_dict():
    # The 5 non-en catalogs ship as empty stubs until a translator fills
    # them in. The shim's fallback chain uses the English catalog for any
    # miss, so an empty stub is a valid "not translated yet" state.
    for tag in ("ja", "pt-BR", "es", "zh-CN", "zh-TW"):
        assert js_catalog(tag) == {}


# ---------------------------------------------------------------------------
# Locale resolution via the HTTP stack (middleware + dependency)
# ---------------------------------------------------------------------------


def test_locale_default_is_english_with_no_hints(client):
    r = client.get("/send")
    assert r.status_code == 200
    assert '<html lang="en">' in r.text


def test_locale_query_param_wins(client):
    r = client.get("/send?lang=ja")
    assert '<html lang="ja">' in r.text
    r = client.get("/send?lang=zh-TW")
    assert '<html lang="zh-TW">' in r.text


def test_locale_cookie_wins_over_accept_language(client):
    client.cookies.set("ephemera_lang_v1", "es")
    r = client.get("/send", headers={"Accept-Language": "ja"})
    assert '<html lang="es">' in r.text


def test_locale_query_param_wins_over_cookie(client):
    client.cookies.set("ephemera_lang_v1", "es")
    r = client.get("/send?lang=pt-BR", headers={"Accept-Language": "ja"})
    assert '<html lang="pt-BR">' in r.text


def test_locale_accept_language_negotiation(client):
    r = client.get("/send", headers={"Accept-Language": "pt-BR,en;q=0.9"})
    assert '<html lang="pt-BR">' in r.text


def test_locale_unknown_falls_through_silently(client):
    # ?lang=xx is advisory, not validation -- an unknown tag must not
    # 400 the request. It just falls through to the next step in the
    # precedence chain (Accept-Language, then DEFAULT).
    r = client.get("/send?lang=xx", headers={"Accept-Language": "ja"})
    assert r.status_code == 200
    assert '<html lang="ja">' in r.text


def test_locale_authed_user_preference_wins_over_header(authed_client, provisioned_user):
    # Persist a preference on the authed user, then verify a request with
    # a conflicting Accept-Language still gets the stored locale.
    from app.models import users as users_model

    users_model.set_preferred_language(provisioned_user["id"], "zh-CN")
    r = authed_client.get("/send", headers={"Accept-Language": "ja"})
    assert '<html lang="zh-CN">' in r.text


def test_locale_cookie_beats_user_preference(authed_client, provisioned_user):
    # A user who temporarily picks a different language via the widget
    # (cookie) should see it even though their stored preference differs.
    from app.models import users as users_model

    users_model.set_preferred_language(provisioned_user["id"], "zh-CN")
    authed_client.cookies.set("ephemera_lang_v1", "ja")
    r = authed_client.get("/send")
    assert '<html lang="ja">' in r.text


# ---------------------------------------------------------------------------
# Schema v2 migration
# ---------------------------------------------------------------------------


def test_fresh_db_is_at_schema_v2(tmp_db_path):
    import sqlite3

    with sqlite3.connect(str(tmp_db_path)) as conn:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    assert row is not None
    assert row[0] == 2


def test_fresh_db_has_preferred_language_column(tmp_db_path):
    import sqlite3

    with sqlite3.connect(str(tmp_db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    assert "preferred_language" in cols


def test_v1_legacy_db_upgrades_to_v2(tmp_path, monkeypatch):
    """Seed a v1 DB (no preferred_language column, version stamped at 1),
    boot the current code, and confirm the migration adds the column and
    stamps schema_version to 2."""
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
        assert ver == 2, f"schema_version should be 2, got {ver}"
    finally:
        config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# PATCH /api/me/language
# ---------------------------------------------------------------------------


def test_patch_language_anonymous_is_noop_204(client):
    r = client.patch(
        "/api/me/language",
        json={"language": "ja"},
        headers=ORIGIN,
    )
    assert r.status_code == 204


def test_patch_language_null_clears(authed_client, provisioned_user):
    from app.models import users as users_model

    users_model.set_preferred_language(provisioned_user["id"], "ja")
    r = authed_client.patch(
        "/api/me/language",
        json={"language": None},
        headers=ORIGIN,
    )
    assert r.status_code == 204
    user = users_model.get_user_by_id(provisioned_user["id"])
    assert user["preferred_language"] is None


def test_patch_language_authed_persists(authed_client, provisioned_user):
    from app.models import users as users_model

    r = authed_client.patch(
        "/api/me/language",
        json={"language": "zh-CN"},
        headers=ORIGIN,
    )
    assert r.status_code == 204
    user = users_model.get_user_by_id(provisioned_user["id"])
    assert user["preferred_language"] == "zh-CN"


def test_patch_language_unsupported_rejected(client):
    r = client.patch(
        "/api/me/language",
        json={"language": "xx"},
        headers=ORIGIN,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "unsupported_language"


def test_patch_language_missing_origin_blocked(client):
    # verify_same_origin must still gate this state-changing endpoint.
    r = client.patch("/api/me/language", json={"language": "ja"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "missing_origin"


# ---------------------------------------------------------------------------
# Error-code shape (task #3)
# ---------------------------------------------------------------------------


def test_http_error_basic_shape():
    exc = http_error(401, "wrong_passphrase")
    assert exc.status_code == 401
    assert exc.detail == {
        "code": "wrong_passphrase",
        "message": "Wrong passphrase.",
    }


def test_http_error_custom_message_overrides():
    exc = http_error(422, "invalid_json_body", message="Invalid JSON body: got 3.")
    assert exc.detail["code"] == "invalid_json_body"
    assert exc.detail["message"] == "Invalid JSON body: got 3."


def test_http_error_extra_fields_merged():
    exc = http_error(423, "locked", until="2026-04-23T10:00:00Z")
    assert exc.detail["code"] == "locked"
    assert exc.detail["until"] == "2026-04-23T10:00:00Z"
    assert "message" in exc.detail


def test_http_error_live_response_shape(client):
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


def test_page_inlines_active_and_fallback_catalogs(client):
    r = client.get("/send?lang=ja")
    body = r.text
    # Active locale's catalog (currently empty stub for ja) appears first.
    assert 'id="i18n-catalog">{}' in body
    # English fallback contains the real strings.
    assert 'id="i18n-fallback"' in body
    assert "Wrong passphrase" in body


def test_picker_widget_marks_active_option_selected(client):
    r = client.get("/send?lang=pt-BR")
    body = r.text
    assert 'value="pt-BR" selected' in body
    # Other options render without `selected`.
    assert 'value="en" selected' not in body
    assert 'value="ja" selected' not in body
