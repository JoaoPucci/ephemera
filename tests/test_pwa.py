"""Tests for the Progressive Web App install surface.

The manifest, the head wiring that links it, and the icon assets that
home-screen launchers capture at install time. Designer brief signed off
in PR-thread; implementation contract:

  - GET /manifest.webmanifest returns 200 with the
    application/manifest+json MIME type (browsers reject manifests
    served as application/octet-stream). Served as a route, not a
    static file, so the operator can vary the manifest per environment
    via EPHEMERA_DEPLOYMENT_LABEL.
  - Manifest is parseable JSON and carries the keys home-screen
    install needs: name, short_name, start_url, scope, display,
    theme_color, background_color, icons, plus an explicit `id` to
    decouple install identity from start_url.
  - id is "/" forever -- pinned so future moves of start_url (e.g.
    eventually re-rooting the sender at /) don't fork existing installs
    from fresh ones. Must be same-origin as start_url.
  - start_url is "/send?source=pwa" because the bare root returns 404
    today and the operator's job-to-be-done is creating a secret.
    ?source=pwa is the presence-only signal a future telemetry consumer
    could use to distinguish a home-screen launch from a browser visit.
  - display is "standalone" (chosen over minimal-ui because iOS
    doesn't honour minimal-ui).
  - Icons cover both purpose=any and purpose=maskable so Android can
    render its OS-cropped tile correctly.

  - Every HTML page (covered via /send) has the head tags Chrome and
    iOS Safari look for: manifest link, paired theme-color metas,
    apple-touch-icon, apple-mobile-web-app-capable, status-bar style,
    apple-mobile-web-app-title.

  - The PNG icons referenced by the manifest are reachable under
    /static/icons/ at 200, served as image/png.

  - When EPHEMERA_DEPLOYMENT_LABEL is set, the manifest's name +
    short_name become "ephemera-{label}", the icon list narrows to the
    visually-light variants (icon-*-dark-* in our file naming, where
    "dark" describes the OS theme the asset serves, not the tile's
    appearance), and the apple-touch-icon link in the head points at
    apple-touch-icon-light.png. This makes a non-prod install
    at-a-glance distinguishable from prod on the same home screen.
"""

import json

import pytest

MANIFEST_URL = "/manifest.webmanifest"


# ---------------------------------------------------------------------------
# Manifest endpoint (default / prod posture)
# ---------------------------------------------------------------------------


def test_manifest_endpoint_returns_200(client):
    r = client.get(MANIFEST_URL)
    assert r.status_code == 200, r.text


def test_manifest_endpoint_uses_manifest_mime(client):
    r = client.get(MANIFEST_URL)
    ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    assert ctype == "application/manifest+json", (
        f"manifest must be served as application/manifest+json, got {ctype!r}"
    )


def test_manifest_is_parseable_json(client):
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    assert isinstance(data, dict)


def test_manifest_has_required_keys(client):
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    required = {
        "name",
        "short_name",
        "start_url",
        "scope",
        "display",
        "theme_color",
        "background_color",
        "icons",
    }
    missing = required - data.keys()
    assert not missing, f"manifest missing required keys: {sorted(missing)}"


def test_manifest_pins_stable_id(client):
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    assert data.get("id") == "/", (
        "manifest must pin id=/ so identity is stable across start_url changes"
    )


def test_manifest_start_url_lands_on_sender(client):
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    assert data["start_url"] == "/send?source=pwa"


def test_manifest_display_is_standalone(client):
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    assert data["display"] == "standalone"


def test_manifest_default_name_is_ephemera_unsuffixed(client):
    # Empty deployment_label is the prod posture: bare "ephemera",
    # no environment suffix.
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    assert data["name"] == "ephemera"
    assert data["short_name"] == "ephemera"


def test_manifest_icons_cover_any_and_maskable_purposes(client):
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    purposes = {
        purpose
        for icon in data["icons"]
        for purpose in icon.get("purpose", "any").split()
    }
    assert "any" in purposes, "manifest must declare an icon with purpose=any"
    assert "maskable" in purposes, (
        "manifest must declare an icon with purpose=maskable for Android OS-crop"
    )


