// Shared show/hide toggle for masked text inputs.
//
// Four sites use this pattern: login password, login recovery code,
// sender passphrase, and receiver passphrase. They all do the same
// three things on click -- flip <input type> between password and text,
// swap a "show" / "hide" label, and flip aria-pressed -- with one
// optional variation: the login fields also swap aria-label between
// per-field show/hide phrases ("show password", "show recovery code"),
// while the passphrase fields keep their template-rendered aria-label
// static and rely on aria-pressed alone (per ARIA Authoring Practices
// for toggle buttons).
//
// The helper bakes in the existence guard so call sites don't repeat
// `if (input && button) { ... }` -- a missing element is a silent no-op.
//
// Usage:
//
//   import { bindMaskToggle } from './mask-toggle.js';
//
//   bindMaskToggle(passwordInput, showHideButton, {
//     ariaShowKey: 'login.aria_show_password',
//     ariaHideKey: 'login.aria_hide_password',
//   });
//
//   // No aria-label swap (the simpler shape):
//   bindMaskToggle(passphraseInput, showHideButton);
//
// Initial state must be `type="password"` -- every template renders the
// masked-by-default form, and the toggle's first click flips to "text".
// Pre-existing aria-label / aria-pressed / button text are honoured as
// the rest state (i.e. set them in the template; the helper only flips
// them on subsequent state changes).

export function bindMaskToggle(input, button, opts = {}) {
  if (!input || !button) return;
  const labelShowKey = opts.labelShowKey ?? 'button.show';
  const labelHideKey = opts.labelHideKey ?? 'button.hide';
  const { ariaShowKey, ariaHideKey } = opts;

  button.addEventListener('click', () => {
    const showing = input.getAttribute('type') === 'text';
    input.setAttribute('type', showing ? 'password' : 'text');
    button.textContent = window.i18n.t(showing ? labelShowKey : labelHideKey);
    button.setAttribute('aria-pressed', String(!showing));
    if (ariaShowKey && ariaHideKey) {
      button.setAttribute('aria-label', window.i18n.t(showing ? ariaShowKey : ariaHideKey));
    }
  });
}
