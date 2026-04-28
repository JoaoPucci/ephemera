import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mountLoginForm } from './fixtures/login.js';
import { flushAsync, jsonResponse, loadModule, neverResolveFetch } from './helpers.js';

function submitBtn() {
  return document.querySelector('#login-form button[type="submit"]');
}

function submitForm() {
  document
    .getElementById('login-form')
    .dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
}

describe('login.js — submit in-flight guard', () => {
  beforeEach(() => {
    mountLoginForm();
  });

  it('fires exactly one fetch when submit is dispatched twice in succession', async () => {
    const fetchMock = vi.fn(neverResolveFetch());
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('login');

    submitForm();
    submitForm();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('disables the submit button and swaps its label while the request is in flight', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    submitForm();
    await flushAsync();

    const btn = submitBtn();
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toBe('Signing in…');
  });

  it('restores the button and surfaces an error on a 401', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'nope' }, 401));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('login');

    submitForm();
    await flushAsync();
    await flushAsync();

    const btn = submitBtn();
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe('Sign in');
    const err = document.getElementById('login-error');
    expect(err.hidden).toBe(false);
    expect(err.textContent.toLowerCase()).toContain('invalid');
  });

  it('restores the button on a network error', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('offline'));
    vi.stubGlobal('fetch', fetchMock);
    await loadModule('login');

    submitForm();
    await flushAsync();
    await flushAsync();

    const btn = submitBtn();
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe('Sign in');
    expect(document.getElementById('login-error').hidden).toBe(false);
  });
});

describe('login.js — recovery-code input masking', () => {
  beforeEach(() => {
    mountLoginForm();
  });

  it('starts in TOTP mode with the code field visible and no show/hide toggle', async () => {
    // Don't start any fetch; we're only exercising the DOM wiring.
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    const codeInput = document.getElementById('code');
    const codeToggle = document.getElementById('toggle-code');

    expect(codeInput.getAttribute('type')).toBe('text');
    expect(codeToggle.hidden).toBe(true);
  });

  it('switches the code field to masked password mode when the user toggles into recovery mode', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    document.getElementById('toggle-code-mode').click();

    const codeInput = document.getElementById('code');
    const codeToggle = document.getElementById('toggle-code');

    expect(codeInput.getAttribute('type')).toBe('password');
    expect(codeToggle.hidden).toBe(false);
    // Switching modes resets the toggle's internal state.
    expect(codeToggle.getAttribute('aria-pressed')).toBe('false');
    expect(codeToggle.textContent).toBe('show');
  });

  it('flips back to plain-text when the user toggles back to TOTP mode', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    const toggleCodeMode = document.getElementById('toggle-code-mode');
    toggleCodeMode.click(); // → recovery mode (masked)
    toggleCodeMode.click(); // → back to TOTP mode (text)

    const codeInput = document.getElementById('code');
    const codeToggle = document.getElementById('toggle-code');

    expect(codeInput.getAttribute('type')).toBe('text');
    expect(codeToggle.hidden).toBe(true);
  });

  it('show/hide button toggles the recovery-code input between password and text', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    document.getElementById('toggle-code-mode').click(); // recovery mode

    const codeInput = document.getElementById('code');
    const codeToggle = document.getElementById('toggle-code');

    // Click show
    codeToggle.click();
    expect(codeInput.getAttribute('type')).toBe('text');
    expect(codeToggle.textContent).toBe('hide');
    expect(codeToggle.getAttribute('aria-pressed')).toBe('true');
    expect(codeToggle.getAttribute('aria-label')).toBe('hide code');

    // Click hide
    codeToggle.click();
    expect(codeInput.getAttribute('type')).toBe('password');
    expect(codeToggle.textContent).toBe('show');
    expect(codeToggle.getAttribute('aria-pressed')).toBe('false');
    expect(codeToggle.getAttribute('aria-label')).toBe('show code');
  });

  it('resets the show/hide toggle state when switching modes mid-flow', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    const toggleCodeMode = document.getElementById('toggle-code-mode');
    const codeToggle = document.getElementById('toggle-code');

    toggleCodeMode.click(); // recovery mode
    codeToggle.click(); // unmasked
    expect(codeToggle.getAttribute('aria-pressed')).toBe('true');

    toggleCodeMode.click(); // back to TOTP
    toggleCodeMode.click(); // back to recovery again -- toggle should have been reset

    expect(codeToggle.getAttribute('aria-pressed')).toBe('false');
    expect(codeToggle.textContent).toBe('show');
    expect(document.getElementById('code').getAttribute('type')).toBe('password');
  });
});