def test_manifest_icons_include_192_and_512(client):
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    sizes = {size for icon in data["icons"] for size in icon["sizes"].split()}
    assert "192x192" in sizes, "manifest must declare a 192x192 icon"
    assert "512x512" in sizes, "manifest must declare a 512x512 icon"


def test_manifest_default_lists_both_colourways(client):
    # Prod posture: list both visually-dark (icon-*-light-*, the
    # primary at install) and visually-light (icon-*-dark-*) variants
    # so prefers-color-scheme-aware browsers can pick. Non-prod
    # narrows this to the visually-light variants only -- that's
    # exercised separately in test_dev_label.
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    srcs = {icon["src"] for icon in data["icons"]}
    light_named = {s for s in srcs if "-light-" in s}
    dark_named = {s for s in srcs if "-dark-" in s}
    assert light_named, "prod manifest must list visually-dark (icon-*-light-*) icons"
    assert dark_named, "prod manifest must list visually-light (icon-*-dark-*) icons"


# ---------------------------------------------------------------------------
# Head wiring (every HTML page that could trigger install)
# ---------------------------------------------------------------------------


def test_head_links_manifest(client):
    html = client.get("/send").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html, (
        "head must link the manifest so install affordances pick it up"
    )


def test_head_has_theme_color_for_light_and_dark(client):
    html = client.get("/send").text
    assert (
        'name="theme-color"' in html and 'media="(prefers-color-scheme: light)"' in html
    ), "head must carry a light-mode theme-color meta"
    assert (
        'name="theme-color"' in html and 'media="(prefers-color-scheme: dark)"' in html
    ), "head must carry a dark-mode theme-color meta"


def test_head_links_apple_touch_icon(client):
    html = client.get("/send").text
    assert 'rel="apple-touch-icon"' in html, (
        "head must link an apple-touch-icon for iOS Add-to-Home-Screen"
    )


def test_head_default_apple_touch_icon_is_dark_variant(client):
    # Prod posture: dark-bg/light-glyph apple-touch-icon (filename
    # apple-touch-icon.png, no -light suffix). The -light variant is
    # only wired when EPHEMERA_DEPLOYMENT_LABEL is set.
    html = client.get("/send").text
    assert (
        '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">'
        in html
    )


def test_head_declares_apple_mobile_web_app_capable(client):
    html = client.get("/send").text
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html, (
        "iOS only enters standalone mode when this meta is present"
    )


def test_head_sets_apple_status_bar_style(client):
    html = client.get("/send").text
    assert 'name="apple-mobile-web-app-status-bar-style"' in html, (
        "head must set the iOS status-bar style for the standalone shell"
    )


def test_head_default_apple_mobile_web_app_title_is_unsuffixed(client):
    html = client.get("/send").text
    assert '<meta name="apple-mobile-web-app-title" content="ephemera">' in html, (
        "iOS uses this for the home-screen label; without it the <title> is used"
    )


# ---------------------------------------------------------------------------
# Icon assets (referenced from the manifest, must exist on disk and serve)
# ---------------------------------------------------------------------------


def test_apple_touch_icon_is_reachable(client):
    r = client.get("/static/icons/apple-touch-icon.png")
    assert r.status_code == 200, "apple-touch-icon.png missing from /static/icons/"
    assert r.headers.get("content-type", "").startswith("image/png"), (
        f"apple-touch-icon must be served as image/png, got {r.headers.get('content-type')!r}"
    )


def test_apple_touch_icon_light_variant_is_reachable(client):
    # Used when EPHEMERA_DEPLOYMENT_LABEL is set; must exist on disk
    # regardless of which posture the test instance is running in,
    # because the static mount is shared across all environments.
    r = client.get("/static/icons/apple-touch-icon-light.png")
    assert r.status_code == 200, (
        "apple-touch-icon-light.png missing -- regenerate via "
        "scripts/generate-pwa-icons.py"
    )
    assert r.headers.get("content-type", "").startswith("image/png")


