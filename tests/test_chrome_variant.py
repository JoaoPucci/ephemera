"""Tests for the chrome_variant template hint that strips operator-only
chrome from receiver flows and adds the language-confirm dialog on the
sender form.

Designer brief signed off (PR thread): the chrome menu was designed for
the operator and exposes rows that don't make sense on receiver flows
(language picker, sign-out, analytics opt-in). The receiver is anonymous
and one-shot -- a language switch reloads the page, which destroys an
already-revealed secret. The picker therefore must not render on
receiver pages, and the menu's rendering must remain free of any row
that would trigger location.reload() in a recipient context.

On the sender, the picker stays visible and clickable so the user is
never lied to about whether they CAN switch -- but a clickable picker
on a dirty form fires a confirm dialog ("Switch and clear / Keep
editing") via lang-confirm.js. The dialog markup is server-rendered
into the head/body when chrome_variant == "sender".
"""

from fastapi.testclient import TestClient


def test_receiver_landing_does_not_render_language_picker(client: TestClient) -> None:
    # /s/{token} routes through receiver.py, which passes
    # chrome_variant="receiver". The desktop <select id="lang-picker">
    # and the mobile <select id="chrome-menu-lang"> rows must both be
    # gated out of the rendered HTML for receiver pages.
    html = client.get("/s/anything").text
    assert 'id="lang-picker"' not in html, (
        "receiver pages must not render the desktop lang-picker select; "
        "language switch reloads the page and reload after reveal "
        "destroys the one-shot secret"
    )
    assert 'id="chrome-menu-lang"' not in html, (
        "receiver pages must not render the mobile drawer language row"
    )


def test_receiver_landing_still_renders_theme_toggle(client: TestClient) -> None:
    # Hiding the language picker should not collapse the rest of the
    # chrome menu. Theme toggle stays because it's safe (theme.js flips
    # data-theme via JS, no reload) and recipient-relevant (read in
    # dark or light, regardless of OS theme).
    html = client.get("/s/anything").text
    assert "chrome-menu-theme" in html, (
        "receiver pages must keep the theme toggle -- it doesn't reload "
        "and is recipient-relevant"
    )


def test_sender_form_renders_lang_confirm_dialog_markup(client: TestClient) -> None:
    # /send routes through sender.py with chrome_variant="sender". The
    # confirm dialog markup is rendered server-side so lang-confirm.js
    # can locate and animate it without injecting DOM at runtime
    # (matches the analytics-popover precedent).
    html = client.get("/send").text
    assert 'id="lang-confirm-dialog"' in html, (
        "sender pages must render the lang-confirm dialog shell so "
        "lang-confirm.js can show it on a dirty-form picker click"
    )
    # The dialog uses data-i18n hooks for its strings (rendered by the
    # JS shim from the JSON catalog at runtime, like analytics-popover
    # already does). Asserting the hooks rather than the english text
    # so the test stays locale-stable.
    for key in (
        "lang_confirm.title",
        "lang_confirm.cancel",
        "lang_confirm.confirm",
    ):
        assert f'data-i18n="{key}"' in html, (
            f"sender lang-confirm dialog must declare a slot for {key}"
        )


def test_receiver_does_not_render_lang_confirm_dialog(client: TestClient) -> None:
    # Dialog is only useful on the sender (where dirty state exists).
    # Rendering it on receiver is dead markup and a bug source if the
    # picker ever leaks back in.
    html = client.get("/s/anything").text
    assert 'id="lang-confirm-dialog"' not in html, (
        "the lang-confirm dialog must only render on the sender; "
        "rendering it on receiver is dead markup"
    )


def test_pages_without_chrome_variant_default_to_no_lang_confirm_dialog(client: TestClient) -> None:
    # Login / docs / other auth-gated pages don't pass a chrome_variant
    # and default to the unsuffixed (no dialog) shape -- the dialog is
    # specifically for the sender form's dirty-state path. /send/login
    # is the unauthenticated path that reaches the layout without going
    # through the sender or receiver flag-passing routes.
    html = client.get("/").text
    assert 'id="lang-confirm-dialog"' not in html, (
        "non-sender pages must not render the lang-confirm dialog"
    )