describe('login.js — code field attribute swap on mode change', () => {
  beforeEach(() => {
    mountLoginForm();
  });

  it('TOTP mode keeps the field shaped for a 6-digit numeric code', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    const code = document.getElementById('code');
    expect(code.getAttribute('inputmode')).toBe('numeric');
    expect(code.getAttribute('pattern')).toBe('[0-9]{6}');
    expect(code.getAttribute('maxlength')).toBe('6');
    expect(code.getAttribute('minlength')).toBe('6');
    expect(code.getAttribute('autocomplete')).toBe('one-time-code');
    expect(document.getElementById('code-hint').hidden).toBe(true);
  });

  it('switching to recovery mode rewrites the field for the 11-char dashed format', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    document.getElementById('toggle-code-mode').click();

    const code = document.getElementById('code');
    expect(code.getAttribute('inputmode')).toBe('text');
    expect(code.getAttribute('pattern')).toBe('[A-Za-z0-9]{5}-?[A-Za-z0-9]{5}');
    expect(code.getAttribute('maxlength')).toBe('11');
    expect(code.getAttribute('minlength')).toBe('10');
    expect(code.getAttribute('autocapitalize')).toBe('characters');
    expect(code.getAttribute('autocomplete')).toBe('off');
    // Hint reveals an "10 characters, dash optional" nudge in recovery mode.
    const hint = document.getElementById('code-hint');
    expect(hint.hidden).toBe(false);
    expect(hint.textContent.length).toBeGreaterThan(0);
  });

  it('flipping back to TOTP restores the numeric attributes and hides the hint', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');

    const toggleCodeMode = document.getElementById('toggle-code-mode');
    toggleCodeMode.click(); // recovery
    toggleCodeMode.click(); // back to TOTP

    const code = document.getElementById('code');
    expect(code.getAttribute('inputmode')).toBe('numeric');
    expect(code.getAttribute('pattern')).toBe('[0-9]{6}');
    expect(code.getAttribute('maxlength')).toBe('6');
    expect(document.getElementById('code-hint').hidden).toBe(true);
  });
});

describe('login.js — recovery code soft-format on input', () => {
  beforeEach(() => {
    mountLoginForm();
  });

  // Helper: type a value into the code input and dispatch a synthetic
  // 'input' event so the soft-format listener runs. Cursor goes to end --
  // matches the steady-state of typing left-to-right.
  function typeIntoCode(value) {
    const code = document.getElementById('code');
    code.value = value;
    code.setSelectionRange(value.length, value.length);
    code.dispatchEvent(new Event('input', { bubbles: true }));
    return code;
  }

  async function enterRecoveryMode() {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');
    document.getElementById('toggle-code-mode').click();
  }

  it('uppercases lowercase letters as the user types', async () => {
    await enterRecoveryMode();
    const code = typeIntoCode('abc');
    expect(code.value).toBe('ABC');
  });

  it('strips characters outside [A-Za-z0-9]', async () => {
    await enterRecoveryMode();
    const code = typeIntoCode('ab*c! 1');
    expect(code.value).toBe('ABC1');
  });

  it('auto-inserts the dash after the 5th alphanumeric character', async () => {
    await enterRecoveryMode();
    const code = typeIntoCode('ABCDE6');
    expect(code.value).toBe('ABCDE-6');
  });

  it('truncates to 10 alphanumeric characters (11 visible with the dash)', async () => {
    await enterRecoveryMode();
    const code = typeIntoCode('ABCDEFGHIJKL');
    expect(code.value).toBe('ABCDE-FGHIJ');
    expect(code.value.length).toBe(11);
  });

  it('preserves an already-dashed paste exactly as written (after uppercasing)', async () => {
    await enterRecoveryMode();
    const code = typeIntoCode('abcde-fghij');
    expect(code.value).toBe('ABCDE-FGHIJ');
  });

  it('does not transform input in TOTP mode', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    await loadModule('login');
    // Stay in TOTP mode (no toggle click). HTML5 maxlength would normally
    // cap a 6-digit field, but jsdom does not enforce it on .value
    // assignment -- so the assertion here is "the listener does not
    // mutate the value", which is the actual contract.
    const code = typeIntoCode('abc-123');
    expect(code.value).toBe('abc-123'); // unchanged: soft-format listener inactive
  });
});
