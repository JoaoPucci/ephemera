import { describe, it, expect, beforeEach, vi } from 'vitest';
import { evalScript, flushAsync, jsonResponse, neverResolveFetch, readStatic } from './helpers.js';

const LOGIN_JS = readStatic('login.js');

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
      <input type="text" id="code" name="code" required>

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
  document.getElementById('login-form').dispatchEvent(
    new Event('submit', { cancelable: true, bubbles: true })
  );
}

describe('login.js — submit in-flight guard', () => {
  beforeEach(() => {
    mountLoginForm();
  });

  it('fires exactly one fetch when submit is dispatched twice in succession', async () => {
    const fetchMock = vi.fn(neverResolveFetch());
    vi.stubGlobal('fetch', fetchMock);
    evalScript(LOGIN_JS);

    submitForm();
    submitForm();
    await flushAsync();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('disables the submit button and swaps its label while the request is in flight', async () => {
    vi.stubGlobal('fetch', vi.fn(neverResolveFetch()));
    evalScript(LOGIN_JS);

    submitForm();
    await flushAsync();

    const btn = submitBtn();
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toBe('Signing in…');
  });

  it('restores the button and surfaces an error on a 401', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'nope' }, 401));
    vi.stubGlobal('fetch', fetchMock);
    evalScript(LOGIN_JS);

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
    evalScript(LOGIN_JS);

    submitForm();
    await flushAsync();
    await flushAsync();

    const btn = submitBtn();
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe('Sign in');
    expect(document.getElementById('login-error').hidden).toBe(false);
  });
});
