"""Tests for the Progressive Web App install surface.

The manifest, the head wiring that links it, and the icon assets that
home-screen launchers capture at install time. Designer brief signed off
in PR-thread; implementation contract:

  - GET /static/manifest.webmanifest returns 200 with the
    application/manifest+json MIME type (browsers reject manifests
    served as application/octet-stream).
  - Manifest is parseable JSON and carries the keys home-screen
    install needs: name, short_name, start_url, scope, display,
    theme_color, background_color, icons.
  - start_url is "/?source=pwa" so future telemetry can distinguish a
    home-screen launch from a browser visit (presence-only signal,
    no PII).
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
"""

import json

# ---------------------------------------------------------------------------
# Manifest endpoint
# ---------------------------------------------------------------------------


def test_manifest_endpoint_returns_200(client):
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200, r.text


def test_manifest_endpoint_uses_manifest_mime(client):
    r = client.get("/static/manifest.webmanifest")
    ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    assert ctype == "application/manifest+json", (
        f"manifest must be served as application/manifest+json, got {ctype!r}"
    )


def test_manifest_is_parseable_json(client):
    r = client.get("/static/manifest.webmanifest")
    data = json.loads(r.text)
    assert isinstance(data, dict)


def test_manifest_has_required_keys(client):
    r = client.get("/static/manifest.webmanifest")
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
    # id decouples the install's identity from start_url. Without it,
    # browsers fall back to start_url for identity and a future change
    # to where the app lands could fork the installed app from a fresh
    # install. Pin "/" forever so the same install upgrades in place
    # across start_url moves (e.g. when the app is eventually re-rooted
    # at /). Must be same-origin as start_url per the manifest spec.
    r = client.get("/static/manifest.webmanifest")
    data = json.loads(r.text)
    assert data.get("id") == "/", (
        "manifest must pin id=/ so identity is stable across start_url changes"
    )


def test_manifest_start_url_lands_on_sender(client):
    # The operator's job-to-be-done from the home screen is "create a
    # secret right now" -- the sender form is the unambiguous answer.
    # The bare root (/) returns 404 today, so start_url must point at
    # /send, not /. ?source=pwa is the presence-only signal a future
    # telemetry consumer can use to distinguish a home-screen launch
    # from a browser visit.
    r = client.get("/static/manifest.webmanifest")
    data = json.loads(r.text)
    assert data["start_url"] == "/send?source=pwa"


def test_manifest_display_is_standalone(client):
    r = client.get("/static/manifest.webmanifest")
    data = json.loads(r.text)
    assert data["display"] == "standalone"


def test_manifest_icons_cover_any_and_maskable_purposes(client):
    r = client.get("/static/manifest.webmanifest")
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
    r = client.get("/static/manifest.webmanifest")
    data = json.loads(r.text)
    sizes = {size for icon in data["icons"] for size in icon["sizes"].split()}
    assert "192x192" in sizes, "manifest must declare a 192x192 icon"
    assert "512x512" in sizes, "manifest must declare a 512x512 icon"


# ---------------------------------------------------------------------------
# Head wiring (every HTML page that could trigger install)
# ---------------------------------------------------------------------------


def test_head_links_manifest(client):
    html = client.get("/send").text
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in html, (
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


def test_head_sets_apple_mobile_web_app_title(client):
    html = client.get("/send").text
    assert '<meta name="apple-mobile-web-app-title" content="ephemera">' in html, (
        "iOS uses this for the home-screen label; without it the <title> is used and truncates ugly"
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


def test_manifest_icon_targets_resolve(client):
    r = client.get("/static/manifest.webmanifest")
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
