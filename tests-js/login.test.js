import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushAsync, jsonResponse, loadModule, neverResolveFetch } from './helpers.js';

function mountLoginForm() {
  document.body.innerHTML = `
    <form id="login-form">
      <label for="username">Username</label>
      <input type="text" id="username" name="username" required>

      <label for="password">Password</label>
      <div class="input-with-action">
        <input type="password" id="password" name="password" required>
        <button type="button" id="toggle-password" class="input-action"
                aria-label="show password" aria-pressed="false">show</button>
      </div>

      <label for="code" id="code-label">6-digit code</label>
      <div class="input-with-action">
        <input type="text" id="code" name="code" required>
        <button type="button" id="toggle-code" class="input-action"
                aria-label="show code" aria-pressed="false" hidden>show</button>
      </div>

      <button type="submit">Sign in</button>

      <p class="error" id="login-error" hidden></p>
      <button type="button" id="toggle-code-mode" class="link">Use a recovery code</button>
    </form>
  `;
  document.getElementById('username').value = 'admin';
  document.getElementById('password').value = 'pw';
  document.getElementById('code').value = '123456';
}

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