def test_manifest_icon_targets_resolve(client):
    r = client.get(MANIFEST_URL)
    data = json.loads(r.text)
    for icon in data["icons"]:
        # src is manifest-relative; in our manifest we author absolute /static
        # paths so the same resolution rule works for both fetch and install.
        target = icon["src"]
        ir = client.get(target)
        assert ir.status_code == 200, (
            f"icon {target} declared in manifest but not reachable (status {ir.status_code})"
        )
        assert ir.headers.get("content-type", "").startswith("image/png"), (
            f"icon {target} must be image/png, got {ir.headers.get('content-type')!r}"
        )


# ---------------------------------------------------------------------------
# Deployment-label posture (non-prod environments: dev, staging, etc.)
#
# When EPHEMERA_DEPLOYMENT_LABEL is set, the manifest and the layout head
# both pivot:
#   - name + short_name suffix with "-{label}" so a dev install on the
#     same phone as prod doesn't collide on the home screen.
#   - manifest icon list narrows to the visually-light variants
#     (icon-*-dark-*), so the captured-at-install tile is the inverse
#     of prod's dark-bg/light-glyph.
#   - apple-touch-icon link points at apple-touch-icon-light.png (iOS
#     doesn't read the manifest, so this needs its own switch).
#   - apple-mobile-web-app-title meta carries the suffixed name so the
#     iOS home-screen label matches the manifest.
# ---------------------------------------------------------------------------


@pytest.fixture
def dev_label_client(tmp_db_path, monkeypatch):
    """TestClient with EPHEMERA_DEPLOYMENT_LABEL=dev wired through the
    settings cache before the app is built. Mirrors conftest.py's
    `client` shape but interposes the env var + cache_clear so
    create_app() picks up the dev posture."""
    from fastapi.testclient import TestClient

    from app import config, create_app
    from app.limiter import (
        create_limiter,
        login_limiter,
        read_limiter,
        reveal_limiter,
    )

    monkeypatch.setenv("EPHEMERA_DEPLOYMENT_LABEL", "dev")
    config.get_settings.cache_clear()

    for lim in (reveal_limiter, login_limiter, create_limiter, read_limiter):
        lim.reset()
    app = create_app()
    with TestClient(app) as c:
        yield c
    for lim in (reveal_limiter, login_limiter, create_limiter, read_limiter):
        lim.reset()
    config.get_settings.cache_clear()


def test_dev_manifest_name_is_suffixed(dev_label_client):
    r = dev_label_client.get(MANIFEST_URL)
    data = json.loads(r.text)
    assert data["name"] == "ephemera-dev"
    assert data["short_name"] == "ephemera-dev"


def test_dev_manifest_lists_only_visually_light_icons(dev_label_client):
    # In our naming, icon-*-dark-* is the light-bg/dark-glyph asset
    # (used when the OS is in dark mode -- visually a light tile). The
    # dev manifest must list ONLY these so a fresh install on a dev
    # box captures the visually-light tile, regardless of which
    # variant the browser would prefer at install time.
    r = dev_label_client.get(MANIFEST_URL)
    data = json.loads(r.text)
    srcs = {icon["src"] for icon in data["icons"]}
    assert srcs, "dev manifest must still list at least one icon"
    assert all("-dark-" in s for s in srcs), (
        f"dev manifest must list only visually-light (icon-*-dark-*) icons; "
        f"found visually-dark entries: {sorted(s for s in srcs if '-light-' in s)}"
    )


def test_dev_manifest_keeps_stable_id_and_start_url(dev_label_client):
    # The dev / prod cohorts must remain *the same app* in browsers
    # that respect manifest id. The label affects presentation, not
    # identity.
    r = dev_label_client.get(MANIFEST_URL)
    data = json.loads(r.text)
    assert data["id"] == "/"
    assert data["start_url"] == "/send?source=pwa"


def test_dev_head_apple_touch_icon_uses_light_variant(dev_label_client):
    html = dev_label_client.get("/send").text
    assert (
        '<link rel="apple-touch-icon" '
        'href="/static/icons/apple-touch-icon-light.png">' in html
    ), "dev head must point apple-touch-icon at the visually-light variant"


def test_dev_head_apple_mobile_web_app_title_is_suffixed(dev_label_client):
    html = dev_label_client.get("/send").text
    assert '<meta name="apple-mobile-web-app-title" content="ephemera-dev">' in html, (
        "dev head's iOS home-screen label must match the suffixed manifest name"
    )
