import { beforeEach, describe, expect, it } from 'vitest';
import { loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// bindMaskToggle is a thin shared helper -- four sites use it (login
// password, login recovery, sender passphrase, receiver passphrase) and
// each of those has its own behavioural test suite. The tests here pin
// the helper's contract: the type/label/aria flips, the optional aria-
// label swap (login-only variant), the existence guard, and the i18n
// key plumbing.
// ---------------------------------------------------------------------------

function mountInputAndButton({ initialType = 'password' } = {}) {
  document.body.innerHTML = `
    <input type="${initialType}" id="field" autocomplete="off">
    <button type="button" id="toggle" aria-pressed="false"
            aria-label="show field">show</button>
  `;
  return {
    input: document.getElementById('field'),
    button: document.getElementById('toggle'),
  };
}

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('mask-toggle.js — type swap + label + aria-pressed', () => {
  it('first click flips type to "text", swaps label to "hide", flips aria-pressed', async () => {
    const { input, button } = mountInputAndButton();
    const { bindMaskToggle } = await loadModule('mask-toggle');

    bindMaskToggle(input, button);
    button.click();

    expect(input.getAttribute('type')).toBe('text');
    expect(button.textContent).toBe('hide');
    expect(button.getAttribute('aria-pressed')).toBe('true');
  });

  it('second click flips back to "password" and "show"', async () => {
    const { input, button } = mountInputAndButton();
    const { bindMaskToggle } = await loadModule('mask-toggle');

    bindMaskToggle(input, button);
    button.click();
    button.click();

    expect(input.getAttribute('type')).toBe('password');
    expect(button.textContent).toBe('show');
    expect(button.getAttribute('aria-pressed')).toBe('false');
  });
});

describe('mask-toggle.js — optional aria-label swap (login variant)', () => {
  it('flips aria-label between the show/hide keys when both are provided', async () => {
    const { input, button } = mountInputAndButton();
    const { bindMaskToggle } = await loadModule('mask-toggle');

    bindMaskToggle(input, button, {
      ariaShowKey: 'login.aria_show_password',
      ariaHideKey: 'login.aria_hide_password',
    });

    button.click();
    // aria-label resolves through window.i18n.t against the real en.json
    // catalog (loaded by loadModule's installI18nStub). The exact strings
    // are en-locked here -- if the catalog renames them, this test
    // surfaces it.
    expect(button.getAttribute('aria-label')).toBe('hide password');

    button.click();
    expect(button.getAttribute('aria-label')).toBe('show password');
  });

  it('leaves aria-label untouched when no aria-keys are passed', async () => {
    const { input, button } = mountInputAndButton();
    button.setAttribute('aria-label', 'static template label');
    const { bindMaskToggle } = await loadModule('mask-toggle');

    bindMaskToggle(input, button); // no aria keys
    button.click();
    button.click();

    expect(button.getAttribute('aria-label')).toBe('static template label');
  });
});

describe('mask-toggle.js — existence guard (silent no-op)', () => {
  it('does nothing when the input is null', async () => {
    const { button } = mountInputAndButton();
    const { bindMaskToggle } = await loadModule('mask-toggle');

    bindMaskToggle(null, button);
    // No throw; clicking the button has no listener wired so nothing
    // observable changes -- the button keeps its rest-state attributes.
    button.click();
    expect(button.getAttribute('aria-pressed')).toBe('false');
  });

  it('does nothing when the button is null', async () => {
    const { input } = mountInputAndButton();
    const { bindMaskToggle } = await loadModule('mask-toggle');

    bindMaskToggle(input, null);
    // No throw; input stays masked since there's no toggle to click.
    expect(input.getAttribute('type')).toBe('password');
  });
});

describe('mask-toggle.js — custom label keys', () => {
  it('honours labelShowKey / labelHideKey overrides instead of button.show/hide', async () => {
    // Documented hook for a future caller that wants different label
    // keys (none today; the test pins the API in case one shows up).
    const { input, button } = mountInputAndButton();
    const { bindMaskToggle } = await loadModule('mask-toggle');

    bindMaskToggle(input, button, {
      // These keys aren't in en.json; the t() shim's miss-sentinel
      // returns the key as-is, so we observe the raw key in textContent.
      // That's enough to prove the helper actually consults the keys
      // we passed rather than ignoring them.
      labelShowKey: 'custom.show.key',
      labelHideKey: 'custom.hide.key',
    });

    button.click();
    expect(button.textContent).toBe('custom.hide.key');
    button.click();
    expect(button.textContent).toBe('custom.show.key');
  });
});
